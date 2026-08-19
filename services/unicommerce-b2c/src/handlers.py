"""Lambda entrypoints. Thin: parse event -> open DB -> run worker -> report.

Every handler alerts via SES on an unhandled crash and re-raises so the
invocation is marked failed in CloudWatch (EventBridge metrics stay
honest) -- same pattern as amazon-us-b2c's handlers.py.

fetch (orders+returns), postInvoices, postCreditNotes, pogrn, recon, and
the read-only dashboard API are implemented -- see
docs/migration/03-unicommerce-b2c.md for the full plan (inventorySync
was built then deliberately removed -- see
docs/superpowers/specs/2026-07-27-pogrn-inventorysync-design.md, it was
solving a problem that doesn't exist here: UC decrements batch inventory
itself on order fulfillment, and EC-FG<->EC-B2C / AMZ-* outward SAP
transfers are same-company internal bookkeeping, not a real removal
event -- a manual UC REMOVE call for those would have double-decremented
real inventory).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

# Lambda package root is ``src/``; local modules use ``from unicommerce.*``.
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from unicommerce import credit_notes, dashboard_queries, fetch_sync, pogrn, post_invoices, recon, sap_client, store
from unicommerce.alerts import send_alert

# boto3 isn't a declared dev/CI dependency (only present in the real Lambda
# runtime) -- lazy-import guarded the same way alerts.py's send_alert does,
# so importing this module doesn't require it locally/in CI.
_lambda_client = None


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        import boto3

        _lambda_client = boto3.client("lambda")
    return _lambda_client


logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# DATABASE_URL embeds a password; some driver errors echo the DSN back in
# the exception text, so scrub it before any crash detail goes out in an
# alert email (same precaution as amazon-us-b2c's handlers.py).
_DSN_CREDS_RE = re.compile(r"://[^/@\s]+:[^/@\s]+@")


def _scrub(text: str) -> str:
    return _DSN_CREDS_RE.sub("://***:***@", text)


def _run(label: str, fn):
    try:
        result = fn()
        logger.info("%s result: %s", label, result)
        return result
    except Exception as e:
        send_alert(f"{label} crashed", _scrub(f"{e}\n\n{traceback.format_exc()[-3000:]}"))
        raise


def fetch_handler(event, context):
    """{"mode": "orders"|"returns" (default "orders"), "max_orders"|"max_returns": int?,
    "from_date": "YYYY-MM-DD"?, "to_date": "YYYY-MM-DD"?, "channel_name": str? (orders only)}.
    from_date present -> explicit backfill window, cursor not advanced.
    Omitted -> cursor-driven incremental fetch. One handler covers both
    UC fetch flows (mirrors amazonFetch's mode input)."""
    ev = event or {}
    mode = (ev.get("mode") or "orders").strip().lower()

    def _go_orders():
        with store.connect() as conn:
            return fetch_sync.fetch_orders(
                conn,
                max_orders=ev.get("max_orders"),
                from_date=ev.get("from_date"),
                to_date=ev.get("to_date"),
                channel_name=ev.get("channel_name"),
            )

    def _go_returns():
        with store.connect() as conn:
            return fetch_sync.fetch_returns(
                conn,
                max_returns=ev.get("max_returns") or ev.get("max_orders"),
                from_date=ev.get("from_date"),
                to_date=ev.get("to_date"),
            )

    if mode == "returns":
        return _run("fetch_returns", _go_returns)
    return _run("fetch_orders", _go_orders)


def post_invoices_handler(event, context):
    """{"limit": 50?, "channel_name": str?, "start_date": "YYYY-MM-DD"?,
    "end_date": "YYYY-MM-DD"?} — bulk posting run (promote fetched->ready +
    recovery + verify + ready-order posting, same as the scheduled cron;
    the date range keys off the order_date, matching the Ops Center UX).
    {"uniware_id": str} — manual single-order retry, bypassing the limit.
    {"trigger": "manual_retry"} — set by dashboard_handler's async invoke;
    logs to unicommerce_sync_log as "retry_invoices" instead of
    "post_invoices" so the dashboard's sync-log panel can tell a manual
    dashboard retry apart from the scheduled cron run."""
    ev = event or {}

    def _go():
        with store.connect() as conn:
            if ev.get("uniware_id"):
                return post_invoices.post_one_order(conn, ev["uniware_id"])
            sync_mode = "retry_invoices" if ev.get("trigger") == "manual_retry" else "post_invoices"
            return post_invoices.post_ready_orders(
                conn,
                limit=int(ev.get("limit") or 50),
                channel_name=ev.get("channel_name"),
                start_date=ev.get("start_date"),
                end_date=ev.get("end_date"),
                sync_mode=sync_mode,
            )

    return _run("post_invoices", _go)


def post_credit_notes_handler(event, context):
    """{"limit": 50?, "start_date": "YYYY-MM-DD"?, "end_date": "YYYY-MM-DD"?,
    "channel_name": str?} — bulk posting run, date range keys off the
    parent order's order_date (same UX as the Ops Center date picker).
    {"uc_return_id": str} — manual single-return retry, bypassing the limit.
    {"trigger": "manual_retry"} — set by dashboard_handler's async invoke;
    logs to unicommerce_sync_log as "retry_cn" instead of
    "post_credit_notes", same distinction as post_invoices_handler."""
    ev = event or {}

    def _go():
        with store.connect() as conn:
            if ev.get("uc_return_id"):
                return credit_notes.post_one_credit_note(conn, ev["uc_return_id"])
            sync_mode = "retry_cn" if ev.get("trigger") == "manual_retry" else "post_credit_notes"
            return credit_notes.post_credit_notes(
                conn,
                limit=int(ev.get("limit") or 50),
                start_date=ev.get("start_date"),
                end_date=ev.get("end_date"),
                channel_name=ev.get("channel_name"),
                sync_mode=sync_mode,
            )

    return _run("post_credit_notes", _go)


def admin_debug_sap_invoice_handler(event, context):
    """TEMPORARY, invoke-only, read-only. {"doc_entry": int} -- GETs the
    live AR Invoice from SAP by DocEntry to compare its actual CardCode/
    DocumentLines against our locally stored payload_json, for debugging
    the credit-note CardCode/ItemCode mismatch errors."""
    ev = event or {}
    doc_entry = int(ev["doc_entry"])
    return _run("admin_debug_sap_invoice", lambda: sap_client.get_invoice_by_doc_entry(doc_entry))


def pogrn_handler(event, context):
    """{"limit": 20?, "dry_run": bool?} — bulk run (poll SAP transfers ->
    stage -> claim -> create UC PO+GRN). {"sap_doc_entry": int, "dry_run":
    bool?} — manual single-transfer retry/dry-run, bypassing the limit
    and watermark."""
    ev = event or {}

    def _go():
        with store.connect() as conn:
            dry_run = bool(ev.get("dry_run") or False)
            if ev.get("sap_doc_entry"):
                return pogrn.process_one_transfer(conn, int(ev["sap_doc_entry"]), dry_run=dry_run)
            return pogrn.process_ready_transfers(conn, limit=int(ev.get("limit") or 20), dry_run=dry_run)

    return _run("pogrn", _go)


def recon_handler(event, context):
    """{"limit": 200?, "start_date": "YYYY-MM-DD"?, "end_date": "YYYY-MM-DD"?}
    — bulk sweep re-verifying posted invoices/CNs against SAP for the
    current+previous calendar month (default window if dates omitted).
    {"uniware_id": str} — manual single-order retry, bypassing the window
    and limit. Read-only against SAP; only writes to our own Postgres."""
    ev = event or {}

    def _go():
        with store.connect() as conn:
            if ev.get("uniware_id"):
                return recon.recon_one_order(conn, ev["uniware_id"])
            return recon.refresh_recon(
                conn,
                limit=int(ev.get("limit") or recon.DEFAULT_LIMIT),
                start_date=ev.get("start_date"),
                end_date=ev.get("end_date"),
            )

    return _run("recon", _go)


# ── Dashboard HTTP API (read-only + thin retry actions) ───────────────────
# Behind HTTP API + Cognito JWT authorizer, same shape as amazon-us-b2c's
# dashboardApi. Serves the kiosk-frontend Unicommerce Ops components.

def _json_response(status_code: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _http_method(event: dict[str, Any]) -> str:
    http = (event.get("requestContext") or {}).get("http") or {}
    return str(http.get("method") or event.get("httpMethod") or "GET").upper()


def _path(event: dict[str, Any]) -> str:
    return str(event.get("rawPath") or event.get("path") or "")


def _query_params(event: dict[str, Any]) -> dict[str, str]:
    params = event.get("queryStringParameters") or {}
    return {str(k): str(v) for k, v in params.items() if v is not None} if isinstance(params, dict) else {}


def _invoke_retry_batch(function_arn_env: str, q: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "trigger": "manual_retry",
        "limit": dashboard_queries._clamp_limit(q.get("limit")),
        "start_date": q.get("start_date") or None,
        "end_date": q.get("end_date") or None,
    }
    _get_lambda_client().invoke(
        FunctionName=os.environ[function_arn_env],
        InvocationType="Event",
        Payload=json.dumps(payload),
    )
    return {"accepted": True, **payload}


def dashboard_handler(event, context):
    method = _http_method(event)
    if method == "OPTIONS":
        return _json_response(200, {"ok": True})

    path = _path(event)
    q = _query_params(event)

    try:
        # ── thin write actions (retry) ──
        # Batch retries are handed off async to the scheduled Lambda (900s
        # budget) instead of running inline here -- this route sits behind
        # httpApi, which hard-caps its integration timeout at 30s regardless
        # of this function's own configured timeout, so a batch large enough
        # to run long could truncate mid-run. Single-order retries stay
        # synchronous: one row, one SAP call, comfortably fits 30s and the
        # caller gets a real result back immediately.
        if path.endswith("/v1/unicommerce/retry-invoices") and method == "POST":
            if (q.get("uniware_id") or "").strip():
                with store.connect() as conn:
                    return _json_response(200, post_invoices.post_one_order(conn, q["uniware_id"].strip()))
            return _json_response(202, _invoke_retry_batch("POST_INVOICES_FUNCTION_ARN", q))

        if path.endswith("/v1/unicommerce/retry-cn") and method == "POST":
            if (q.get("uc_return_id") or "").strip():
                with store.connect() as conn:
                    return _json_response(200, credit_notes.post_one_credit_note(conn, q["uc_return_id"].strip()))
            return _json_response(202, _invoke_retry_batch("POST_CREDIT_NOTES_FUNCTION_ARN", q))

        if path.endswith("/v1/unicommerce/order-detail") and not (q.get("uniware_id") or "").strip():
            return _json_response(400, {"error": "uniware_id is required"})

        # ── read-only queries ──
        with store.connect() as conn:
            if path.endswith("/v1/unicommerce/stats"):
                return _json_response(200, dashboard_queries.fetch_stats(conn))

            if path.endswith("/v1/unicommerce/orders"):
                return _json_response(
                    200,
                    dashboard_queries.fetch_orders(
                        conn,
                        limit=dashboard_queries._clamp_limit(q.get("limit")),
                        offset=dashboard_queries._clamp_offset(q.get("offset")),
                        pipeline_status=q.get("pipeline_status") or None,
                        channel_name=q.get("channel_name") or None,
                        uniware_id=(q.get("uniware_id") or "").strip() or None,
                        order_date_from=q.get("order_date_from") or None,
                        order_date_to=q.get("order_date_to") or None,
                        sort_by=q.get("sort_by") or None,
                        sort_dir=q.get("sort_dir") or None,
                    ),
                )

            if path.endswith("/v1/unicommerce/order-detail"):
                uid = (q.get("uniware_id") or "").strip()
                detail = dashboard_queries.fetch_order_detail(conn, uid)
                if detail["header"] is None:
                    return _json_response(404, {"error": "Order not found", "uniware_id": uid})
                return _json_response(200, detail)

            if path.endswith("/v1/unicommerce/returns"):
                return _json_response(
                    200,
                    dashboard_queries.fetch_returns(
                        conn, limit=dashboard_queries._clamp_limit(q.get("limit")), status=q.get("status") or None
                    ),
                )

            if path.endswith("/v1/unicommerce/sync-log"):
                items = dashboard_queries.fetch_sync_log(
                    conn, limit=dashboard_queries._clamp_limit(q.get("limit"), default=20), mode=q.get("mode") or None
                )
                return _json_response(200, {"items": items})

            if path.endswith("/v1/unicommerce/recon-gaps"):
                return _json_response(
                    200,
                    dashboard_queries.fetch_recon_gaps(
                        conn, limit=dashboard_queries._clamp_limit(q.get("limit")),
                        min_diff=float(q.get("min_diff") or 1.0),
                    ),
                )

        return _json_response(404, {"error": "Not found", "path": path})
    except Exception as exc:
        scrubbed = _scrub(str(exc))
        logger.error("dashboard_api_error path=%s error=%s", path, scrubbed)
        return _json_response(500, {"error": scrubbed})
