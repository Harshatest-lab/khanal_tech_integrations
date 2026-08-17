"""Sync FBA shipment dates into staged orders' actual_ship_date.

The FBA shipments report is the ONLY source of ship dates, and ship date
is the SAP DocDate — orders wait in 'ready' until this sync covers them.
Report windows are chunked to <= 30 days (longer windows FATAL).
"""

from __future__ import annotations

import csv
import io
import logging

from amazon_us import constants, reports_api, store
from amazon_us.dates import date_chunks, pacific_day_to_utc, utc_to_pacific_date
from amazon_us.sp_api import SpApiThrottledError

logger = logging.getLogger(__name__)


def parse_earliest_ship_dates(tsv_content: str) -> dict[str, str]:
    """amazon-order-id -> earliest Pacific shipment-date across its lines
    (matches Amazon's own MTR alignment)."""
    ship_dates: dict[str, str] = {}
    reader = csv.DictReader(io.StringIO(tsv_content), delimiter="\t")
    for row in reader:
        order_id = (row.get("amazon-order-id") or "").strip()
        raw_date = (row.get("shipment-date") or "").strip()
        if not order_id or not raw_date:
            continue
        pacific_date = utc_to_pacific_date(raw_date)
        if not pacific_date:
            continue
        if order_id not in ship_dates or pacific_date < ship_dates[order_id]:
            ship_dates[order_id] = pacific_date
    return ship_dates


# See fetch_sync._REPORT_POLL_BUDGET_SEC / _RESUME_POLL_BUDGET_SEC -- same
# reasoning: don't block a whole invocation on poll_report's 840s default
# when createReport shares one global quota with every other report type.
_REPORT_POLL_BUDGET_SEC = 180
_RESUME_POLL_BUDGET_SEC = 60


def _process_fba_report(conn, content: str) -> dict:
    ship_dates = parse_earliest_ship_dates(content)
    updated = store.set_actual_ship_dates(conn, ship_dates)
    return {"parsed": len(ship_dates), "updated": updated}


def sync_ship_dates(conn, from_date: str, to_date: str) -> dict:
    """Request + process the FBA shipments report for [from_date, to_date]
    (Pacific 'YYYY-MM-DD'). A chunk whose report isn't ready within
    _REPORT_POLL_BUDGET_SEC is deferred to resume_fba_reports() instead of
    blocking this invocation for the full 840s; later chunks stop too, and
    this run reports 'partial'.

    Single-flight via REPORTS_API_LOCK -- shared with amazonFetch's
    sync_via_report, a different Lambda function, because createReport has
    one global quota across every report type."""
    if not store.try_acquire_lock(conn, store.REPORTS_API_LOCK):
        logger.info("sync_ship_dates skipped -- another Reports API sync is already running")
        return {"status": "skipped", "reason": "locked"}
    try:
        run_id = store.start_sync_log(conn, "fba", from_date, to_date)
        try:
            totals = {"parsed": 0, "updated": 0}
            deferred = False
            for chunk_from, chunk_to in date_chunks(from_date, to_date, max_days=30):
                report_id = reports_api.create_report(
                    reports_api.FBA_SHIPMENTS_REPORT_TYPE,
                    constants.MARKETPLACE_ID,
                    pacific_day_to_utc(chunk_from),
                    pacific_day_to_utc(chunk_to, end_of_day=True),
                )
                logger.info("FBA report %s requested for %s..%s", report_id, chunk_from, chunk_to)
                try:
                    doc_id = reports_api.poll_report(report_id, max_wait=_REPORT_POLL_BUDGET_SEC)
                except TimeoutError:
                    store.create_pending_report(
                        conn, "fba_report", report_id, {"chunk_from": chunk_from, "chunk_to": chunk_to}
                    )
                    logger.warning(
                        "FBA report %s still processing after %ss -- deferred to resume pass",
                        report_id, _REPORT_POLL_BUDGET_SEC,
                    )
                    deferred = True
                    break

                content = reports_api.download_report(doc_id)
                result = _process_fba_report(conn, content)
                totals["parsed"] += result["parsed"]
                totals["updated"] += result["updated"]

            status = "partial" if deferred else "success"
            store.finish_sync_log(conn, run_id, status, totals)
            return {"status": status, **totals}
        except SpApiThrottledError as e:
            logger.warning("sync_ship_dates throttled: %s", e)
            store.finish_sync_log(conn, run_id, "throttled", error=str(e))
            return {"status": "throttled", "detail": str(e)}
        except Exception as e:
            store.finish_sync_log(conn, run_id, "error", error=str(e))
            raise
    finally:
        store.release_lock(conn, store.REPORTS_API_LOCK)


def resume_fba_reports(conn, poll_budget: int = _RESUME_POLL_BUDGET_SEC) -> dict:
    """Finish any FBA-report jobs sync_ship_dates deferred because Amazon's
    queue wasn't done within the poll budget. Same pattern as
    fetch_sync.resume_orders_reports -- never re-requests a pending
    report, just checks whether it's ready yet."""
    if not store.try_acquire_lock(conn, store.REPORTS_API_LOCK):
        return {"status": "skipped", "reason": "locked"}
    try:
        pending = store.list_pending_reports(conn, "fba_report")
        if not pending:
            return {"status": "success", "resumed": 0, "still_pending": 0}

        resumed, still_pending = 0, 0
        for row in pending:
            try:
                doc_id = reports_api.poll_report(row["report_id"], max_wait=poll_budget)
            except TimeoutError:
                still_pending += 1
                continue
            content = reports_api.download_report(doc_id)
            _process_fba_report(conn, content)
            store.finish_pending_report(conn, row["id"], "done")
            resumed += 1

        return {"status": "success", "resumed": resumed, "still_pending": still_pending}
    except SpApiThrottledError as e:
        logger.warning("resume_fba_reports throttled: %s", e)
        return {"status": "throttled", "detail": str(e)}
    finally:
        store.release_lock(conn, store.REPORTS_API_LOCK)
