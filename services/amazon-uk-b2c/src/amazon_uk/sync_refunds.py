"""Sync amazonuk.refund_events / refund_items from the Finances API.

Entry points, called by handlers.fetch_refunds_handler:
  sync_incremental()      postedAfter watermark sync (scheduled)
  sync(start, end)        manual date-range backfill

Incremental mode: refunds POSTED since the last run's watermark (from
amazonuk.fetch_log, source='refunds') minus a small overlap, since a
refund can post days after it was issued. Refunds typically land ~8-10
days after the order was purchased, so this catches them independently of
the orders sync.
"""

from datetime import datetime, timedelta, timezone

from amazon_uk import applog
from amazon_uk import fetch_refunds
from amazon_uk import store

log = applog.get_logger("amz_refunds")

# Buffer subtracted from the watermark — covers refunds that Amazon posts
# between "we read the watermark" and "the API serves the query".
INCREMENTAL_LOOKBACK_HOURS = 24

# First-ever run: how far back to catch up.
INITIAL_LOOKBACK_DAYS = 60  # within Finances' 180-day window

# The 2024-06-19 transactions endpoint (unlike the old v0 API) rejects a
# postedBefore within 2 minutes of the actual request time (confirmed live:
# "End date must be no later than 2 minutes before to the current time").
# 3 minutes leaves margin for the request itself taking a moment to land.
POSTED_BEFORE_SAFETY_MARGIN_MINUTES = 3


def fetch_and_store_refunds(posted_after: str, posted_before: str) -> dict:
    events = fetch_refunds.fetch_refunds(posted_after, posted_before)
    stored = store.upsert_refunds(events)
    log.info(f"Refund events stored: {stored}")
    return {"refund_events": len(events), "events_stored": stored}



# Same UK-local-vs-UTC edge as sync_amazonuk.BACKFILL_EDGE_PAD_HOURS — a
# manual backfill's start_date/end_date are meant as UK-local calendar days;
# padding by 2h on both ends covers the up-to-1h BST gap with margin.
# Upserts are idempotent (keyed on transaction_id/item), so the overlap
# can't duplicate anything.
BACKFILL_EDGE_PAD_HOURS = 2


def sync(start_date: str, end_date: str) -> dict:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=timezone.utc)
    start_dt -= timedelta(hours=BACKFILL_EDGE_PAD_HOURS)
    # Finances API rejects postedBefore within 2 min of now (see
    # POSTED_BEFORE_SAFETY_MARGIN_MINUTES) — clamp the padded end the same
    # way sync_incremental does.
    now_cutoff = datetime.now(timezone.utc) - timedelta(minutes=POSTED_BEFORE_SAFETY_MARGIN_MINUTES)
    end_dt = min(end_dt + timedelta(hours=BACKFILL_EDGE_PAD_HOURS), now_cutoff)
    start, end = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info(f"=== refund sync {start} -> {end} ===")
    return fetch_and_store_refunds(start, end)


def sync_incremental(initial_lookback_days: int | None = None) -> dict:
    lookback_days = (initial_lookback_days if initial_lookback_days is not None
                     else INITIAL_LOOKBACK_DAYS)
    last_watermark = store.get_last_watermark("refunds")
    if last_watermark is None:
        fetch_from = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        log.info(f"No watermark found — first run, catching up {lookback_days} days")
    else:
        fetch_from = last_watermark - timedelta(hours=INCREMENTAL_LOOKBACK_HOURS)
    new_watermark = datetime.now(timezone.utc) - timedelta(minutes=POSTED_BEFORE_SAFETY_MARGIN_MINUTES)
    result = fetch_and_store_refunds(
        fetch_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
        new_watermark.strftime("%Y-%m-%dT%H:%M:%SZ"))

    store.record_fetch_log("refunds", new_watermark, result["refund_events"], result["events_stored"])
    log.info(f"Done. {result} watermark→{new_watermark.isoformat()}")
    return {**result, "watermark": new_watermark.isoformat()}
