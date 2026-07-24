"""Fetch + stage Unicommerce orders into Postgres.

Ported from khanal_tech_integrations/unicommerce/workers/fetch.py's
fetch_orders. Cursor mode (no explicit range given) advances a watermark
using the *earliest deferred* order's timestamp (not "now"), so a detail
fetch that failed this run doesn't get silently skipped by an
overly-optimistic cursor advance next run -- same reasoning as the Frappe
source.

Returns are not yet implemented (next slice of this build, once fetch_orders
is proven in the dual-run comparison against Frappe).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from unicommerce import store
from unicommerce.credentials import UcCredentials, get_uc_credentials
from unicommerce.dates import parse_source_datetime
from unicommerce.mappers import extract_uc_order_id, order_ready_for_invoice
from unicommerce.uc_client import UcClient

logger = logging.getLogger(__name__)

ORDER_CURSOR_OVERLAP_MINUTES = 20
_WATERMARK_ORDERS = "orders"
_DEFAULT_LOOKBACK_DAYS = 60


def _order_event_timestamp(order: dict):
    return (
        parse_source_datetime(order.get("updated"))
        or parse_source_datetime(order.get("channelProcessingTime"))
        or parse_source_datetime(order.get("created"))
        or parse_source_datetime(order.get("displayOrderDateTime"))
    )


def _format(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _next_cursor(since: str, cycle_ended_at: datetime, seen: list[datetime], deferred: list[datetime]) -> str:
    try:
        base_since = datetime.strptime(since[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        base_since = datetime.now(timezone.utc) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)

    if deferred:
        anchor = min(deferred)
    elif seen:
        anchor = max(seen)
    else:
        anchor = cycle_ended_at

    candidate = anchor - timedelta(minutes=abs(ORDER_CURSOR_OVERLAP_MINUTES))
    if candidate < base_since:
        candidate = base_since
    return _format(candidate)


def fetch_orders(
    conn,
    max_orders: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    channel_name: str | None = None,
) -> dict:
    """Fetch new/updated orders from Unicommerce and stage them.

    from_date/to_date: explicit range (manual backfill) -- cursor is NOT
    advanced. Omitted: the stored watermark drives the window and is
    advanced after the run.
    """
    creds: UcCredentials = get_uc_credentials()
    uc = UcClient(creds)
    uc.login()

    explicit_range = bool(from_date)
    default_since = (datetime.now(timezone.utc) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    cycle_ended_at = datetime.now(timezone.utc)

    if explicit_range:
        since = str(from_date).strip()
        if len(since) == 10:
            since += " 00:00:00"
        end = str(to_date).strip() if to_date else _format(cycle_ended_at)
        if len(end) == 10:
            end += " 23:59:59"
    else:
        since = store.get_watermark(conn, _WATERMARK_ORDERS) or default_since
        end = _format(cycle_ended_at)

    run_id = store.start_sync_log(conn, "fetch_orders", since, end)

    try:
        orders = uc.get_orders_between(from_date=since, to_date=end, channel=channel_name or None)
        if max_orders:
            orders = orders[: int(max_orders)]

        total = len(orders)
        new_count = updated_count = skipped_posted = detail_failed = 0
        seen_timestamps: list[datetime] = []
        deferred_timestamps: list[datetime] = []

        for summary in orders:
            uc_order_id = extract_uc_order_id(summary)
            if not uc_order_id:
                continue
            summary_ts = _order_event_timestamp(summary)
            if summary_ts:
                seen_timestamps.append(summary_ts)

            try:
                order = uc.get_order_detail(uc_order_id)
                if not order:
                    detail_failed += 1
                    if summary_ts:
                        deferred_timestamps.append(summary_ts)
                    continue
            except Exception as exc:
                detail_failed += 1
                if summary_ts:
                    deferred_timestamps.append(summary_ts)
                logger.warning("fetch_orders: detail fetch failed for %s: %s", uc_order_id, exc)
                continue

            result = store.upsert_order(conn, uc_order_id, order)
            if result == "created":
                new_count += 1
            elif result == "updated":
                updated_count += 1
            elif result == "skipped_posted":
                skipped_posted += 1

        if explicit_range:
            next_cursor = since
        else:
            next_cursor = _next_cursor(since, cycle_ended_at, seen_timestamps, deferred_timestamps)
            store.set_watermark(conn, _WATERMARK_ORDERS, next_cursor)

        counts = {
            "fetched": total,
            "new": new_count,
            "updated": updated_count,
            "skipped_posted": skipped_posted,
            "detail_failed": detail_failed,
        }
        store.finish_sync_log(conn, run_id, "success", counts)
        return {
            **counts,
            "since": since,
            "to": end,
            "explicit_range": explicit_range,
            "next_cursor": next_cursor,
        }
    except Exception as e:
        store.finish_sync_log(conn, run_id, "error", error=str(e))
        raise


def promote_fetched_to_ready(
    conn, limit: int | None = None, start_date: str | None = None, end_date: str | None = None
) -> dict:
    """Move invoice-eligible 'fetched' orders to 'ready'.

    Ported from khanal_tech_integrations/unicommerce/workers/fetch.py's
    promote_fetched_to_ready. Called as the first step of every posting
    run (workers/invoice.py:212 in the Frappe source) -- orders are
    staged as 'fetched' and only become postable once this promotes the
    ones that are COMPLETE with eligible line items (mappers.
    order_ready_for_invoice). Orders that aren't ready yet stay 'fetched'
    with a reason recorded, so a later cycle re-checks them once they
    complete upstream in UC.
    """
    rows = store.get_fetched_orders(conn, limit=limit, start_date=start_date, end_date=end_date)
    moved = deferred = 0
    for row in rows:
        raw = row["order_json"]
        order = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
        is_ready, reason = order_ready_for_invoice(order)
        if not is_ready:
            store.defer_fetched(conn, row["uniware_id"], reason)
            deferred += 1
            continue
        if store.mark_ready(conn, row["uniware_id"]):
            moved += 1
    return {"moved_to_ready": moved, "deferred": deferred}
