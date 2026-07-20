"""Post AR Credit Notes for Amazon refund events.

Flow per event (refund_id = "{order_id}-{PostedDate}" = SAP NumAtCard):
  1. Local cn_log duplicate guard, then SAP duplicate guard by NumAtCard
     (CancelStatus csNo). A failed SAP check aborts the event — never
     treated as "not found".
  2. Cutoff guard: the ORDER's ship/purchase date must be on/after the
     posting cutoff (pre-cutoff refund revenue was booked manually by
     Finance; posting a CN for it would double-count).
  3. Order must be staged with a posted invoice; the stored DocEntry is
     re-fetched from SAP and its NumAtCard verified against the order id
     (stale-pointer guard) and CancelStatus verified live.
  4. Build (WithoutInventoryMovement, shipping reversal) + post + log.

Window guidance: Finances PostedDate lags the MTR refund date by up to
2 weeks — schedule with a wide window (e.g. last 45 days); duplicate
guards make re-processing free.
"""

from __future__ import annotations

import logging

from amazon_us import constants, finances_api, sap_client, store
from amazon_us.credit_note_payload import build_credit_note_payload
from amazon_us.hana_access import resolve_acct_code

logger = logging.getLogger(__name__)


def post_refunds(conn, posted_after: str, posted_before: str | None = None, dry_run: bool = False) -> dict:
    """posted_after/posted_before: UTC ISO 8601."""
    run_id = store.start_sync_log(conn, "cn", posted_after, posted_before)
    try:
        events = finances_api.list_refund_events(posted_after, posted_before)
        logger.info("%s refund events (dry_run=%s)", len(events), dry_run)
        mapping = store.get_sku_mapping(conn)

        counts = {"total": len(events), "posted": 0, "skipped": 0, "failed": 0}
        for event in events:
            try:
                outcome = _process_event(conn, event, mapping, dry_run)
                counts[outcome] += 1
            except Exception as e:
                counts["failed"] += 1
                if not dry_run:
                    store.upsert_cn_log(
                        conn, event.refund_id, event.amazon_order_id, event.posted_date,
                        "failed", error=str(e),
                    )
                logger.error("FAIL %s: %s", event.refund_id, e)

        store.finish_sync_log(conn, run_id, "success", counts)
        return {"status": "dry_run" if dry_run else "done", **counts}
    except Exception as e:
        store.finish_sync_log(conn, run_id, "error", error=str(e))
        raise


def _process_event(conn, event: finances_api.RefundEvent, mapping: dict[str, str], dry_run: bool) -> str:
    """Returns 'posted' | 'skipped'. Raises on real failures."""
    rid = event.refund_id

    local = store.get_cn_log(conn, rid)
    if local and local["status"] == "posted":
        return "skipped"

    existing = sap_client.find_credit_note_by_ref(rid)
    if existing:
        if not dry_run:
            store.upsert_cn_log(
                conn, rid, event.amazon_order_id, event.posted_date, "posted",
                doc_entry=existing["doc_entry"], doc_num=existing["doc_num"],
                skip_reason="already in SAP",
            )
        return "skipped"

    order = store.get_order(conn, event.amazon_order_id)
    if order is None or not order.get("sap_doc_entry"):
        _skip(conn, event, "order not staged or has no posted invoice", dry_run)
        return "skipped"

    order_date = str(order.get("actual_ship_date") or order.get("purchase_date") or "")[:10]
    if order_date and order_date < constants.POSTING_CUTOFF_DATE:
        _skip(conn, event, f"pre-cutoff order ({order_date} < {constants.POSTING_CUTOFF_DATE})", dry_run)
        return "skipped"

    # Stale-pointer guard: re-fetch the invoice and prove ownership before
    # base-linking. A DocEntry from a repointed/rebuilt DB can belong to a
    # completely unrelated document.
    invoice = sap_client.get_invoice_doc(order["sap_doc_entry"])
    if (invoice.get("NumAtCard") or "").strip() != event.amazon_order_id:
        _skip(
            conn, event,
            f"stale invoice pointer: DocEntry={order['sap_doc_entry']} "
            f"belongs to NumAtCard={invoice.get('NumAtCard')!r}",
            dry_run,
        )
        return "skipped"
    if (invoice.get("CancelStatus") or "csNo") != "csNo":
        _skip(conn, event, f"base invoice DocEntry={order['sap_doc_entry']} is cancelled", dry_run)
        return "skipped"

    payload, line_skips = build_credit_note_payload(
        order, event, mapping, order["sap_doc_entry"], invoice.get("DocumentLines") or [], resolve_acct_code
    )
    if payload is None:
        _skip(conn, event, f"no postable lines: {line_skips}", dry_run)
        return "skipped"

    if dry_run:
        logger.info("dry-run would post CN %s (%s lines)", rid, len(payload["DocumentLines"]))
        return "posted"

    result = sap_client.post_credit_note(payload)
    store.upsert_cn_log(
        conn, rid, event.amazon_order_id, event.posted_date, "posted",
        doc_entry=result["doc_entry"], doc_num=result["doc_num"],
        base_doc_entry=order["sap_doc_entry"], payload=payload, response=result["response"],
    )
    logger.info("OK %s -> DocEntry=%s DocNum=%s", rid, result["doc_entry"], result["doc_num"])
    return "posted"


def _skip(conn, event: finances_api.RefundEvent, reason: str, dry_run: bool) -> None:
    logger.warning("SKIP %s: %s", event.refund_id, reason)
    if not dry_run:
        store.upsert_cn_log(
            conn, event.refund_id, event.amazon_order_id, event.posted_date,
            "skipped", skip_reason=reason[:500],
        )
