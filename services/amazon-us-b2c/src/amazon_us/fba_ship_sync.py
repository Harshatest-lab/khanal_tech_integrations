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


def sync_ship_dates(conn, from_date: str, to_date: str) -> dict:
    """Request + process the FBA shipments report for [from_date, to_date]
    (Pacific 'YYYY-MM-DD')."""
    run_id = store.start_sync_log(conn, "fba", from_date, to_date)
    try:
        totals = {"parsed": 0, "updated": 0}
        for chunk_from, chunk_to in date_chunks(from_date, to_date, max_days=30):
            report_id = reports_api.create_report(
                reports_api.FBA_SHIPMENTS_REPORT_TYPE,
                constants.MARKETPLACE_ID,
                pacific_day_to_utc(chunk_from),
                pacific_day_to_utc(chunk_to, end_of_day=True),
            )
            logger.info("FBA report %s requested for %s..%s", report_id, chunk_from, chunk_to)
            doc_id = reports_api.poll_report(report_id)
            content = reports_api.download_report(doc_id)

            ship_dates = parse_earliest_ship_dates(content)
            updated = store.set_actual_ship_dates(conn, ship_dates)
            totals["parsed"] += len(ship_dates)
            totals["updated"] += updated

        store.finish_sync_log(conn, run_id, "success", totals)
        return {"status": "success", **totals}
    except Exception as e:
        store.finish_sync_log(conn, run_id, "error", error=str(e))
        raise
