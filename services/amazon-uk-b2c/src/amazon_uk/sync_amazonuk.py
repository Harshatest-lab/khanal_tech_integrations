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

import time
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

# The window is fetched in slices of this many hours, each stored and its
# watermark saved as soon as it completes. Before slicing, a run stored
# NOTHING until every page was fetched — so a window with more orders than
# one Lambda can page through (~100 orders / 180 s / 900 s budget) timed out
# every time, lost all work and left the watermark where it was (the
# 2026-10-07 dev outage). Now a run keeps whatever slices it finished and the
# next run (or chained invocation) resumes from the last saved slice end.
SLICE_HOURS = 6

# Time kept free at the end of a run for the shipment report (it polls
# every 30 s, up to 20 times) plus its DB writes.
SHIPMENT_RESERVE_SECONDS = 240
SHIPMENT_POLL_SECONDS = 30


def fetch_and_store_orders(start: str, end: str) -> dict:
    """Step 1: fetch every order (each already carries its items embedded,
    via the single bulk orders endpoint) and store them. No more per-order
    loop — fetch_orders.fetch_orders() already paginates the whole range."""
    orders = fetch_orders.fetch_orders(start, end)
    log.info(f"Orders fetched: {len(orders)}")

    items_stored = store.upsert_orders(orders)
    log.info(f"Order items stored: {items_stored}")

    return {"orders": len(orders), "items_stored": items_stored}


def fetch_and_store_shipments(start: str, end: str, max_polls: int = 20) -> dict:
    """Step 2: fetch the shipment report and append the actual ship date."""
    report_rows = fetch_shipment_report.fetch(start, end, max_polls=max_polls)
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


def sync_incremental(initial_lookback_days: int | None = None, time_budget_seconds: float | None = None) -> dict:
    """Incremental sync — scheduled daily via EventBridge.
    Fetches every order UPDATED since the last successful run (fetch_orders_by_update, lastUpdatedAfter) instead of
    orders created "today" — so an order purchased days ago that just
    flipped to Shipped gets picked up and re-upserted (its order_status
    column updates via the existing ON CONFLICT DO UPDATE), not missed.
    That flip is what makes the order eligible for the AR-invoice post
    step (post_to_sap, its own Lambda).

    Watermark = MAX(watermark) from amazonuk.fetch_log for source='orders'.
    First-ever run (no watermark yet) catches up initial_lookback_days
    (defaults to INITIAL_LOOKBACK_DAYS, 7). One run's window is capped at
    MAX_WINDOW_HOURS and fetched in SLICE_HOURS slices; each slice is stored
    and its end saved as the watermark as soon as it completes, so a gap
    drains over consecutive runs and a run that runs out of time keeps
    everything it finished.

    time_budget_seconds (the Lambda's remaining time, minus safety): when
    given, no page is started that cannot finish inside it, and
    SHIPMENT_RESERVE_SECONDS are kept for the shipment report. The result
    then has budget_exhausted=True and more remains to fetch — the caller
    should run again (handlers.fetch_handler chains itself). None = no
    limit (local/manual runs).

    The final slice of an uncapped window is open-ended (no
    lastUpdatedBefore) and its watermark is "now" when it completes, so the
    next run's INCREMENTAL_LOOKBACK_HOURS buffer covers anything updated
    while it was in flight. Same pattern as the sibling Amazon US
    pipeline's sync_watermark.

    Also re-fetches the shipment report for a trailing window, since the
    Reports API has no equivalent incremental cursor — shipment data for a
    recently-shipped order can land a day or two after the ship date.
    """
    started = time.monotonic()
    deadline = started + time_budget_seconds if time_budget_seconds is not None else None
    orders_deadline = deadline - SHIPMENT_RESERVE_SECONDS if deadline is not None else None

    lookback_days = initial_lookback_days if initial_lookback_days is not None else INITIAL_LOOKBACK_DAYS
    last_watermark = store.get_last_watermark("orders")
    now = datetime.now(timezone.utc)
    if last_watermark is None:
        fetch_from = now - timedelta(days=lookback_days)
        log.info(f"No watermark found — first run, catching up {lookback_days} days")
    else:
        fetch_from = last_watermark - timedelta(hours=INCREMENTAL_LOOKBACK_HOURS)

    window_end = fetch_from + timedelta(hours=MAX_WINDOW_HOURS)
    capped = window_end < now
    limit = window_end if capped else now
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info(f"LastUpdatedAfter={fmt(fetch_from)} LastUpdatedBefore={fmt(window_end) if capped else 'now'} "
             f"(watermark={last_watermark}, lookback={INCREMENTAL_LOOKBACK_HOURS}h, slices of {SLICE_HOURS}h)")
    if capped:
        log.info(f"Window capped at {MAX_WINDOW_HOURS}h — catching up; the next run continues from "
                 f"{window_end.isoformat()}")

    total_orders = total_items = slices_done = 0
    new_watermark = last_watermark
    budget_exhausted = False
    cursor = fetch_from
    while cursor < limit:
        slice_end = min(cursor + timedelta(hours=SLICE_HOURS), limit)
        final = slice_end >= limit
        open_ended = final and not capped          # final slice of an uncapped window: no lastUpdatedBefore
        try:
            orders = fetch_orders.fetch_orders_by_update(fmt(cursor), None if open_ended else fmt(slice_end),
                                                         orders_deadline)
        except fetch_orders.FetchBudgetExceeded as exc:
            if exc.partial:                         # idempotent upsert; watermark stays at the last finished slice
                store.upsert_orders(exc.partial)
                log.info(f"Budget hit mid-slice: stored {len(exc.partial)} partial orders (watermark not advanced)")
            budget_exhausted = True
            break
        items_stored = store.upsert_orders(orders)
        new_watermark = datetime.now(timezone.utc) if open_ended else slice_end
        store.record_fetch_log("orders", new_watermark, len(orders), items_stored)
        total_orders += len(orders)
        total_items += items_stored
        slices_done += 1
        log.info(f"Slice {fmt(cursor)} -> {'now' if open_ended else fmt(slice_end)}: "
                 f"{len(orders)} orders, {items_stored} items stored, watermark→{new_watermark.isoformat()}")
        cursor = slice_end

    if budget_exhausted:
        log.info(f"Out of time budget after {slices_done} slice(s) — more remains; run again to continue "
                 f"from {new_watermark.isoformat() if new_watermark else fmt(fetch_from)}")

    shipment_result = {"shipments_stored": 0}
    if slices_done and new_watermark is not None:
        max_polls = 20
        if deadline is not None:
            max_polls = int((deadline - time.monotonic() - 45) // SHIPMENT_POLL_SECONDS)
        if max_polls >= 2:
            shipment_end = new_watermark.strftime("%Y-%m-%dT%H:%M:%SZ")
            shipment_start = (new_watermark - timedelta(days=INITIAL_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
            shipment_result = fetch_and_store_shipments(shipment_start, shipment_end, max_polls=min(20, max_polls))
        else:
            log.info("Skipping shipment report this run (no time left); the next run re-fetches a trailing window")

    log.info(f"Done. orders={total_orders} items_stored={total_items} slices={slices_done} "
             f"shipments={shipment_result['shipments_stored']} budget_exhausted={budget_exhausted} "
             f"watermark→{new_watermark.isoformat() if new_watermark else None}")

    return {"orders": total_orders, "items_stored": total_items,
            "watermark": new_watermark.isoformat() if new_watermark else None,
            "window_capped": capped, "slices": slices_done, "budget_exhausted": budget_exhausted,
            **shipment_result}
