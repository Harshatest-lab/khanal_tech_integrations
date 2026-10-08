"""Sync amazonuk.order_items from SP-API (orders + shipment report).

Entry points, called by handlers.fetch_handler:
  sync_incremental()      lastUpdatedAfter watermark sync (scheduled)
  sync(start, end)        manual date-range backfill (createdAfter/Before)

Incremental mode fetches by lastUpdatedAfter watermark to catch status
changes (e.g. Pending -> Shipped) on orders purchased any number of days
ago — not just orders created "today". See sync_incremental's docstring.

Posting to SAP is a separate Lambda (handlers.post_invoices_handler ->
post_to_sap); it runs after this one and picks up whatever is Shipped.

This can run for many minutes (SP-API's orders endpoint paces at ~180s/page
— see fetch_orders.PAGE_PACING_SECONDS). All progress is logged via applog
to amazonuk.sync_log in RDS, and echoed to stdout/CloudWatch.
"""

from datetime import datetime, timedelta, timezone

from amazon_uk import applog
from amazon_uk import store
from amazon_uk import fetch_orders
from amazon_uk import fetch_shipment_report

log = applog.get_logger("amz_sync")

# Buffer subtracted from the watermark before every incremental fetch, to
# cover any order updated in the gap between "we read the watermark" and
# "SP-API actually served the request" on the previous run. Same purpose as
# the Amazon US pipeline's INCREMENTAL_LOOKBACK_DAYS, scaled down — this
# job is meant to run daily, not every 30 min, so the gap it needs to cover
# is naturally smaller.
INCREMENTAL_LOOKBACK_HOURS = 6

# First-ever run has no watermark yet — how far back to catch up.
INITIAL_LOOKBACK_DAYS = 7

# Upper bound on ONE run's lastUpdated window. The orders endpoint paces at
# ~180 s/page (~100 orders/page) and the Lambda has 900 s, so an unbounded
# catch-up after downtime (or the 7-day first run) could time out — and a
# timed-out run never saves its watermark, so the next run's window is even
# bigger. Capping the window makes every run finish; the watermark advances
# to the window's end and a long gap simply drains over consecutive runs
# (each run moves it forward by MAX_WINDOW_HOURS - INCREMENTAL_LOOKBACK_HOURS).
MAX_WINDOW_HOURS = 48


def fetch_and_store_orders(start: str, end: str) -> dict:
    """Step 1: fetch every order (each already carries its items embedded,
    via the single bulk orders endpoint) and store them. No more per-order
    loop — fetch_orders.fetch_orders() already paginates the whole range."""
    orders = fetch_orders.fetch_orders(start, end)
    log.info(f"Orders fetched: {len(orders)}")

    items_stored = store.upsert_orders(orders)
    log.info(f"Order items stored: {items_stored}")

    return {"orders": len(orders), "items_stored": items_stored}


def fetch_and_store_shipments(start: str, end: str) -> dict:
    """Step 2: fetch the shipment report and append the actual ship date."""
    report_rows = fetch_shipment_report.fetch(start, end)
    shipments_stored = store.upsert_shipment_rows(report_rows)
    log.info(f"Shipment rows stored: {shipments_stored}")

    return {"shipments_stored": shipments_stored}



# Manual backfill takes calendar-date strings (e.g. "2026-08-01") with no
# timezone — they're meant as UK-local calendar days, but get turned into a
# literal UTC midnight boundary below. UK local time is ahead of UTC by up
# to 1h (BST, UTC+1) for roughly half the year, so an order placed UK-local
# 00:00-00:59 on start_date (or end_date+1) falls just outside a
# non-padded [start_date T00:00Z, end_date T23:59Z] window. Padding by 2h
# on both ends covers that with margin; upserts are idempotent
# (amazon_order_id + order_item_id PK), so the overlap can't duplicate or
# miscategorize anything.
BACKFILL_EDGE_PAD_HOURS = 2


def sync(start_date: str, end_date: str) -> dict:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=timezone.utc)
    start_dt -= timedelta(hours=BACKFILL_EDGE_PAD_HOURS)
    end_dt = min(end_dt + timedelta(hours=BACKFILL_EDGE_PAD_HOURS), datetime.now(timezone.utc))
    start, end = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    order_result = fetch_and_store_orders(start, end)
    shipment_result = fetch_and_store_shipments(start, end)

    return {**order_result, **shipment_result}


def sync_incremental(initial_lookback_days: int | None = None) -> dict:
    """Incremental sync — scheduled daily via EventBridge.
    Fetches every order UPDATED since the last successful run (fetch_orders_by_update, lastUpdatedAfter) instead of
    orders created "today" — so an order purchased days ago that just
    flipped to Shipped gets picked up and re-upserted (its order_status
    column updates via the existing ON CONFLICT DO UPDATE), not missed.
    That flip is what makes the order eligible for the AR-invoice post
    step (post_to_sap, its own Lambda).

    Watermark = MAX(watermark) from amazonuk.fetch_log for source='orders'.
    First-ever run (no watermark yet) catches up initial_lookback_days
    (defaults to INITIAL_LOOKBACK_DAYS, 7). Because one run's window is
    capped at MAX_WINDOW_HOURS, that catch-up (like any gap after downtime)
    drains automatically over consecutive scheduled runs — no manual seeding
    or backfill needed. Pass a smaller initial_lookback_days only to get a
    first watermark sooner.

    New watermark is saved AFTER the fetch completes — not before — as "now"
    for an uncapped run, or the window's end for a capped one — so the next
    run's INCREMENTAL_LOOKBACK_HOURS buffer covers anything updated while
    this run's (possibly slow, paginated) fetch was still in flight. Same
    pattern as the sibling Amazon US pipeline's sync_watermark.

    Also re-fetches the shipment report for a trailing window, since the
    Reports API has no equivalent incremental cursor — shipment data for a
    recently-shipped order can land a day or two after the ship date.
    """
    lookback_days = initial_lookback_days if initial_lookback_days is not None else INITIAL_LOOKBACK_DAYS
    last_watermark = store.get_last_watermark("orders")
    now = datetime.now(timezone.utc)
    if last_watermark is None:
        fetch_from = now - timedelta(days=lookback_days)
        log.info(f"No watermark found — first run, catching up {lookback_days} days")
    else:
        fetch_from = last_watermark - timedelta(hours=INCREMENTAL_LOOKBACK_HOURS)

    # Cap the window (see MAX_WINDOW_HOURS). Uncapped runs stay open-ended
    # (no lastUpdatedBefore) and the watermark becomes "now", exactly as
    # before; capped runs bound the query and the watermark becomes the
    # window's end, so the next run continues from there.
    window_end = fetch_from + timedelta(hours=MAX_WINDOW_HOURS)
    capped = window_end < now
    fetch_from_str = fetch_from.strftime("%Y-%m-%dT%H:%M:%SZ")
    fetch_to_str = window_end.strftime("%Y-%m-%dT%H:%M:%SZ") if capped else None
    log.info(f"LastUpdatedAfter={fetch_from_str} LastUpdatedBefore={fetch_to_str or 'now'} "
             f"(watermark={last_watermark}, lookback={INCREMENTAL_LOOKBACK_HOURS}h)")
    if capped:
        log.info(f"Window capped at {MAX_WINDOW_HOURS}h — catching up; the next run continues from "
                 f"{window_end.isoformat()}")

    orders = fetch_orders.fetch_orders_by_update(fetch_from_str, fetch_to_str)
    log.info(f"Got {len(orders)} orders to process")

    items_stored = store.upsert_orders(orders)
    log.info(f"Order items stored: {items_stored}")

    new_watermark = window_end if capped else datetime.now(timezone.utc)
    store.record_fetch_log("orders", new_watermark, len(orders), items_stored)

    shipment_end = new_watermark.strftime("%Y-%m-%dT%H:%M:%SZ")
    shipment_start = (new_watermark - timedelta(days=INITIAL_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    shipment_result = fetch_and_store_shipments(shipment_start, shipment_end)

    log.info(f"Done. orders={len(orders)} items_stored={items_stored} "
             f"shipments={shipment_result['shipments_stored']} watermark→{new_watermark.isoformat()}")

    return {"orders": len(orders), "items_stored": items_stored,
            "watermark": new_watermark.isoformat(), "window_capped": capped, **shipment_result}
