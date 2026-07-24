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

from unicommerce import returns as returns_mod
from unicommerce import store
from unicommerce.credentials import UcCredentials, get_uc_credentials
from unicommerce.dates import parse_source_datetime
from unicommerce.mappers import extract_uc_order_id, order_ready_for_invoice
from unicommerce.uc_client import UcClient

logger = logging.getLogger(__name__)

ORDER_CURSOR_OVERLAP_MINUTES = 20
RETURN_CURSOR_OVERLAP_MINUTES = 45
_WATERMARK_ORDERS = "orders"
_WATERMARK_RETURNS = "returns"
_DEFAULT_LOOKBACK_DAYS = 60
_DEFAULT_RETURN_LOOKBACK_DAYS = 90


def _order_event_timestamp(order: dict):
    return (
        parse_source_datetime(order.get("updated"))
        or parse_source_datetime(order.get("channelProcessingTime"))
        or parse_source_datetime(order.get("created"))
        or parse_source_datetime(order.get("displayOrderDateTime"))
    )


def _return_event_timestamp(ret: dict):
    rsov = ret.get("returnSaleOrderValue") or {}
    return (
        parse_source_datetime(rsov.get("returnCompletedDate"))
        or parse_source_datetime(rsov.get("inventoryReceivedDate"))
        or parse_source_datetime(rsov.get("returnCreatedDate"))
        or parse_source_datetime(ret.get("updated"))
        or parse_source_datetime(ret.get("created"))
    )


def _format(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _next_cursor(
    since: str,
    cycle_ended_at: datetime,
    seen: list[datetime],
    deferred: list[datetime],
    overlap_minutes: int = ORDER_CURSOR_OVERLAP_MINUTES,
    default_lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
) -> str:
    try:
        base_since = datetime.strptime(since[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        base_since = datetime.now(timezone.utc) - timedelta(days=default_lookback_days)

    if deferred:
        anchor = min(deferred)
    elif seen:
        anchor = max(seen)
    else:
        anchor = cycle_ended_at

    candidate = anchor - timedelta(minutes=abs(overlap_minutes))
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


def fetch_returns(
    conn,
    max_returns: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict:
    """Fetch UC returns (CIR + RTO) and stage them as credit notes.

    Ported from khanal_tech_integrations/unicommerce/workers/fetch.py's
    fetch_returns. Read-only against Unicommerce + Postgres (no SAP
    writes). A return whose parent sale order isn't staged yet is
    skipped (fetch orders for that window first). Cursor mode uses UC's
    updatedFrom/updatedTo (catch status changes on old returns); an
    explicit range uses createdFrom/createdTo.
    """
    creds: UcCredentials = get_uc_credentials()
    uc = UcClient(creds)
    uc.login()

    explicit_range = bool(from_date)
    default_since = (datetime.now(timezone.utc) - timedelta(days=_DEFAULT_RETURN_LOOKBACK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    cycle_ended_at = datetime.now(timezone.utc)

    if explicit_range:
        since = str(from_date).strip()
        if len(since) == 10:
            since += " 00:00:00"
        end = str(to_date).strip() if to_date else _format(cycle_ended_at)
        if len(end) == 10:
            end += " 23:59:59"
    else:
        since = store.get_watermark(conn, _WATERMARK_RETURNS) or default_since
        end = _format(cycle_ended_at)

    run_id = store.start_sync_log(conn, "fetch_returns", since, end)

    try:
        rets = uc.get_returns_between(from_date=since, to_date=end, use_updated=not explicit_range)
        if max_returns:
            rets = rets[: int(max_returns)]

        total = len(rets)
        created = updated = skipped_existing = skipped_parent = skipped_no_ids = 0
        seen_timestamps: list[datetime] = []
        deferred_timestamps: list[datetime] = []

        for ret in rets:
            uc_return_id, uc_order_id, fallback_codes = returns_mod.extract_return_ids(ret)
            if not uc_order_id and fallback_codes:
                uc_order_id = store.find_order_ref_by_line_codes(conn, fallback_codes)
            if not uc_return_id or not uc_order_id:
                skipped_no_ids += 1
                continue

            return_ts = _return_event_timestamp(ret)
            if return_ts:
                seen_timestamps.append(return_ts)

            if store.get_cn_status(conn, uc_return_id) == "posted":
                skipped_existing += 1
                continue

            parent = store.get_order(conn, uc_order_id)
            if not parent:
                skipped_parent += 1
                if return_ts:
                    deferred_timestamps.append(return_ts)
                continue

            items = returns_mod.extract_return_items(ret)
            rsov = ret.get("returnSaleOrderValue") or {}
            fallback_warehouse = rsov.get("forwardItemFacility") or ret.get("returnFacilityCode") or ""
            prepared = returns_mod.prepare_credit_note_items(parent, items, fallback_warehouse=fallback_warehouse)

            parent_ready = parent.get("pipeline_status") == "posted" and bool(parent.get("sap_ar_invoice_docentry"))
            if not parent_ready or prepared["unresolved_count"]:
                if return_ts:
                    deferred_timestamps.append(return_ts)
            last_error = ""
            if not parent_ready:
                last_error = "Parent order is not posted to SAP yet"
            elif prepared["unresolved_count"]:
                last_error = f"Awaiting SAP base-line mapping for {prepared['unresolved_count']} return item(s)"

            outcome = store.upsert_cn_staging(
                conn,
                uc_return_id=uc_return_id,
                uc_order_id=parent["uniware_id"],
                parent_sap_doc_entry=parent["sap_ar_invoice_docentry"] if parent_ready else "",
                return_date=returns_mod.extract_return_date(ret),
                return_type=returns_mod.extract_return_type(ret),
                items=prepared["items"],
                status="pending",
                last_error=last_error,
            )
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1
            elif outcome == "skipped_posted":
                skipped_existing += 1

        if explicit_range:
            next_cursor = since
        else:
            next_cursor = _next_cursor(
                since, cycle_ended_at, seen_timestamps, deferred_timestamps,
                overlap_minutes=RETURN_CURSOR_OVERLAP_MINUTES,
                default_lookback_days=_DEFAULT_RETURN_LOOKBACK_DAYS,
            )
            store.set_watermark(conn, _WATERMARK_RETURNS, next_cursor)

        counts = {
            "fetched": total,
            "created": created,
            "updated": updated,
            "skipped_existing": skipped_existing,
            "skipped_parent_not_found": skipped_parent,
            "skipped_no_ids": skipped_no_ids,
        }
        store.finish_sync_log(conn, run_id, "success", counts)
        return {**counts, "since": since, "to": end, "explicit_range": explicit_range, "next_cursor": next_cursor}
    except Exception as e:
        store.finish_sync_log(conn, run_id, "error", error=str(e))
        raise
