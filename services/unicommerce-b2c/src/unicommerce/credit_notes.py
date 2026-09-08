"""Post staged Unicommerce returns to SAP B1 as Credit Notes.

Ported from khanal_tech_integrations/unicommerce/workers/credit_note.py.
frappe.get_doc/frappe.db.set_value become explicit store.py calls;
frappe.publish_realtime progress events are dropped (no equivalent
consumer in Lambda -- CloudWatch logs serve the same purpose here).

Unlike the Frappe source (a plain frappe.db.set_value with no WHERE-status
guard before posting), this port adds an atomic pending/failed -> posting
claim (store.claim_credit_note) before _post_credit_note runs -- the same
double-submission race the invoice pipeline already closed
(khanal_tech_integrations PR #1301-#1304), reproduced here rather than
carried over unfixed since this store layer is being written fresh.
"""

from __future__ import annotations

import logging

from unicommerce import constants, returns, sap_client, sap_payload, store
from unicommerce.mappers import normalize_text
from unicommerce.settings import get_settings

logger = logging.getLogger(__name__)

_STUCK_STATUSES = frozenset({"batch_hold", "failed", "dead"})
_CANCELLED_ORDER_STATUSES = frozenset({"CANCELLED", "CANCEL", "CANCELLATION_INITIATED"})


def _sum_lines_unit_price(lines: list) -> float:
    total = 0.0
    for line in lines or []:
        try:
            total += float(line.get("UnitPrice") or 0) * float(line.get("Quantity") or 0)
        except Exception:
            pass
    return round(total, 2)


def _sum_lines_tax(lines: list) -> float:
    total = 0.0
    for line in lines or []:
        for key in ("TaxTotal", "TaxAmount", "LineTaxTotal", "LineTaxAmount"):
            if key in line:
                try:
                    total += float(line.get(key) or 0)
                except Exception:
                    pass
                break
    return round(total, 2)


def _line_batch_total(line: dict) -> float:
    total = 0.0
    for b in line.get("BatchNumbers") or []:
        try:
            total += float((b or {}).get("Quantity") or 0)
        except Exception:
            pass
    return round(total, 3)


def _line_mismatches(payload_lines: list, sap_lines: list) -> list[str]:
    mismatches = []
    for idx, pl in enumerate(payload_lines):
        if idx >= len(sap_lines):
            mismatches.append(f"line {idx}: missing in SAP credit note")
            continue
        sl = sap_lines[idx] or {}
        p_item = str(pl.get("ItemCode") or "").strip()
        s_item = str(sl.get("ItemCode") or "").strip()
        if p_item and s_item and p_item != s_item:
            mismatches.append(f"line {idx}: item mismatch payload={p_item} sap={s_item}")
        p_batch_qty = _line_batch_total(pl)
        s_batch_qty = _line_batch_total(sl)
        if abs(p_batch_qty - s_batch_qty) > 0.01:
            mismatches.append(f"line {idx}: batch qty mismatch payload={p_batch_qty} sap={s_batch_qty}")
    return mismatches


def _prepare_credit_note(conn, cn: dict, settings) -> bool | str:
    """Returns True (ready to post), False (deferred -- parent not posted
    yet or some return item couldn't be mapped to a SAP invoice line),
    "stuck" (parent order is batch_hold/failed/dead -- post the parent
    first), or "dead" (parent was cancelled before it ever reached SAP,
    so no base invoice line will ever exist to reference)."""
    parent = store.get_order(conn, cn["uc_order_id"]) if cn.get("uc_order_id") else None
    if not parent:
        store.set_cn_prepared(
            conn, cn["uc_return_id"], "pending",
            last_error="Parent order staging row is not available yet",
        )
        return False

    if str(parent.get("status") or "").upper() in _CANCELLED_ORDER_STATUSES and not parent.get("sap_ar_invoice_docentry"):
        store.set_cn_prepared(
            conn, cn["uc_return_id"], "dead",
            uc_order_id=parent["uniware_id"], parent_sap_doc_entry="",
            last_error=(
                f"Parent order {parent['uniware_id']} was cancelled before posting to SAP -- "
                "credit note not possible"
            ),
        )
        return "dead"

    if parent.get("pipeline_status") in _STUCK_STATUSES:
        store.set_cn_prepared(
            conn, cn["uc_return_id"], "pending",
            uc_order_id=parent["uniware_id"], parent_sap_doc_entry="",
            last_error=f"Parent order {parent['uniware_id']} is stuck ({parent['pipeline_status']}) -- post parent first",
        )
        return "stuck"

    if parent.get("pipeline_status") != "posted" or not parent.get("sap_ar_invoice_docentry"):
        store.set_cn_prepared(
            conn, cn["uc_return_id"], "pending",
            uc_order_id=parent["uniware_id"], parent_sap_doc_entry="",
            last_error="Parent order is not posted to SAP yet",
        )
        return False

    items = cn.get("items") or []
    fallback_warehouse = normalize_text((items[0] or {}).get("warehouseCode")) if items else ""
    prepared = returns.prepare_credit_note_items(parent, items, fallback_warehouse=fallback_warehouse)

    if prepared["unresolved_count"]:
        store.set_cn_prepared(
            conn, cn["uc_return_id"], "pending",
            uc_order_id=parent["uniware_id"], parent_sap_doc_entry=parent["sap_ar_invoice_docentry"],
            items=prepared["items"],
            last_error=f"Awaiting SAP base-line mapping for {prepared['unresolved_count']} return item(s)",
        )
        return False

    store.set_cn_prepared(
        conn, cn["uc_return_id"], "pending",
        uc_order_id=parent["uniware_id"], parent_sap_doc_entry=parent["sap_ar_invoice_docentry"],
        items=prepared["items"], last_error="",
    )
    return True


def _post_credit_note(conn, uc_return_id: str, settings) -> None:
    cn = store.get_cn(conn, uc_return_id)
    parent = store.get_order(conn, cn["uc_order_id"]) if cn and cn.get("uc_order_id") else None

    # Idempotency guard: SAP may already have a credit note for this return
    # if a previous attempt posted successfully but timed out before we
    # could record the DocEntry.
    try:
        existing = sap_client.find_credit_note_by_return_id(
            uc_return_id, base_doc_entry=parent.get("sap_ar_invoice_docentry") if parent else None
        )
        if existing:
            store.mark_cn_posted(
                conn, uc_return_id, str(existing["doc_entry"]), str(existing["doc_num"]),
                cn.get("uc_order_id") if cn else None,
            )
            logger.info("Credit note already in SAP for return %s -- marked posted (DocEntry=%s)", uc_return_id, existing["doc_entry"])
            return
    except sap_client.SapLookupError as check_err:
        # Could not prove the CN is absent from SAP -- do NOT post blind.
        # Unlike the invoice path there's no dedicated 'verify' polling
        # here; re-raise so the caller's mark_cn_failed retry path picks
        # it up and re-runs this same check next cycle.
        logger.error("Credit note pre-check failed for %s; deferred to retry: %s", uc_return_id, check_err)
        raise

    payload = sap_payload.credit_note_payload(cn, parent or {}, settings)
    try:
        result = sap_client.post_credit_note_with_series_fallback(payload)
    except sap_client.SapPostError as e:
        err = str(e)
        if "timeout" in err.lower() or "connection" in err.lower():
            # SAP may have processed it -- on the next retry the
            # idempotency guard above will detect the existing doc.
            store.mark_cn_failed(conn, uc_return_id, f"[TIMEOUT - may be posted in SAP] {err}", settings.max_retry_attempts)
            return
        raise

    doc_entry = str(result.get("doc_entry") or "")
    doc_num = str(result.get("doc_num") or "")
    response_body = result.get("response") or {}

    # Verify the POST response actually describes THIS return before
    # trusting/recording its DocEntry -- same defense as post_invoices.py's
    # _post_order. Unlike invoices, this can't key off U_UCReturnID: ORIN
    # (SAP B1's A/R Credit Memo table) has no such UDF on this install at
    # all (see sap_client.find_credit_note_by_return_id's docstring), so
    # that field is always absent from the response regardless of whether
    # the post succeeded -- confirmed live 2026-09-02 this was failing
    # every real (non-idempotency-guard) credit note post, silently
    # orphaning genuinely-correct SAP documents (matches the known
    # DocEntry 12717-12732 symptom from 2026-08-28). Comments is the field
    # that's actually set (credit_note_payload: "Return for UC Order:
    # {uc_order_id}") and reliably echoed back, same as the invoice-side
    # check -- it only carries the parent order id, not the return id, so
    # this confirms the response describes the right order, mirroring
    # exactly what the invoice check already relies on.
    # Second, stronger signal on top of Comments: DocumentLines[*].U_Order
    # is SAP's own copy-from-base-document-line value (not something our
    # own credit_note_payload sets -- confirmed it's absent from what we
    # submit), so a line whose U_Order is populated but doesn't match our
    # order id is real evidence of a cross-attributed response, not a
    # parsing fluke. Only lines that actually carry a value are checked --
    # the field isn't guaranteed present (it depends on SAP's own field-
    # copy config, not our code), so its absence isn't itself suspicious.
    def _line_order_mismatch(lines: list) -> str | None:
        for line in lines or []:
            line_order = str((line or {}).get("U_Order") or "").strip()
            if line_order and line_order != str(uc_order_id):
                return line_order
        return None

    uc_order_id = cn.get("uc_order_id") if cn else None
    comments_ok = bool(uc_order_id) and str(uc_order_id) in str(response_body.get("Comments") or "")
    mismatched_line_order = _line_order_mismatch(response_body.get("DocumentLines")) if comments_ok else None

    if not comments_ok or mismatched_line_order:
        store.mark_cn_failed(
            conn, uc_return_id,
            f"Post response did not describe this return (DocEntry={doc_entry}, "
            f"Comments={response_body.get('Comments')!r}, "
            f"line U_Order={mismatched_line_order!r} expected {uc_order_id!r}) -- not trusting it",
            settings.max_retry_attempts,
        )
        logger.error(
            "_post_credit_note(%s): POST response mismatch, DocEntry=%s Comments=%r line U_Order=%r -- "
            "treating as failed, not recording",
            uc_return_id, doc_entry, response_body.get("Comments"), mismatched_line_order,
        )
        return

    store.mark_cn_posted(conn, uc_return_id, doc_entry, doc_num, cn.get("uc_order_id") if cn else None)

    try:
        # SAP Service Layer POST returns the full document -- reuse it
        # directly, no second GET needed for reconciliation.
        sap_doc = result.get("response") or {}
        payload_lines = payload.get("DocumentLines") or []
        sap_lines = sap_doc.get("DocumentLines") or []

        if cn and cn.get("uc_order_id"):
            actual_unit = _sum_lines_unit_price(sap_lines) if sap_lines else 0.0
            actual_tax = float(sap_doc.get("DocTaxAmount") or 0) or _sum_lines_tax(sap_lines)
            store.update_cn_reconciliation(conn, cn["uc_order_id"], actual_unit, actual_tax)

        mismatches = _line_mismatches(payload_lines, sap_lines)
        if mismatches:
            store.set_cn_last_error(conn, uc_return_id, "Posted with reconciliation warnings: " + "; ".join(mismatches[:5]))
        else:
            store.set_cn_last_error(conn, uc_return_id, "Posted and reconciled with SAP")
    except Exception as rec_err:
        logger.warning("Credit note reconciliation check failed for %s: %s", uc_return_id, rec_err, exc_info=True)


def post_credit_notes(
    conn,
    limit: int = 50,
    start_date: str | None = None,
    end_date: str | None = None,
    channel_name: str | None = None,
    sync_mode: str = "post_credit_notes",
) -> dict:
    """Process pending/failed credit note staging rows into SAP Credit
    Notes. start_date/end_date restrict to CNs whose parent UC sale
    order's order_date falls in the window (inclusive)."""
    settings = get_settings(force_reload=True)
    run_id = store.start_sync_log(conn, sync_mode, start_date, end_date)

    series_ok, series_msg = sap_client.validate_active_series(settings)
    if not series_ok:
        logger.error("post_credit_notes series guard ABORTED: %s", series_msg)
        result = {
            "processed": 0, "posted": 0, "deferred": 0, "failed": 0, "dead": 0,
            "skipped_parent_stuck": 0, "skipped_claimed": 0, "abort_reason": series_msg,
        }
        store.finish_sync_log(conn, run_id, "aborted", result)
        return result

    try:
        rows = store.get_pending_credit_notes(
            conn, limit, constants.POSTING_CUTOFF_DATE, start_date=start_date, end_date=end_date, channel_name=channel_name
        )

        processed = posted = deferred = failed = dead = 0
        skipped_parent_stuck = skipped_claimed = 0
        for row in rows:
            uc_return_id = row["uc_return_id"]
            processed += 1

            # SAP DocEntry already written -- this record is posted regardless of status.
            if row.get("sap_cn_docentry"):
                store.mark_cn_posted(conn, uc_return_id, row["sap_cn_docentry"], "", row.get("uc_order_id"))
                posted += 1
                continue

            try:
                cn = store.get_cn(conn, uc_return_id)
                prepare_result = _prepare_credit_note(conn, cn, settings)
                if prepare_result == "stuck":
                    skipped_parent_stuck += 1
                    continue
                if prepare_result == "dead":
                    dead += 1
                    continue
                if not prepare_result:
                    deferred += 1
                    continue

                if not store.claim_credit_note(conn, uc_return_id):
                    skipped_claimed += 1
                    continue

                _post_credit_note(conn, uc_return_id, settings)
                posted += 1
            except Exception as e:
                transitioned = store.mark_cn_failed(conn, uc_return_id, str(e), settings.max_retry_attempts)
                if transitioned == "dead":
                    dead += 1
                else:
                    failed += 1
                logger.error("Credit note processor failed for %s: %s", uc_return_id, e, exc_info=True)

        result = {
            "processed": processed,
            "posted": posted,
            "deferred": deferred,
            "failed": failed,
            "dead": dead,
            "skipped_parent_stuck": skipped_parent_stuck,
            "skipped_claimed": skipped_claimed,
        }
        store.finish_sync_log(conn, run_id, "success", result)
        return result
    except Exception as e:
        store.finish_sync_log(conn, run_id, "error", error=str(e))
        raise


def post_one_credit_note(conn, uc_return_id: str) -> dict:
    """Manual retry path, mirroring post_invoices.post_one_order: prepare
    + claim + post through the same atomic path the scheduler uses,
    independent of the pending/failed batch's limit and ordering."""
    settings = get_settings(force_reload=True)

    series_ok, series_msg = sap_client.validate_active_series(settings)
    if not series_ok:
        logger.error("post_one_credit_note(%s) series guard ABORTED: %s", uc_return_id, series_msg)
        return {"status": "series_guard_failed", "uc_return_id": uc_return_id, "error": series_msg}

    cn = store.get_cn(conn, uc_return_id)
    if cn is None:
        return {"status": "not_found", "uc_return_id": uc_return_id}

    cn_doc_date = str(cn.get("return_date") or "")
    if cn_doc_date and store.is_period_locked(conn, cn_doc_date):
        return {"status": "period_locked", "uc_return_id": uc_return_id, "return_date": cn_doc_date}

    prepare_result = _prepare_credit_note(conn, cn, settings)
    if prepare_result == "stuck":
        return {"status": "stuck", "uc_return_id": uc_return_id}
    if prepare_result == "dead":
        return {"status": "dead", "uc_return_id": uc_return_id}
    if not prepare_result:
        return {"status": "deferred", "uc_return_id": uc_return_id}

    if not store.claim_credit_note(conn, uc_return_id):
        return {"status": "already_claimed", "uc_return_id": uc_return_id}

    try:
        _post_credit_note(conn, uc_return_id, settings)
        return {"status": "posted", "uc_return_id": uc_return_id}
    except Exception as e:
        transitioned = store.mark_cn_failed(conn, uc_return_id, str(e), settings.max_retry_attempts)
        return {"status": transitioned, "uc_return_id": uc_return_id, "error": str(e)[:500]}
