"""Prepare and post SAP A/R Credit Notes for Walmart returns.

Prerequisite: original AR Invoice must already be posted in SAP
(NumAtCard == customerOrderId). Payload is rebuilt fresh every run from
return raw_json + live SAP invoice — never reused from a saved payload.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import psycopg

from walmart.credentials import DATABASE_URL
from walmart.credit_note_payload import (
    BLOCKED_CN_STATUSES,
    REFUND_COMPLETED,
    CreditNoteBuildError,
    build_credit_note_payload,
    can_post_credit_note,
)
from walmart.sap_client import (
    SapLookupError,
    SapPostError,
    cancel_credit_note,
    find_credit_note_by_ref,
    find_invoice_by_customer_order_id,
    get_invoice_doc,
    post_credit_note,
)
from walmart.sap_item_lookup import resolve_item_code

logger = logging.getLogger(__name__)

SAP_CN_LOG_TABLE = "walmart_sap_cn_log"
DEFAULT_CN_POST_LIMIT = 50
DEFAULT_CN_RETRY_LIMIT = 50
DEFAULT_MAX_CN_ATTEMPTS = 3


def _max_cn_attempts() -> int:
    raw = (os.environ.get("SAP_SYNC_MAX_ATTEMPTS") or "").strip()
    if not raw:
        return DEFAULT_MAX_CN_ATTEMPTS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_CN_ATTEMPTS


def get_db_connection() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)


def _rollback(conn: psycopg.Connection) -> None:
    try:
        conn.rollback()
    except Exception:
        logger.debug("conn rollback failed", exc_info=True)


def _local_invoice_posted(conn: psycopg.Connection, customer_order_id: str) -> bool:
    """True if at least one related PO has a posted AR invoice in local log."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM walmart_sap_invoice_log s
            INNER JOIN walmart_order_headers h
              ON h.purchase_order_id = s.purchase_order_id
            WHERE h.customer_order_id = %s
              AND s.status = 'posted'
            LIMIT 1
            """,
            (customer_order_id,),
        )
        return cur.fetchone() is not None


def _fetch_return(conn: psycopg.Connection, return_order_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              return_order_id,
              customer_order_id,
              return_order_date,
              refund_mode,
              total_refund_amount,
              total_refund_currency,
              purchase_order_ids,
              return_reasons,
              status,
              status_time,
              current_delivery_status,
              current_refund_status,
              credit_note_status,
              raw_json,
              fetched_at,
              created_at,
              updated_at
            FROM walmart_returns
            WHERE return_order_id = %s
            """,
            (return_order_id,),
        )
        columns = [d[0] for d in cur.description] if cur.description else []
        row = cur.fetchone()
    if not row:
        return None
    return dict(zip(columns, row))


def _fetch_ready_returns(
    conn: psycopg.Connection,
    *,
    limit: int,
    return_order_id: str | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "limit": limit,
        "refund_completed": REFUND_COMPLETED,
        "blocked": list(BLOCKED_CN_STATUSES),
    }
    sql = """
        SELECT
          r.return_order_id,
          r.customer_order_id,
          r.return_order_date,
          r.purchase_order_ids,
          r.return_reasons,
          r.current_refund_status,
          r.credit_note_status,
          r.raw_json
        FROM walmart_returns r
        WHERE r.current_refund_status = %(refund_completed)s
          AND r.credit_note_status <> ALL(%(blocked)s)
          AND NOT EXISTS (
            SELECT 1 FROM walmart_sap_cn_log c
            WHERE c.return_order_id = r.return_order_id
              AND c.status IN ('posted', 'failed')
          )
    """
    if return_order_id:
        sql += " AND r.return_order_id = %(return_order_id)s\n"
        params["return_order_id"] = return_order_id
    sql += " ORDER BY r.return_order_date ASC NULLS LAST, r.return_order_id ASC\nLIMIT %(limit)s"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _fetch_failed_for_retry(
    conn: psycopg.Connection,
    *,
    limit: int,
    return_order_id: str | None = None,
    max_attempts: int,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "limit": limit,
        "max_attempts": max_attempts,
        "refund_completed": REFUND_COMPLETED,
        "blocked": list(BLOCKED_CN_STATUSES),
    }
    sql = f"""
        SELECT
          r.return_order_id,
          r.customer_order_id,
          r.return_order_date,
          r.purchase_order_ids,
          r.return_reasons,
          r.current_refund_status,
          r.credit_note_status,
          r.raw_json
        FROM {SAP_CN_LOG_TABLE} c
        INNER JOIN walmart_returns r ON r.return_order_id = c.return_order_id
        WHERE c.status = 'failed'
          AND COALESCE(c.attempt_count, 0) < %(max_attempts)s
          AND r.current_refund_status = %(refund_completed)s
          AND r.credit_note_status <> ALL(%(blocked)s)
    """
    if return_order_id:
        sql += " AND r.return_order_id = %(return_order_id)s\n"
        params["return_order_id"] = return_order_id
    sql += " ORDER BY c.updated_at ASC\nLIMIT %(limit)s"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _upsert_cn_log(
    conn: psycopg.Connection,
    *,
    return_order_id: str,
    customer_order_id: str | None,
    status: str,
    sap_payload: dict[str, Any] | None = None,
    sap_doc_entry: int | None = None,
    sap_doc_num: int | None = None,
    base_doc_entry: int | None = None,
    error_message: str | None = None,
    error_phase: str | None = None,
    skip_reason: str | None = None,
    sap_response: dict[str, Any] | None = None,
    bump_attempt: bool = True,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {SAP_CN_LOG_TABLE} (
              return_order_id, customer_order_id, status,
              sap_payload, sap_doc_entry, sap_doc_num, base_doc_entry,
              error_message, error_phase, skip_reason, sap_response,
              attempt_count, updated_at
            ) VALUES (
              %(return_order_id)s, %(customer_order_id)s, %(status)s,
              %(sap_payload)s::jsonb, %(sap_doc_entry)s, %(sap_doc_num)s, %(base_doc_entry)s,
              %(error_message)s, %(error_phase)s, %(skip_reason)s, %(sap_response)s::jsonb,
              CASE WHEN %(bump_attempt)s THEN 1 ELSE 0 END,
              now()
            )
            ON CONFLICT (return_order_id) DO UPDATE SET
              customer_order_id = COALESCE(EXCLUDED.customer_order_id, {SAP_CN_LOG_TABLE}.customer_order_id),
              status = EXCLUDED.status,
              sap_payload = COALESCE(EXCLUDED.sap_payload, {SAP_CN_LOG_TABLE}.sap_payload),
              sap_doc_entry = COALESCE(EXCLUDED.sap_doc_entry, {SAP_CN_LOG_TABLE}.sap_doc_entry),
              sap_doc_num = COALESCE(EXCLUDED.sap_doc_num, {SAP_CN_LOG_TABLE}.sap_doc_num),
              base_doc_entry = COALESCE(EXCLUDED.base_doc_entry, {SAP_CN_LOG_TABLE}.base_doc_entry),
              error_message = EXCLUDED.error_message,
              error_phase = EXCLUDED.error_phase,
              skip_reason = EXCLUDED.skip_reason,
              sap_response = COALESCE(EXCLUDED.sap_response, {SAP_CN_LOG_TABLE}.sap_response),
              attempt_count = CASE
                WHEN %(bump_attempt)s THEN {SAP_CN_LOG_TABLE}.attempt_count + 1
                ELSE {SAP_CN_LOG_TABLE}.attempt_count
              END,
              updated_at = now()
            WHERE {SAP_CN_LOG_TABLE}.status IS DISTINCT FROM 'posted'
            """,
            {
                "return_order_id": return_order_id,
                "customer_order_id": customer_order_id,
                "status": status,
                "sap_payload": json.dumps(sap_payload) if sap_payload is not None else None,
                "sap_doc_entry": sap_doc_entry,
                "sap_doc_num": sap_doc_num,
                "base_doc_entry": base_doc_entry,
                "error_message": (error_message or "")[:4000] if error_message else None,
                "error_phase": error_phase,
                "skip_reason": (skip_reason or "")[:1000] if skip_reason else None,
                "sap_response": json.dumps(sap_response) if sap_response is not None else None,
                "bump_attempt": bump_attempt,
            },
        )


def _set_return_cn_status(
    conn: psycopg.Connection,
    return_order_id: str,
    credit_note_status: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE walmart_returns
            SET credit_note_status = %(status)s,
                updated_at = now()
            WHERE return_order_id = %(return_order_id)s
            """,
            {"status": credit_note_status, "return_order_id": return_order_id},
        )


def _process_one(
    conn: psycopg.Connection,
    return_row: dict[str, Any],
    *,
    post_to_sap: bool,
) -> str:
    """Returns 'posted' | 'skipped' | 'failed' | 'prepared'."""
    rid = str(return_row["return_order_id"])
    customer_order_id = str(return_row.get("customer_order_id") or "").strip()

    ok, reason = can_post_credit_note(
        current_refund_status=return_row.get("current_refund_status"),
        credit_note_status=return_row.get("credit_note_status"),
        raw_json=return_row.get("raw_json"),
    )
    if not ok:
        _upsert_cn_log(
            conn,
            return_order_id=rid,
            customer_order_id=customer_order_id,
            status="skipped",
            skip_reason=reason,
            bump_attempt=False,
        )
        conn.commit()
        logger.warning("SKIP CN %s: %s", rid, reason)
        return "skipped"

    if not customer_order_id:
        return _fail(conn, rid, None, "missing customer_order_id", "gate")

    # Local invoice must be posted first (order pipeline).
    if not _local_invoice_posted(conn, customer_order_id):
        return _fail(
            conn,
            rid,
            customer_order_id,
            "AR Invoice not posted locally yet for this customerOrderId — post invoice first",
            "gate",
        )

    try:
        existing = find_credit_note_by_ref(rid)
    except SapLookupError as e:
        return _fail(conn, rid, customer_order_id, str(e), "lookup")

    if existing:
        _upsert_cn_log(
            conn,
            return_order_id=rid,
            customer_order_id=customer_order_id,
            status="posted",
            sap_doc_entry=existing["doc_entry"],
            sap_doc_num=existing["doc_num"],
            skip_reason="already in SAP",
            bump_attempt=False,
        )
        _set_return_cn_status(conn, rid, "Posted")
        conn.commit()
        return "posted"

    try:
        inv_ref = find_invoice_by_customer_order_id(customer_order_id)
    except SapLookupError as e:
        return _fail(conn, rid, customer_order_id, str(e), "lookup")

    if inv_ref is None:
        return _fail(
            conn,
            rid,
            customer_order_id,
            f"no live AR Invoice in SAP with NumAtCard={customer_order_id!r}",
            "lookup",
        )

    try:
        invoice = get_invoice_doc(inv_ref["doc_entry"])
    except SapLookupError as e:
        return _fail(conn, rid, customer_order_id, str(e), "lookup")

    if (invoice.get("NumAtCard") or "").strip() != customer_order_id:
        return _fail(
            conn,
            rid,
            customer_order_id,
            f"stale invoice pointer DocEntry={inv_ref['doc_entry']} "
            f"NumAtCard={invoice.get('NumAtCard')!r}",
            "lookup",
        )
    if (invoice.get("CancelStatus") or "csNo") != "csNo":
        return _fail(
            conn,
            rid,
            customer_order_id,
            f"base invoice DocEntry={inv_ref['doc_entry']} is cancelled",
            "lookup",
        )

    def _resolve(sku: str) -> str:
        return resolve_item_code(conn, sku)

    try:
        payload = build_credit_note_payload(
            return_row=return_row,
            invoice=invoice,
            resolve_item_code=_resolve,
        )
    except CreditNoteBuildError as e:
        return _fail(conn, rid, customer_order_id, str(e), "build")
    except Exception as e:
        return _fail(conn, rid, customer_order_id, str(e), "build")

    if not post_to_sap:
        _upsert_cn_log(
            conn,
            return_order_id=rid,
            customer_order_id=customer_order_id,
            status="prepared",
            sap_payload=payload,
            base_doc_entry=int(invoice["DocEntry"]),
            bump_attempt=False,
        )
        conn.commit()
        return "prepared"

    try:
        result = post_credit_note(payload)
    except SapPostError as e:
        return _fail(
            conn,
            rid,
            customer_order_id,
            str(e),
            "post",
            sap_payload=payload,
            base_doc_entry=int(invoice["DocEntry"]),
        )

    _upsert_cn_log(
        conn,
        return_order_id=rid,
        customer_order_id=customer_order_id,
        status="posted",
        sap_payload=payload,
        sap_doc_entry=result["doc_entry"],
        sap_doc_num=result["doc_num"],
        base_doc_entry=int(invoice["DocEntry"]),
        sap_response=result.get("response"),
        bump_attempt=True,
    )
    _set_return_cn_status(conn, rid, "Posted")
    conn.commit()
    logger.info(
        "OK CN %s -> DocEntry=%s DocNum=%s",
        rid,
        result["doc_entry"],
        result["doc_num"],
    )
    return "posted"


def _fail(
    conn: psycopg.Connection,
    return_order_id: str,
    customer_order_id: str | None,
    error: str,
    phase: str,
    *,
    sap_payload: dict[str, Any] | None = None,
    base_doc_entry: int | None = None,
) -> str:
    _rollback(conn)
    try:
        _upsert_cn_log(
            conn,
            return_order_id=return_order_id,
            customer_order_id=customer_order_id,
            status="failed",
            sap_payload=sap_payload,
            base_doc_entry=base_doc_entry,
            error_message=error,
            error_phase=phase,
            bump_attempt=True,
        )
        _set_return_cn_status(conn, return_order_id, "Failed")
        conn.commit()
    except Exception:
        _rollback(conn)
        logger.exception("failed to persist CN failure for %s", return_order_id)
    logger.error("FAIL CN %s [%s]: %s", return_order_id, phase, error)
    return "failed"


def run_sap_cn_post_sync(
    *,
    return_order_id: str | None = None,
    limit: int = DEFAULT_CN_POST_LIMIT,
    post_to_sap: bool = True,
) -> dict[str, Any]:
    with get_db_connection() as conn:
        rows = _fetch_ready_returns(conn, limit=limit, return_order_id=return_order_id)
        counts = {
            "candidates": len(rows),
            "posted": 0,
            "skipped": 0,
            "failed": 0,
            "prepared": 0,
        }
        for row in rows:
            outcome = _process_one(conn, row, post_to_sap=post_to_sap)
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts


def run_sap_cn_retry_sync(
    *,
    return_order_id: str | None = None,
    limit: int = DEFAULT_CN_RETRY_LIMIT,
    post_to_sap: bool = True,
) -> dict[str, Any]:
    max_attempts = _max_cn_attempts()
    with get_db_connection() as conn:
        rows = _fetch_failed_for_retry(
            conn,
            limit=limit,
            return_order_id=return_order_id,
            max_attempts=max_attempts,
        )
        # Reset Failed → Not Posted so gate allows rebuild
        for row in rows:
            if str(row.get("credit_note_status") or "") == "Failed":
                _set_return_cn_status(conn, str(row["return_order_id"]), "Not Posted")
                row["credit_note_status"] = "Not Posted"
        conn.commit()

        counts = {
            "candidates": len(rows),
            "posted": 0,
            "skipped": 0,
            "failed": 0,
            "prepared": 0,
        }
        for row in rows:
            outcome = _process_one(conn, row, post_to_sap=post_to_sap)
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts


def run_sap_cn_post_single(
    return_order_id: str,
    *,
    post_to_sap: bool = True,
) -> dict[str, Any]:
    rid = (return_order_id or "").strip()
    if not rid:
        return {"status": "error", "error": "return_order_id is required"}

    with get_db_connection() as conn:
        row = _fetch_return(conn, rid)
        if row is None:
            return {"status": "error", "return_order_id": rid, "error": "Return not found"}

        # Manual post may retry Failed
        if str(row.get("credit_note_status") or "") == "Failed":
            _set_return_cn_status(conn, rid, "Not Posted")
            row["credit_note_status"] = "Not Posted"
            conn.commit()

        outcome = _process_one(conn, row, post_to_sap=post_to_sap)
        result: dict[str, Any] = {
            "status": outcome,
            "return_order_id": rid,
            "customer_order_id": row.get("customer_order_id"),
        }
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT status, sap_doc_entry, sap_doc_num, error_message, error_phase, sap_payload
                FROM {SAP_CN_LOG_TABLE}
                WHERE return_order_id = %s
                """,
                (rid,),
            )
            log_row = cur.fetchone()
        if log_row:
            result.update(
                {
                    "cn_status": log_row[0],
                    "sap_doc_entry": log_row[1],
                    "sap_doc_num": log_row[2],
                    "error_message": log_row[3],
                    "error_phase": log_row[4],
                    "sap_payload": log_row[5],
                }
            )
        if outcome == "failed":
            result["status"] = "failed"
        return result


def preview_credit_note_payload(return_order_id: str) -> dict[str, Any]:
    """Rebuild payload without posting (still hits live SAP for invoice).

    Preview ignores credit_note_status (Posted / Manual / Failed) so ops can
    inspect the body for Postman even after a successful post.
    """
    rid = (return_order_id or "").strip()
    with get_db_connection() as conn:
        row = _fetch_return(conn, rid)
        if row is None:
            return {"ok": False, "error": "Return not found", "return_order_id": rid}

        # Preview-only: do not block on Posted / Manual / Failed.
        ok, reason = can_post_credit_note(
            current_refund_status=row.get("current_refund_status"),
            credit_note_status="Not Posted",
            raw_json=row.get("raw_json"),
        )
        if not ok:
            return {"ok": False, "error": reason, "return_order_id": rid}

        customer_order_id = str(row.get("customer_order_id") or "").strip()
        if not customer_order_id:
            return {"ok": False, "error": "missing customer_order_id", "return_order_id": rid}

        inv_ref = find_invoice_by_customer_order_id(customer_order_id)
        if inv_ref is None:
            return {
                "ok": False,
                "error": f"no AR Invoice with NumAtCard={customer_order_id!r}",
                "return_order_id": rid,
            }
        invoice = get_invoice_doc(inv_ref["doc_entry"])
        payload = build_credit_note_payload(
            return_row={**row, "credit_note_status": "Not Posted"},
            invoice=invoice,
            resolve_item_code=lambda sku: resolve_item_code(conn, sku),
        )
        return {
            "ok": True,
            "return_order_id": rid,
            "customer_order_id": customer_order_id,
            "base_doc_entry": inv_ref["doc_entry"],
            "payload": payload,
        }


def mark_credit_note_manual(return_order_id: str) -> dict[str, Any]:
    """Ops: mark return as Posted (Manual) without posting to SAP."""
    rid = (return_order_id or "").strip()
    if not rid:
        return {"ok": False, "error": "return_order_id is required"}

    with get_db_connection() as conn:
        row = _fetch_return(conn, rid)
        if row is None:
            return {"ok": False, "error": "Return not found", "return_order_id": rid}

        status = str(row.get("credit_note_status") or "").strip()
        if status in ("Posted", "Posted (Manual)", "Not Applicable"):
            return {
                "ok": False,
                "error": f"cannot mark Manual when credit_note_status is {status!r}",
                "return_order_id": rid,
                "credit_note_status": status,
            }

        _set_return_cn_status(conn, rid, "Posted (Manual)")
        _upsert_cn_log(
            conn,
            return_order_id=rid,
            customer_order_id=str(row.get("customer_order_id") or "") or None,
            status="skipped",
            skip_reason="Posted (Manual) — marked by operator, no SAP post",
            bump_attempt=False,
        )
        conn.commit()
        return {
            "ok": True,
            "return_order_id": rid,
            "credit_note_status": "Posted (Manual)",
        }


def unmark_credit_note(return_order_id: str) -> dict[str, Any]:
    """Ops: clear Posted (Manual) / Failed back to Not Posted so auto-post can run."""
    rid = (return_order_id or "").strip()
    if not rid:
        return {"ok": False, "error": "return_order_id is required"}

    with get_db_connection() as conn:
        row = _fetch_return(conn, rid)
        if row is None:
            return {"ok": False, "error": "Return not found", "return_order_id": rid}

        status = str(row.get("credit_note_status") or "").strip()
        if status not in ("Posted (Manual)", "Failed"):
            return {
                "ok": False,
                "error": (
                    f"unmark only allowed from Posted (Manual) or Failed "
                    f"(current={status!r}). Cancel in SAP to reverse a Posted CN."
                ),
                "return_order_id": rid,
                "credit_note_status": status,
            }

        _set_return_cn_status(conn, rid, "Not Posted")
        # Clear failed/skipped log so ready-selection can pick it up again.
        with conn.cursor() as cur:
            cur.execute(
                f"""
                DELETE FROM {SAP_CN_LOG_TABLE}
                WHERE return_order_id = %s
                  AND status IN ('failed', 'skipped', 'prepared')
                """,
                (rid,),
            )
        conn.commit()
        return {
            "ok": True,
            "return_order_id": rid,
            "credit_note_status": "Not Posted",
        }


def cancel_posted_credit_note(return_order_id: str) -> dict[str, Any]:
    """Cancel the SAP Credit Note (POST .../Cancel) and reset local status to Not Posted."""
    rid = (return_order_id or "").strip()
    if not rid:
        return {"ok": False, "error": "return_order_id is required"}

    with get_db_connection() as conn:
        row = _fetch_return(conn, rid)
        if row is None:
            return {"ok": False, "error": "Return not found", "return_order_id": rid}

        status = str(row.get("credit_note_status") or "").strip()
        if status != "Posted":
            return {
                "ok": False,
                "error": f"cancel only allowed when Posted (current={status!r})",
                "return_order_id": rid,
                "credit_note_status": status,
            }

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT sap_doc_entry, sap_doc_num
                FROM {SAP_CN_LOG_TABLE}
                WHERE return_order_id = %s AND status = 'posted'
                """,
                (rid,),
            )
            log_row = cur.fetchone()

        doc_entry = int(log_row[0]) if log_row and log_row[0] is not None else None
        if doc_entry is None:
            # Fall back to live SAP lookup by NumAtCard = return_order_id
            try:
                existing = find_credit_note_by_ref(rid)
            except SapLookupError as e:
                return {"ok": False, "error": str(e), "return_order_id": rid}
            if not existing:
                return {
                    "ok": False,
                    "error": "no SAP DocEntry found for this return",
                    "return_order_id": rid,
                }
            doc_entry = existing["doc_entry"]

        try:
            cancel_result = cancel_credit_note(doc_entry)
        except SapPostError as e:
            return {"ok": False, "error": str(e), "return_order_id": rid, "sap_doc_entry": doc_entry}

        _set_return_cn_status(conn, rid, "Not Posted")
        # Force past the posted-lock on the upsert helper.
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {SAP_CN_LOG_TABLE} SET
                  status = 'skipped',
                  skip_reason = %(skip_reason)s,
                  sap_response = %(sap_response)s::jsonb,
                  error_message = NULL,
                  error_phase = NULL,
                  updated_at = now()
                WHERE return_order_id = %(return_order_id)s
                """,
                {
                    "return_order_id": rid,
                    "skip_reason": f"Cancelled in SAP (DocEntry={doc_entry})",
                    "sap_response": json.dumps(cancel_result.get("response")),
                },
            )
            if cur.rowcount == 0:
                cur.execute(
                    f"""
                    INSERT INTO {SAP_CN_LOG_TABLE} (
                      return_order_id, customer_order_id, status,
                      sap_doc_entry, skip_reason, sap_response, attempt_count, updated_at
                    ) VALUES (
                      %(return_order_id)s, %(customer_order_id)s, 'skipped',
                      %(sap_doc_entry)s, %(skip_reason)s, %(sap_response)s::jsonb, 0, now()
                    )
                    """,
                    {
                        "return_order_id": rid,
                        "customer_order_id": str(row.get("customer_order_id") or "") or None,
                        "sap_doc_entry": doc_entry,
                        "skip_reason": f"Cancelled in SAP (DocEntry={doc_entry})",
                        "sap_response": json.dumps(cancel_result.get("response")),
                    },
                )
        conn.commit()
        return {
            "ok": True,
            "return_order_id": rid,
            "credit_note_status": "Not Posted",
            "sap_doc_entry": doc_entry,
            "cancelled": True,
        }
