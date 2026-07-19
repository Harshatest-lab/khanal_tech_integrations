"""Prepare and optionally post SAP A/R Invoices when Walmart lines ship."""

from __future__ import annotations

import json
import logging
import os
import traceback
from typing import Any

import psycopg

from walmart.credentials import DATABASE_URL
from walmart.sap_alert_email import send_sap_failure_alert
from walmart.sap_invoice_payload import build_sap_invoice_payload

logger = logging.getLogger(__name__)

SAP_INVOICE_LOG_TABLE = "walmart_sap_invoice_log"

DEFAULT_RETRY_LIMIT = 50
DEFAULT_SAP_POST_LIMIT = 100
DEFAULT_MAX_SYNC_ATTEMPTS = 3
SAP_ELIGIBLE_STATUSES = ("Shipped", "Delivered", "Dispensed")

FETCH_INVOICE_ROW = f"""
SELECT status, sap_doc_entry, sap_doc_num, attempt_count
FROM {SAP_INVOICE_LOG_TABLE}
WHERE purchase_order_id = %(purchase_order_id)s
"""

FETCH_FAILED_FOR_RETRY_BASE = f"""
SELECT s.purchase_order_id, h.raw_payload
FROM {SAP_INVOICE_LOG_TABLE} s
INNER JOIN walmart_order_headers h
  ON h.purchase_order_id = s.purchase_order_id
WHERE s.status = 'failed'
  AND COALESCE(s.attempt_count, 0) < %(max_attempts)s
"""


def _max_sync_attempts() -> int:
    raw = (os.environ.get("SAP_SYNC_MAX_ATTEMPTS") or "").strip()
    if not raw:
        return DEFAULT_MAX_SYNC_ATTEMPTS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_SYNC_ATTEMPTS


def _fetch_failed_for_retry_sql(
    purchase_order_id: str | None, *, max_attempts: int
) -> tuple[str, dict[str, Any]]:
    """Build retry query without ``%(param)s IS NULL`` (psycopg ambiguous type)."""
    params: dict[str, Any] = {"max_attempts": max_attempts}
    sql = FETCH_FAILED_FOR_RETRY_BASE
    if purchase_order_id:
        sql += "  AND s.purchase_order_id = %(purchase_order_id)s\n"
        params["purchase_order_id"] = purchase_order_id
    sql += "ORDER BY s.updated_at ASC\nLIMIT %(limit)s"
    return sql, params


COUNT_SAP_ELIGIBLE_LINES = """
SELECT COUNT(*)::int
FROM walmart_order_lines
WHERE purchase_order_id = %(purchase_order_id)s
  AND status = ANY(%(eligible_statuses)s::text[])
"""

FETCH_READY_FOR_SAP_BASE = f"""
SELECT h.purchase_order_id, h.raw_payload
FROM walmart_order_headers h
WHERE EXISTS (
  SELECT 1
  FROM walmart_order_lines l
  WHERE l.purchase_order_id = h.purchase_order_id
    AND l.status = ANY(%(eligible_statuses)s::text[])
)
AND NOT EXISTS (
  SELECT 1
  FROM {SAP_INVOICE_LOG_TABLE} s
  WHERE s.purchase_order_id = h.purchase_order_id
    AND s.status IN ('posted', 'failed')
)
ORDER BY h.updated_at ASC
LIMIT %(limit)s
"""


def _line_newly_shipped(line: dict[str, Any]) -> bool:
    if str(line.get("status") or "").strip() != "Shipped":
        return False
    prev = line.get("previous_status")
    return prev is None or str(prev).strip() != "Shipped"


def _orders_by_po(orders: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_po: dict[str, dict[str, Any]] = {}
    for order in orders:
        po = str(order.get("purchaseOrderId") or "").strip()
        if po:
            by_po[po] = order
    return by_po


def _post_to_sap_sl(payload: dict[str, Any]) -> dict[str, Any]:
    """POST Invoices to SAP Service Layer (/sap/inc/* when SAP_SL_PROFILE=inc)."""
    try:
        from khanal_shared.sap_sl import (
            build_opener_and_base_from_env,
            build_opener_and_base_via_ssm,
            sap_sl_enabled,
            sap_sl_request_json,
            sap_sl_use_ssm,
        )
    except ImportError:
        logger.info("SAP SL not available in layer; skipping post")
        return {"skipped": True, "reason": "sap_sl_not_installed"}

    if not sap_sl_enabled():
        logger.info("SAP SL not configured; payload prepared only")
        return {"skipped": True, "reason": "sap_sl_not_configured"}

    if sap_sl_use_ssm():
        opener, base = build_opener_and_base_via_ssm()
    else:
        opener, base = build_opener_and_base_from_env()

    url = f"{base.rstrip('/')}/Invoices"
    status, body = sap_sl_request_json(opener, "POST", url, body=payload)
    if status not in (200, 201):
        raise RuntimeError(f"SAP SL POST Invoices returned HTTP {status}: {body}")

    doc_entry: int | None = None
    doc_num: int | None = None
    if isinstance(body, dict):
        if body.get("DocEntry") is not None:
            doc_entry = int(body["DocEntry"])
        if body.get("DocNum") is not None:
            doc_num = int(body["DocNum"])

    return {
        "doc_entry": doc_entry,
        "doc_num": doc_num,
        "sap_response": body if isinstance(body, dict) else None,
    }


def _rollback_conn(conn: psycopg.Connection) -> None:
    """Clear an aborted PostgreSQL transaction so follow-up writes can run."""
    try:
        conn.rollback()
    except Exception:
        logger.debug("conn rollback failed", exc_info=True)


def _mark_retries_exhausted(
    conn: psycopg.Connection,
    *,
    purchase_order_id: str,
    attempt_count: int,
    max_attempts: int,
    customer_order_id: str = "",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {SAP_INVOICE_LOG_TABLE} SET
              error_phase = 'max_retries_exceeded',
              error_message = COALESCE(error_message, '') || %(suffix)s,
              updated_at = now()
            WHERE purchase_order_id = %(purchase_order_id)s
              AND status = 'failed'
            """,
            {
                "purchase_order_id": purchase_order_id,
                "suffix": (
                    f"\n[Stopped after {attempt_count} attempt(s); "
                    f"max {max_attempts} — EventBridge will not retry this PO.]"
                ),
            },
        )
    conn.commit()
    _notify_sap_failure(
        purchase_order_id=purchase_order_id,
        customer_order_id=customer_order_id,
        error_phase="max_retries_exceeded",
        error_message=(
            f"SAP sync stopped after {attempt_count} attempt(s) "
            f"(max {max_attempts}). EventBridge will not retry this PO."
        ),
        attempt_count=attempt_count,
        max_retries_exceeded=True,
    )


def _fetch_attempt_count(conn: psycopg.Connection, purchase_order_id: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT attempt_count FROM {SAP_INVOICE_LOG_TABLE} WHERE purchase_order_id = %s",
            (purchase_order_id,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _notify_sap_failure(
    *,
    purchase_order_id: str,
    customer_order_id: str,
    error_phase: str,
    error_message: str,
    attempt_count: int | None = None,
    max_retries_exceeded: bool = False,
) -> None:
    """Email admin on every SAP failure attempt (best-effort, never raises)."""
    try:
        send_sap_failure_alert(
            purchase_order_id=purchase_order_id,
            customer_order_id=customer_order_id,
            error_phase=error_phase,
            error_message=error_message,
            attempt_count=attempt_count,
            max_retries_exceeded=max_retries_exceeded,
        )
    except Exception:
        logger.exception("SAP failure notification failed for PO %s", purchase_order_id)


def _save_failed(
    conn: psycopg.Connection,
    *,
    purchase_order_id: str,
    customer_order_id: str,
    error_message: str,
    error_phase: str,
    sap_payload: dict[str, Any] | None = None,
    attempt_count: int | None = None,
) -> None:
    _rollback_conn(conn)
    payload_json = json.dumps(sap_payload) if sap_payload else "{}"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {SAP_INVOICE_LOG_TABLE} (
              purchase_order_id, customer_order_id, status, sap_payload,
              error_message, error_phase, updated_at
            ) VALUES (
              %(purchase_order_id)s, %(customer_order_id)s, 'failed',
              %(sap_payload)s::jsonb, %(error_message)s, %(error_phase)s, now()
            )
            ON CONFLICT (purchase_order_id) DO UPDATE SET
              status = 'failed',
              error_message = EXCLUDED.error_message,
              error_phase = EXCLUDED.error_phase,
              sap_payload = CASE
                WHEN EXCLUDED.sap_payload <> '{{}}'::jsonb THEN EXCLUDED.sap_payload
                ELSE {SAP_INVOICE_LOG_TABLE}.sap_payload
              END,
              updated_at = now()
            WHERE {SAP_INVOICE_LOG_TABLE}.status <> 'posted'
            """,
            {
                "purchase_order_id": purchase_order_id,
                "customer_order_id": customer_order_id,
                "sap_payload": payload_json,
                "error_message": error_message[:8000],
                "error_phase": error_phase,
            },
        )
    conn.commit()
    resolved_attempt = attempt_count
    if resolved_attempt is None:
        resolved_attempt = _fetch_attempt_count(conn, purchase_order_id)
    _notify_sap_failure(
        purchase_order_id=purchase_order_id,
        customer_order_id=customer_order_id,
        error_phase=error_phase,
        error_message=error_message,
        attempt_count=resolved_attempt,
    )


def _preview_payload_on_build_error(
    conn: psycopg.Connection,
    order: dict[str, Any],
    build_error: str,
) -> dict[str, Any] | None:
    """Build a preview payload when full build fails (e.g. missing batch stock)."""
    try:
        payload = build_sap_invoice_payload(conn, order, require_batches=False)
    except Exception:
        logger.debug("Could not build preview SAP payload after build error", exc_info=True)
        return None
    return {
        **payload,
        "_meta": {
            "preview": True,
            "build_error": build_error[:2000],
            "note": (
                "Preview only — not posted. BatchNumbers may show PREVIEW_NO_BATCH_STOCK "
                "where SAP WM-FBA stock is missing. Fix inventory in SAP, then retry."
            ),
        },
    }


def _save_posted(
    conn: psycopg.Connection,
    *,
    purchase_order_id: str,
    doc_entry: int | None,
    doc_num: int | None,
    sap_response: dict[str, Any] | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {SAP_INVOICE_LOG_TABLE} SET
              status = 'posted',
              sap_doc_entry = %(sap_doc_entry)s,
              sap_doc_num = %(sap_doc_num)s,
              sap_response = %(sap_response)s::jsonb,
              error_message = NULL,
              error_phase = NULL,
              updated_at = now()
            WHERE purchase_order_id = %(purchase_order_id)s
            """,
            {
                "purchase_order_id": purchase_order_id,
                "sap_doc_entry": doc_entry,
                "sap_doc_num": doc_num,
                "sap_response": json.dumps(sap_response) if sap_response else None,
            },
        )
    conn.commit()


def _record_sync_attempt(
    conn: psycopg.Connection,
    *,
    purchase_order_id: str,
    customer_order_id: str,
) -> int | None:
    """Increment sync attempt counter (skipped when already posted)."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {SAP_INVOICE_LOG_TABLE} (
              purchase_order_id, customer_order_id, status, sap_payload,
              attempt_count, updated_at
            ) VALUES (
              %(purchase_order_id)s, %(customer_order_id)s, 'prepared',
              '{{}}'::jsonb, 1, now()
            )
            ON CONFLICT (purchase_order_id) DO UPDATE SET
              attempt_count = {SAP_INVOICE_LOG_TABLE}.attempt_count + 1,
              updated_at = now()
            WHERE {SAP_INVOICE_LOG_TABLE}.status <> 'posted'
            RETURNING attempt_count
            """,
            {
                "purchase_order_id": purchase_order_id,
                "customer_order_id": customer_order_id,
            },
        )
        row = cur.fetchone()
    conn.commit()
    return int(row[0]) if row else None


def _process_one_po(
    conn: psycopg.Connection,
    po_id: str,
    order: dict[str, Any],
    *,
    post_to_sap: bool,
    ignore_retry_limit: bool = False,
) -> dict[str, Any]:
    max_attempts = _max_sync_attempts()
    with conn.cursor() as cur:
        cur.execute(FETCH_INVOICE_ROW, {"purchase_order_id": po_id})
        existing = cur.fetchone()
    if existing:
        status, doc_entry, doc_num, prior_attempts = existing
        if status == "posted":
            return {
                "purchase_order_id": po_id,
                "status": "skipped",
                "reason": "already_posted",
                "sap_doc_entry": doc_entry,
                "sap_doc_num": doc_num,
            }
        if (
            not ignore_retry_limit
            and status == "failed"
            and int(prior_attempts or 0) >= max_attempts
        ):
            logger.info(
                "Skipping SAP invoice sync for PO %s — %s attempts (max %s)",
                po_id,
                prior_attempts,
                max_attempts,
            )
            return {
                "purchase_order_id": po_id,
                "status": "skipped",
                "reason": "max_retries_exceeded",
                "attempt_count": prior_attempts,
            }

    customer_order_id = str(order.get("customerOrderId") or "").strip()
    attempt_count = _record_sync_attempt(
        conn,
        purchase_order_id=po_id,
        customer_order_id=customer_order_id,
    )
    try:
        payload = build_sap_invoice_payload(conn, order)
    except Exception as exc:
        logger.exception("SAP invoice payload build failed for PO %s", po_id)
        _rollback_conn(conn)
        preview = None
        try:
            preview = _preview_payload_on_build_error(conn, order, str(exc))
        except Exception:
            logger.debug("Could not build preview SAP payload for PO %s", po_id, exc_info=True)
        try:
            _save_failed(
                conn,
                purchase_order_id=po_id,
                customer_order_id=customer_order_id,
                error_message=str(exc),
                error_phase="build",
                sap_payload=preview,
                attempt_count=attempt_count,
            )
        except Exception:
            logger.exception("Could not persist SAP build failure for PO %s", po_id)
        if attempt_count is not None and attempt_count >= max_attempts:
            try:
                _mark_retries_exhausted(
                    conn,
                    purchase_order_id=po_id,
                    attempt_count=attempt_count,
                    max_attempts=max_attempts,
                    customer_order_id=customer_order_id,
                )
            except Exception:
                logger.exception("Could not mark max retries for PO %s", po_id)
        return {
            "purchase_order_id": po_id,
            "status": "failed",
            "phase": "build",
            "error": str(exc),
            "attempt_count": attempt_count,
        }

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {SAP_INVOICE_LOG_TABLE} (
              purchase_order_id, customer_order_id, status, sap_payload, updated_at
            ) VALUES (
              %(purchase_order_id)s, %(customer_order_id)s, 'prepared',
              %(sap_payload)s::jsonb, now()
            )
            ON CONFLICT (purchase_order_id) DO UPDATE SET
              customer_order_id = EXCLUDED.customer_order_id,
              status = 'prepared',
              sap_payload = EXCLUDED.sap_payload,
              error_message = NULL,
              error_phase = NULL,
              updated_at = now()
            WHERE {SAP_INVOICE_LOG_TABLE}.status <> 'posted'
            RETURNING id
            """,
            {
                "purchase_order_id": po_id,
                "customer_order_id": customer_order_id,
                "sap_payload": json.dumps(payload),
            },
        )
        row = cur.fetchone()
    conn.commit()

    if row is None:
        return {
            "purchase_order_id": po_id,
            "status": "skipped",
            "reason": "already_posted",
        }

    result: dict[str, Any] = {
        "purchase_order_id": po_id,
        "customer_order_id": customer_order_id,
        "status": "prepared",
        "attempt_count": attempt_count,
    }

    if not post_to_sap:
        return result

    try:
        post_result = _post_to_sap_sl(payload)
        if post_result.get("skipped"):
            result["status"] = "prepared"
            result["post_skipped"] = post_result.get("reason")
            return result

        _save_posted(
            conn,
            purchase_order_id=po_id,
            doc_entry=post_result.get("doc_entry"),
            doc_num=post_result.get("doc_num"),
            sap_response=post_result.get("sap_response"),
        )
        result["status"] = "posted"
        result["sap_doc_entry"] = post_result.get("doc_entry")
        result["sap_doc_num"] = post_result.get("doc_num")
        result["attempt_count"] = attempt_count
        return result
    except Exception as exc:
        logger.exception("SAP invoice post failed for PO %s", po_id)
        _rollback_conn(conn)
        try:
            _save_failed(
                conn,
                purchase_order_id=po_id,
                customer_order_id=customer_order_id,
                error_message=str(exc),
                error_phase="post",
                sap_payload=payload,
                attempt_count=attempt_count,
            )
        except Exception:
            logger.exception("Could not persist SAP post failure for PO %s", po_id)
        if attempt_count is not None and attempt_count >= max_attempts:
            try:
                _mark_retries_exhausted(
                    conn,
                    purchase_order_id=po_id,
                    attempt_count=attempt_count,
                    max_attempts=max_attempts,
                    customer_order_id=customer_order_id,
                )
            except Exception:
                logger.exception("Could not mark max retries for PO %s", po_id)
        return {
            "purchase_order_id": po_id,
            "status": "failed",
            "phase": "post",
            "error": str(exc),
            "trace": traceback.format_exc(),
            "attempt_count": attempt_count,
        }


def process_shipped_orders_for_sap_invoice(
    conn: psycopg.Connection,
    orders: list[dict[str, Any]],
    upsert_counts: dict[str, Any],
    *,
    post_to_sap: bool = True,
) -> list[dict[str, Any]]:
    """
    When a line transitions to **Shipped**, build SAP Invoice payload (once per PO).

    Errors are stored per PO in ``walmart_sap_invoice_log``; processing continues for
    the next PO. Uses SAP Service Layer via ``SAP_SL_PROFILE`` (``inc`` → ``/sap/inc/*``).
    """
    candidates = [
        line
        for line in (upsert_counts.get("created_lines") or [])
        + (upsert_counts.get("changed_lines") or [])
        if _line_newly_shipped(line)
    ]
    if not candidates:
        return []

    po_ids = sorted({str(line["purchase_order_id"]) for line in candidates})
    orders_map = _orders_by_po(orders)
    results: list[dict[str, Any]] = []

    for po_id in po_ids:
        try:
            order = orders_map.get(po_id)
            if not order:
                results.append(
                    {
                        "purchase_order_id": po_id,
                        "status": "skipped",
                        "reason": "order_not_in_fetch_batch",
                    }
                )
                continue

            results.append(
                _process_one_po(conn, po_id, order, post_to_sap=post_to_sap)
            )
        except Exception as exc:
            logger.exception("Unexpected SAP invoice error for PO %s", po_id)
            _rollback_conn(conn)
            try:
                _save_failed(
                    conn,
                    purchase_order_id=po_id,
                    customer_order_id="",
                    error_message=str(exc),
                    error_phase="unexpected",
                )
            except Exception:
                logger.exception("Failed to save SAP invoice error for PO %s", po_id)
            results.append(
                {
                    "purchase_order_id": po_id,
                    "status": "failed",
                    "phase": "unexpected",
                    "error": str(exc),
                }
            )

    return results


def _order_from_raw_payload(raw_payload: Any) -> dict[str, Any] | None:
    if not raw_payload:
        return None
    try:
        parsed = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if isinstance(parsed.get("order"), dict):
        return parsed["order"]
    if parsed.get("purchaseOrderId"):
        return parsed
    return None


def post_ready_sap_invoices(
    conn: psycopg.Connection,
    *,
    purchase_order_id: str | None = None,
    limit: int = DEFAULT_SAP_POST_LIMIT,
    post_to_sap: bool = True,
) -> dict[str, Any]:
    """
    Post stored Walmart orders to SAP in a separate batch.

    This is intentionally decoupled from the Walmart order pull. It selects POs
    with at least one SAP-eligible line status and no posted/failed SAP log row.
    Failed rows are handled by the retry job.
    """
    po_filter = (purchase_order_id or "").strip() or None
    safe_limit = max(1, min(int(limit), 500))
    params: dict[str, Any] = {
        "eligible_statuses": list(SAP_ELIGIBLE_STATUSES),
        "limit": safe_limit,
    }
    sql = FETCH_READY_FOR_SAP_BASE
    if po_filter:
        sql = sql.replace(
            "ORDER BY h.updated_at ASC",
            "AND h.purchase_order_id = %(purchase_order_id)s\nORDER BY h.updated_at ASC",
        )
        params["purchase_order_id"] = po_filter

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    results: list[dict[str, Any]] = []
    for po_id, raw_payload in rows:
        po_id = str(po_id).strip()
        try:
            order = _order_from_raw_payload(raw_payload)
            if not order:
                results.append(
                    {
                        "purchase_order_id": po_id,
                        "status": "skipped",
                        "reason": "no_order_json_in_header",
                    }
                )
                continue
            results.append(_process_one_po(conn, po_id, order, post_to_sap=post_to_sap))
        except Exception as exc:
            logger.exception("SAP invoice post-ready failed for PO %s", po_id)
            _rollback_conn(conn)
            try:
                _save_failed(
                    conn,
                    purchase_order_id=po_id,
                    customer_order_id="",
                    error_message=str(exc),
                    error_phase="post_ready",
                )
            except Exception:
                logger.exception("Failed to save SAP invoice post-ready error for PO %s", po_id)
            results.append(
                {
                    "purchase_order_id": po_id,
                    "status": "failed",
                    "phase": "post_ready",
                    "error": str(exc),
                }
            )

    posted = sum(1 for r in results if r.get("status") == "posted")
    prepared = sum(1 for r in results if r.get("status") == "prepared")
    failed = sum(1 for r in results if r.get("status") == "failed")
    skipped = sum(1 for r in results if r.get("status") == "skipped")

    return {
        "candidates": len(rows),
        "posted": posted,
        "prepared": prepared,
        "failed": failed,
        "skipped": skipped,
        "eligible_statuses": list(SAP_ELIGIBLE_STATUSES),
        "results": results,
    }


def retry_failed_sap_invoices(
    conn: psycopg.Connection,
    *,
    purchase_order_id: str | None = None,
    limit: int = DEFAULT_RETRY_LIMIT,
    post_to_sap: bool = True,
) -> dict[str, Any]:
    """
    Re-process POs with ``walmart_sap_invoice_log.status = 'failed'``.

    Loads order JSON from ``walmart_order_headers.raw_payload``, rebuilds payload,
    and posts to SAP. Skips POs with no eligible lines. Continues on per-PO errors.
    """
    po_filter = (purchase_order_id or "").strip() or None
    safe_limit = max(1, min(int(limit), 200))
    max_attempts = _max_sync_attempts()

    with conn.cursor() as cur:
        sql, params = _fetch_failed_for_retry_sql(po_filter, max_attempts=max_attempts)
        params["limit"] = safe_limit
        cur.execute(sql, params)
        rows = cur.fetchall()

    results: list[dict[str, Any]] = []
    for po_id, raw_payload in rows:
        po_id = str(po_id).strip()
        order: dict[str, Any] | None = None
        try:
            order = _order_from_raw_payload(raw_payload)
            if not order:
                results.append(
                    {
                        "purchase_order_id": po_id,
                        "status": "skipped",
                        "reason": "no_order_json_in_header",
                    }
                )
                continue

            with conn.cursor() as cur:
                cur.execute(
                    COUNT_SAP_ELIGIBLE_LINES,
                    {
                        "purchase_order_id": po_id,
                        "eligible_statuses": list(SAP_ELIGIBLE_STATUSES),
                    },
                )
                eligible_count = int(cur.fetchone()[0])
            if eligible_count < 1:
                results.append(
                    {
                        "purchase_order_id": po_id,
                        "status": "skipped",
                        "reason": "no_sap_eligible_lines",
                    }
                )
                continue

            results.append(
                _process_one_po(conn, po_id, order, post_to_sap=post_to_sap)
            )
        except Exception as exc:
            logger.exception("SAP invoice retry failed for PO %s", po_id)
            _rollback_conn(conn)
            try:
                preview = (
                    _preview_payload_on_build_error(conn, order, str(exc))
                    if order
                    else None
                )
                _save_failed(
                    conn,
                    purchase_order_id=po_id,
                    customer_order_id=str(order.get("customerOrderId") or "") if order else "",
                    error_message=str(exc),
                    error_phase="retry",
                    sap_payload=preview,
                )
            except Exception:
                logger.exception("Failed to save SAP invoice retry error for PO %s", po_id)
            results.append(
                {
                    "purchase_order_id": po_id,
                    "status": "failed",
                    "phase": "retry",
                    "error": str(exc),
                }
            )

    posted = sum(1 for r in results if r.get("status") == "posted")
    failed = sum(1 for r in results if r.get("status") == "failed")
    skipped = sum(1 for r in results if r.get("status") == "skipped")

    return {
        "candidates": len(rows),
        "posted": posted,
        "failed": failed,
        "skipped": skipped,
        "results": results,
    }


def run_sap_invoice_retry_sync(
    *,
    purchase_order_id: str | None = None,
    limit: int = DEFAULT_RETRY_LIMIT,
    post_to_sap: bool = True,
) -> dict[str, Any]:
    """Entry point for Lambda ``mode: sap_retry`` and local CLI."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    with psycopg.connect(DATABASE_URL) as conn:
        return retry_failed_sap_invoices(
            conn,
            purchase_order_id=purchase_order_id,
            limit=limit,
            post_to_sap=post_to_sap,
        )


def post_single_order_to_sap(
    conn: psycopg.Connection,
    purchase_order_id: str,
    *,
    post_to_sap: bool = True,
    ignore_retry_limit: bool = False,
) -> dict[str, Any]:
    """Post one PO to SAP from stored RDS data (dashboard manual action)."""
    po = purchase_order_id.strip()
    if not po:
        raise ValueError("purchase_order_id is required")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT raw_payload FROM walmart_order_headers WHERE purchase_order_id = %s",
            (po,),
        )
        row = cur.fetchone()
    if row is None:
        return {
            "purchase_order_id": po,
            "status": "error",
            "error": "Order not found",
        }

    with conn.cursor() as cur:
        cur.execute(
            COUNT_SAP_ELIGIBLE_LINES,
            {
                "purchase_order_id": po,
                "eligible_statuses": list(SAP_ELIGIBLE_STATUSES),
            },
        )
        eligible_count = int(cur.fetchone()[0])
    if eligible_count < 1:
        return {
            "purchase_order_id": po,
            "status": "skipped",
            "reason": "no_sap_eligible_lines",
            "eligible_statuses": list(SAP_ELIGIBLE_STATUSES),
        }

    order = _order_from_raw_payload(row[0])
    if not order:
        return {
            "purchase_order_id": po,
            "status": "skipped",
            "reason": "no_order_json_in_header",
        }

    try:
        return _process_one_po(
            conn,
            po,
            order,
            post_to_sap=post_to_sap,
            ignore_retry_limit=ignore_retry_limit,
        )
    except Exception as exc:
        logger.exception("Manual SAP post failed for PO %s", po)
        _rollback_conn(conn)
        customer_order_id = str(order.get("customerOrderId") or "").strip()
        try:
            _save_failed(
                conn,
                purchase_order_id=po,
                customer_order_id=customer_order_id,
                error_message=str(exc),
                error_phase="manual_post",
            )
        except Exception:
            logger.exception("Could not persist manual SAP post failure for PO %s", po)
        return {
            "purchase_order_id": po,
            "status": "failed",
            "phase": "manual_post",
            "error": str(exc),
        }


def run_sap_invoice_post_sync(
    *,
    purchase_order_id: str | None = None,
    limit: int = DEFAULT_SAP_POST_LIMIT,
    post_to_sap: bool = True,
) -> dict[str, Any]:
    """Entry point for Lambda ``mode: sap_post`` and local CLI."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    with psycopg.connect(DATABASE_URL) as conn:
        if purchase_order_id and purchase_order_id.strip():
            return post_single_order_to_sap(
                conn,
                purchase_order_id.strip(),
                post_to_sap=post_to_sap,
            )
        return post_ready_sap_invoices(
            conn,
            purchase_order_id=None,
            limit=limit,
            post_to_sap=post_to_sap,
        )


def run_sap_invoice_post_single(
    purchase_order_id: str,
    *,
    post_to_sap: bool = True,
) -> dict[str, Any]:
    """Entry point for dashboard ``POST /v1/walmart/sap-post``."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    with psycopg.connect(DATABASE_URL) as conn:
        return post_single_order_to_sap(
            conn,
            purchase_order_id,
            post_to_sap=post_to_sap,
            ignore_retry_limit=True,
        )
