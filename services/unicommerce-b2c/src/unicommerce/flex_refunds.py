"""Post AR Credit Notes for Amazon Flex refund events (SP-API Finances API).

Ported from amazon-us-b2c's post_credit_notes.py -- same SP-API operation,
same SAP install, same proven flow shape:
  1. Atomic local claim (unicommerce_flex_cn_log status -> 'posting',
     reclaimable after 30min if a worker crashed mid-flight), then a SAP
     duplicate guard by NumAtCard (CancelStatus csNo). A failed SAP check
     aborts the event -- never treated as "not found".
  2. The Amazon order must have at least one posted AMAZON_FLEX invoice
     staged locally; a split order (rare -- see
     store.get_posted_flex_orders_by_amazon_order_id) is resolved to
     whichever shipment-invoice's own order_json actually contains the
     refunded seller SKUs.
  3. Build (CFG bundles + shipping-only adjustments skipped in v1, see
     flex_credit_note_payload.py) + post + log.
  4. An event whose refund_id was already posted takes the amendment path:
     Amazon can grow a refund event's item_adjustments after the base CN
     posted (same incident class amazon-us-b2c hardened against) -- sum
     every live CN already posted under this refund_id and, if the
     event's current total exceeds it, post one more CN for exactly the
     remainder under its own self-idempotent NumAtCard
     ("{refund_id}-AMEND{n}").

Window guidance: Finances PostedDate lags the actual refund by up to 1-2
weeks -- schedule with a wide window (e.g. last 45 days); duplicate
guards make re-processing free.
"""

from __future__ import annotations

import logging

from unicommerce import amazon_finances_api as finances_api
from unicommerce import amazon_flex_items as flex_items
from unicommerce import sap_client, store
from unicommerce.flex_credit_note_payload import build_flex_credit_note_payload
from unicommerce.settings import get_settings

logger = logging.getLogger(__name__)


def _pick_parent(candidates: list[dict], event: finances_api.RefundEvent) -> dict | None:
    """Usually one posted Flex invoice per Amazon order. When an order
    split across shipments produced more than one, pick whichever
    candidate's own order_json shares the most seller SKUs with this
    event's adjustments -- best-effort, logged, not guessed silently."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    event_skus = {adj.seller_sku for adj in event.item_adjustments}
    best, best_overlap = None, -1
    for c in candidates:
        order_json = c.get("order_json") or {}
        c_skus = {str(i.get("sellerSkuCode") or "").strip() for i in order_json.get("saleOrderItems") or []}
        overlap = len(event_skus & c_skus)
        if overlap > best_overlap:
            best, best_overlap = c, overlap
    if best_overlap <= 0:
        logger.warning(
            "flex_refunds: order %s has %s posted shipment-invoices, none share a seller SKU "
            "with refund event %s -- defaulting to %s",
            event.amazon_order_id, len(candidates), event.refund_id, best.get("uniware_id") if best else None,
        )
    return best


def post_flex_refunds(conn, posted_after: str, posted_before: str | None = None, dry_run: bool = False) -> dict:
    """posted_after/posted_before: UTC ISO 8601. Requires
    unicommerce_flex_cn_log to already exist -- this app's DB role has no
    CREATE TABLE grant (least-privilege, RDS hardening 2026-08-18), so the
    table is a one-off manual DDL step, not self-provisioned at runtime.
    See scripts/create_flex_cn_log_table.sql."""
    settings = get_settings(force_reload=True)
    if not settings.credit_note_series:
        result = {"total": 0, "posted": 0, "skipped": 0, "failed": 0, "abort_reason": "credit_note_series is not configured"}
        logger.error("post_flex_refunds ABORTED: %s", result["abort_reason"])
        return result

    run_id = store.start_sync_log(conn, "flex_refunds", posted_after, posted_before)
    try:
        events = finances_api.list_refund_events(posted_after, posted_before)
        logger.info("flex_refunds: %s refund events (dry_run=%s)", len(events), dry_run)
        sku_map = flex_items.build_sku_map(conn)

        counts = {"total": len(events), "posted": 0, "skipped": 0, "failed": 0}
        for event in events:
            try:
                outcome = _process_event(conn, event, sku_map, settings, dry_run)
                counts[outcome] += 1
            except Exception as e:
                counts["failed"] += 1
                if not dry_run:
                    status = "failed_permanent" if sap_client.is_base_document_closed(e) else "failed"
                    store.upsert_flex_cn_log(
                        conn, event.refund_id, event.amazon_order_id, event.posted_date, status, error=str(e),
                    )
                logger.error("flex_refunds FAIL %s: %s", event.refund_id, e, exc_info=True)

        store.finish_sync_log(conn, run_id, "success", counts)
        return {"status": "dry_run" if dry_run else "done", **counts}
    except Exception as e:
        store.finish_sync_log(conn, run_id, "error", error=str(e))
        raise


def _skip(conn, event: finances_api.RefundEvent, reason: str, dry_run: bool) -> None:
    logger.warning("flex_refunds SKIP %s: %s", event.refund_id, reason)
    if not dry_run:
        store.upsert_flex_cn_log(
            conn, event.refund_id, event.amazon_order_id, event.posted_date, "skipped", skip_reason=reason[:500],
        )


def _process_event(conn, event: finances_api.RefundEvent, sku_map: dict, settings, dry_run: bool) -> str:
    """Returns 'posted' | 'skipped'. Raises on real failures."""
    rid = event.refund_id

    local = store.get_flex_cn_log(conn, rid)
    if local and local["status"] == "posted":
        return _process_amendment(conn, event, sku_map, settings, dry_run)

    if not dry_run:
        if not store.claim_flex_cn(conn, rid, event.amazon_order_id, event.posted_date):
            return "skipped"

    existing = sap_client.find_credit_note_by_num_at_card(rid)
    if existing:
        if not dry_run:
            store.upsert_flex_cn_log(
                conn, rid, event.amazon_order_id, event.posted_date, "posted",
                doc_entry=str(existing["doc_entry"]), doc_num=str(existing["doc_num"]), skip_reason="already in SAP",
            )
        return _process_amendment(conn, event, sku_map, settings, dry_run)

    candidates = store.get_posted_flex_orders_by_amazon_order_id(conn, event.amazon_order_id)
    parent = _pick_parent(candidates, event)
    if parent is None:
        _skip(conn, event, "no posted AMAZON_FLEX invoice found for this order", dry_run)
        return "skipped"

    payload, line_skips = build_flex_credit_note_payload(parent, event, sku_map, settings)
    if payload is None:
        _skip(conn, event, f"no postable lines: {line_skips}", dry_run)
        return "skipped"

    if dry_run:
        logger.info(
            "flex_refunds dry-run would post CN %s (%s lines, parent=%s) skipped=%s",
            rid, len(payload["DocumentLines"]), parent.get("uniware_id"), line_skips,
        )
        return "posted"

    result = sap_client.post_credit_note(payload)
    store.upsert_flex_cn_log(
        conn, rid, event.amazon_order_id, event.posted_date, "posted",
        doc_entry=str(result["doc_entry"]), doc_num=str(result["doc_num"]),
        base_doc_entry=str(parent.get("sap_ar_invoice_docentry")), payload=payload, response=result["response"],
    )
    logger.info("flex_refunds OK %s -> DocEntry=%s DocNum=%s", rid, result["doc_entry"], result["doc_num"])
    return "posted"


def _process_amendment(conn, event: finances_api.RefundEvent, sku_map: dict, settings, dry_run: bool) -> str:
    """A refund_id we've already posted a CN for. Amazon can grow that same
    event's item_adjustments afterward -- the base NumAtCard is a fixed
    idempotency key, so nothing upstream ever looks again on its own. Sum
    every live CN already posted under this refund_id and, if the event's
    current total exceeds it, post one more CN for exactly the remainder,
    keyed off its own self-idempotent NumAtCard."""
    rid = event.refund_id
    posted_docs = sap_client.find_credit_notes_by_num_at_card_prefix(rid)
    if not posted_docs:
        logger.info("flex_refunds AMEND-SKIP %s: no live CNs found under this prefix", rid)
        return "skipped"

    already_qty_by_price: dict[float, float] = {}
    for doc in posted_docs:
        for line in doc.get("DocumentLines") or []:
            price_key = round(float(line.get("UnitPrice") or 0), 4)
            already_qty_by_price[price_key] = already_qty_by_price.get(price_key, 0.0) + float(line.get("Quantity") or 0)

    base_doc_entry = next(
        (int(l["BaseEntry"]) for d in posted_docs for l in (d.get("DocumentLines") or []) if l.get("BaseEntry")),
        None,
    )
    if base_doc_entry is None:
        logger.info("flex_refunds AMEND-SKIP %s: no BaseEntry on any posted CN line", rid)
        return "skipped"

    candidates = store.get_posted_flex_orders_by_amazon_order_id(conn, event.amazon_order_id)
    parent = next((c for c in candidates if str(c.get("sap_ar_invoice_docentry")) == str(base_doc_entry)), None) \
        or _pick_parent(candidates, event)
    if parent is None:
        logger.info("flex_refunds AMEND-SKIP %s: parent order not found locally", rid)
        return "skipped"

    payload, line_skips = build_flex_credit_note_payload(
        parent, event, sku_map, settings, already_posted={"qty_by_price": already_qty_by_price},
    )
    if payload is None:
        logger.info("flex_refunds AMEND-SKIP %s: fully covered by prior CN(s), line_skips=%s", rid, line_skips)
        return "skipped"

    amend_rid = f"{rid}-AMEND{len(posted_docs)}"
    payload["NumAtCard"] = amend_rid

    if dry_run:
        logger.info("flex_refunds dry-run would top up %s -> %s (%s lines)", rid, amend_rid, len(payload["DocumentLines"]))
        return "posted"

    if not store.claim_flex_cn(conn, amend_rid, event.amazon_order_id, event.posted_date):
        return "skipped"

    existing_amend = sap_client.find_credit_note_by_num_at_card(amend_rid)
    if existing_amend:
        store.upsert_flex_cn_log(
            conn, amend_rid, event.amazon_order_id, event.posted_date, "posted",
            doc_entry=str(existing_amend["doc_entry"]), doc_num=str(existing_amend["doc_num"]), skip_reason="already in SAP",
        )
        return "skipped"

    try:
        result = sap_client.post_credit_note(payload)
    except Exception as e:
        status = "failed_permanent" if sap_client.is_base_document_closed(e) else "failed"
        store.upsert_flex_cn_log(conn, amend_rid, event.amazon_order_id, event.posted_date, status, error=str(e))
        logger.error("flex_refunds FAIL top-up %s -> %s: %s", rid, amend_rid, e, exc_info=True)
        return "failed"

    store.upsert_flex_cn_log(
        conn, amend_rid, event.amazon_order_id, event.posted_date, "posted",
        doc_entry=str(result["doc_entry"]), doc_num=str(result["doc_num"]),
        base_doc_entry=str(base_doc_entry), payload=payload, response=result["response"],
    )
    logger.info("flex_refunds OK top-up %s -> %s DocEntry=%s DocNum=%s", rid, amend_rid, result["doc_entry"], result["doc_num"])
    return "posted"
