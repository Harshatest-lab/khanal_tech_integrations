"""One-off: cancel + repost AR Invoices that posted under the
pre-2026-09-01 zero_discount_floor_channels behavior (a fully-discounted
BOGO line kept its gross UnitPrice with no discount applied at all,
overstating the invoice by the free item's full value -- see
sap_payload.py's floor-guard fix). Each correction is a real, live SAP
action (Invoices/Cancel); this is deliberately one order at a time,
dry-run by default, with a fresh live-SAP re-check immediately before
cancelling anything -- so a row already corrected (or never actually
broken) is never touched twice, no matter how stale the caller's own
candidate list is.
"""

from __future__ import annotations

import logging

from unicommerce import sap_client, store

logger = logging.getLogger(__name__)


def correct_one(conn, uniware_id: str, doc_entry: int | str, apply: bool = False) -> dict:
    live = sap_client.get_invoice_by_doc_entry(int(doc_entry))
    if str(live.get("Cancelled")) != "tNO":
        return {"uniware_id": uniware_id, "doc_entry": doc_entry, "status": "skipped_already_cancelled"}

    lines = live.get("DocumentLines") or []
    already_discounted = any(float(line.get("DiscountPercent") or 0) > 0 for line in lines)
    if already_discounted:
        return {"uniware_id": uniware_id, "doc_entry": doc_entry, "status": "skipped_already_fixed"}

    if not apply:
        return {
            "uniware_id": uniware_id,
            "doc_entry": doc_entry,
            "status": "dry_run_would_correct",
            "doc_total_before": live.get("DocTotal"),
        }

    sap_client.cancel_invoice(int(doc_entry))
    reset_ok = store.reset_posted_order_for_repost(conn, uniware_id, doc_entry)
    if not reset_ok:
        logger.error(
            "dogsee_bogo_correction: SAP invoice %s cancelled but local reset for %s did NOT match "
            "expected doc_entry -- order needs manual investigation, it will NOT auto-repost",
            doc_entry, uniware_id,
        )
        return {"uniware_id": uniware_id, "doc_entry": doc_entry, "status": "cancelled_but_reset_failed"}
    return {"uniware_id": uniware_id, "doc_entry": doc_entry, "status": "cancelled_and_reset"}


def run(conn, corrections: list[dict], apply: bool = False) -> dict:
    results = [correct_one(conn, c["uniware_id"], c["doc_entry"], apply) for c in corrections]
    return {"apply": apply, "results": results}
