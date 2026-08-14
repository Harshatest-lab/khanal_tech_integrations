"""Post AR Credit Notes for Amazon refund events.

Flow per event (refund_id = "{order_id}-{PostedDate}" = SAP NumAtCard):
  1. Atomic local claim (cn_log status -> 'posting'; reclaimable after
     30min if a worker crashed mid-flight), then SAP duplicate guard by
     NumAtCard (CancelStatus csNo). A failed SAP check aborts the event —
     never treated as "not found".
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
from amazon_us.hana_access import get_bom_children, resolve_acct_code

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

    # A refund_id we've already posted needs the amendment path, not the
    # normal claim/post flow below — claim_cn's WHERE clause refuses to
    # reclaim a 'posted' row, so without this check an Amazon-side
    # amendment (event grows after we posted its base CN, same refund_id)
    # would silently never be looked at again.
    local = store.get_cn_log(conn, rid)
    if local and local["status"] == "posted":
        return _process_amendment(conn, event, mapping, dry_run)

    if not dry_run:
        # Atomic claim: closes the window where two overlapping invocations
        # (manual backfill overlapping the schedule, an ops re-invoke) could
        # both pass the SAP existence check below and both post a CN.
        if not store.claim_cn(conn, rid, event.amazon_order_id, event.posted_date):
            return "skipped"

    existing = sap_client.find_credit_note_by_ref(rid)
    if existing:
        if not dry_run:
            store.upsert_cn_log(
                conn, rid, event.amazon_order_id, event.posted_date, "posted",
                doc_entry=existing["doc_entry"], doc_num=existing["doc_num"],
                skip_reason="already in SAP",
            )
        return _process_amendment(conn, event, mapping, dry_run)

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
        order, event, mapping, order["sap_doc_entry"], invoice.get("DocumentLines") or [], resolve_acct_code,
        get_bom_children,
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


def _process_amendment(conn, event: finances_api.RefundEvent, mapping: dict[str, str], dry_run: bool) -> str:
    """A refund_id we've already posted a CN for. Amazon can grow that same
    event's item_adjustments afterward (confirmed 2026-08-12: SAP had 1 of
    2 refunded units on an order whose Finances API total later covered
    both, same refund_id the whole time) — the base NumAtCard is a fixed
    idempotency key, so nothing upstream ever looks again on its own. Sum
    every live CN already posted under this refund_id (base + prior
    top-ups) and, if the event's current total exceeds it, post one more
    CN for exactly the remainder, keyed off its own (self-idempotent)
    NumAtCard so this is safe to run over and over."""
    rid = event.refund_id
    logger.info("AMEND-CHECK %s: entering amendment path", rid)
    posted_docs = sap_client.find_credit_notes_by_ref_prefix(rid)
    if not posted_docs:
        # cn_log says posted but SAP has nothing live under this prefix
        # (e.g. the CN was cancelled by hand) — that's an ops decision,
        # not something to guess at here.
        logger.info("AMEND-SKIP %s: find_credit_notes_by_ref_prefix returned nothing", rid)
        return "skipped"
    logger.info("AMEND-CHECK %s: %s posted_docs found: %s", rid, len(posted_docs), [d.get("NumAtCard") for d in posted_docs])

    already_tax = already_shipping = 0.0
    already_qty_by_item: dict[str, float] = {}
    for doc in posted_docs:
        for line in doc.get("DocumentLines") or []:
            code = line.get("ItemCode")
            if code == constants.SAP_TAX_ITEM_CODE:
                already_tax += float(line.get("UnitPrice") or 0)
            elif code == constants.SAP_SHIPPING_ITEM_CODE:
                already_shipping += float(line.get("UnitPrice") or 0)
            else:
                already_qty_by_item[code] = already_qty_by_item.get(code, 0.0) + float(line.get("Quantity") or 0)
    logger.info(
        "AMEND-CHECK %s: already_tax=%s already_shipping=%s already_qty_by_item=%s",
        rid, already_tax, already_shipping, already_qty_by_item,
    )

    # base_doc_entry comes from the ALREADY-POSTED CN's own BaseEntry, not
    # order["sap_doc_entry"] -- confirmed for real 2026-08-12: all 3 of
    # that day's amendment cases had a genuinely posted SAP invoice, but
    # amazon_us_orders.sap_doc_entry had gone stale/null locally, which
    # would have silently blocked every one of them. The CN we already
    # posted already proved which invoice it reverses; trust that over a
    # local pointer that can drift for reasons unrelated to whether SAP
    # itself still has a valid, live invoice.
    base_doc_entry = next(
        (int(l["BaseEntry"]) for d in posted_docs for l in (d.get("DocumentLines") or []) if l.get("BaseEntry")),
        None,
    )
    if base_doc_entry is None:
        logger.info("AMEND-SKIP %s: no BaseEntry on any posted_docs line", rid)
        return "skipped"

    order = store.get_order(conn, event.amazon_order_id)
    if order is None:
        logger.info("AMEND-SKIP %s: order not found locally", rid)
        return "skipped"
    # No cutoff re-check here: posted_docs existing already proves this
    # order was posted by us (not booked manually by Finance) — the
    # cutoff guard exists to stop double-counting NEW pre-cutoff revenue,
    # not to block completing revenue we already recorded. Skipping it
    # here mattered for real: all 3 of the 2026-08-12 amendment cases
    # shipped 2026-06-25/26/29, just before the 2026-07-01 cutoff, and the
    # normal cutoff check silently ate every one of them.

    invoice = sap_client.get_invoice_doc(base_doc_entry)
    if (invoice.get("NumAtCard") or "").strip() != event.amazon_order_id:
        logger.info("AMEND-SKIP %s: stale invoice pointer NumAtCard=%r", rid, invoice.get("NumAtCard"))
        return "skipped"
    if (invoice.get("CancelStatus") or "csNo") != "csNo":
        logger.info("AMEND-SKIP %s: invoice cancelled", rid)
        return "skipped"

    payload, line_skips = build_credit_note_payload(
        order, event, mapping, base_doc_entry, invoice.get("DocumentLines") or [], resolve_acct_code,
        get_bom_children,
        already_posted={"tax": already_tax, "shipping": already_shipping, "qty_by_item": already_qty_by_item},
    )
    if payload is None:
        logger.info("AMEND-SKIP %s: build_credit_note_payload returned None, line_skips=%s", rid, line_skips)
        # Already fully covered by prior CN(s) under this refund_id —
        # rounding dust, not a real amendment.
        return "skipped"

    amend_rid = f"{rid}-AMEND{len(posted_docs)}"
    payload["NumAtCard"] = amend_rid

    if dry_run:
        logger.info("dry-run would top up %s -> %s (%s lines)", rid, amend_rid, len(payload["DocumentLines"]))
        return "posted"

    if not store.claim_cn(conn, amend_rid, event.amazon_order_id, event.posted_date):
        return "skipped"

    existing_amend = sap_client.find_credit_note_by_ref(amend_rid)
    if existing_amend:
        store.upsert_cn_log(
            conn, amend_rid, event.amazon_order_id, event.posted_date, "posted",
            doc_entry=existing_amend["doc_entry"], doc_num=existing_amend["doc_num"],
            skip_reason="already in SAP",
        )
        return "skipped"

    try:
        result = sap_client.post_credit_note(payload)
    except Exception as e:
        # claim_cn above already moved amend_rid's own row to 'posting' --
        # the caller's generic except block logs failures against
        # event.refund_id (the BASE rid), a different row that's usually
        # already 'posted', so it never touches this one. Left as-is,
        # amend_rid stays stuck at 'posting' forever: nothing ever revisits
        # it (a later retry recomputes the same amend_rid only while this
        # top-up is still owed, but build_credit_note_payload may start
        # returning None for it -- e.g. once a rounding-dust fix lands --
        # which returns "skipped" above without ever reaching claim_cn
        # again). Confirmed for real 2026-08-14: 3 rows orphaned this way,
        # one from the exact dust-fix scenario just described. Record the
        # failure against the row that's actually claimed.
        store.upsert_cn_log(
            conn, amend_rid, event.amazon_order_id, event.posted_date, "failed", error=str(e),
        )
        logger.error("FAIL top-up %s -> %s: %s", rid, amend_rid, e)
        return "failed"

    store.upsert_cn_log(
        conn, amend_rid, event.amazon_order_id, event.posted_date, "posted",
        doc_entry=result["doc_entry"], doc_num=result["doc_num"],
        base_doc_entry=base_doc_entry, payload=payload, response=result["response"],
    )
    logger.info("OK top-up %s -> %s DocEntry=%s DocNum=%s", rid, amend_rid, result["doc_entry"], result["doc_num"])
    return "posted"


def _skip(conn, event: finances_api.RefundEvent, reason: str, dry_run: bool) -> None:
    logger.warning("SKIP %s: %s", event.refund_id, reason)
    if not dry_run:
        store.upsert_cn_log(
            conn, event.refund_id, event.amazon_order_id, event.posted_date,
            "skipped", skip_reason=reason[:500],
        )
