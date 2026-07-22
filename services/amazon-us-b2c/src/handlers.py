"""Lambda entrypoints. Thin: parse event -> open DB -> run worker -> report.

Every handler alerts via SES on an unhandled crash and re-raises so the
invocation is marked failed in CloudWatch (EventBridge metrics stay
honest).
"""

from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Lambda package root is ``src/``; local modules use ``from amazon_us.*``.
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from amazon_us import dashboard_queries, fetch_sync, post_credit_notes, post_invoices, recon, store
from amazon_us.alerts import send_alert
from amazon_us.fba_ship_sync import sync_ship_dates

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# DATABASE_URL embeds a password (postgresql://user:pass@host/...); some
# driver errors echo the DSN back in the exception text, so scrub it before
# any crash detail goes out in an alert email.
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
    """modes: incremental (default) | report | backfill | seed_watermark.
    report/backfill events: {"mode": "report", "from_date": "YYYY-MM-DD",
    "to_date": "YYYY-MM-DD", "lookback_days": 30?}
    seed_watermark event: {"mode": "seed_watermark", "when": "YYYY-MM-DD HH:MM:SS"?}
    -- sets the incremental watermark directly, defaulting to now. Run once
    before ever enabling the incremental schedule (a missing watermark makes
    sync_incremental try a 30-day per-order catch-up that can exhaust the
    900s Lambda timeout without finishing)."""
    mode = (event or {}).get("mode", "incremental")

    def _go():
        with store.connect() as conn:
            if mode == "incremental":
                return fetch_sync.sync_incremental(conn)
            if mode in ("report", "backfill"):
                from_date = event.get("from_date") or date.today().replace(day=1).isoformat()
                to_date = event.get("to_date") or date.today().isoformat()
                return fetch_sync.sync_via_report(conn, from_date, to_date, event.get("lookback_days"))
            if mode == "seed_watermark":
                value = fetch_sync.seed_watermark(conn, event.get("when"))
                return {"watermark_set_to": value}
            raise ValueError(f"Unknown fetch mode: {mode}")

    return _run(f"fetch[{mode}]", _go)


def fba_ship_sync_handler(event, context):
    """Default window: last 21 days (Pacific). Manual: {"from_date", "to_date"}."""

    def _go():
        from_date = (event or {}).get("from_date") or (date.today() - timedelta(days=21)).isoformat()
        to_date = (event or {}).get("to_date") or date.today().isoformat()
        with store.connect() as conn:
            return sync_ship_dates(conn, from_date, to_date)

    return _run("fba_ship_sync", _go)


def post_invoices_handler(event, context):
    """{"limit": 200?, "dry_run": false?, "mode": "post"|"retry"}"""

    def _go():
        ev = event or {}
        with store.connect() as conn:
            if ev.get("mode") == "retry":
                return post_invoices.retry_failed_orders(conn, limit=int(ev.get("limit") or 50))
            return post_invoices.post_ready_orders(
                conn, limit=int(ev.get("limit") or post_invoices.DEFAULT_LIMIT),
                dry_run=bool(ev.get("dry_run")),
            )

    return _run("post_invoices", _go)


def post_credit_notes_handler(event, context):
    """Default window: last 45 days (Finances PostedDate lags MTR).
    Manual: {"posted_after": ISO, "posted_before": ISO, "dry_run": bool}."""

    def _go():
        ev = event or {}
        posted_after = ev.get("posted_after") or (
            datetime.now(timezone.utc) - timedelta(days=45)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with store.connect() as conn:
            return post_credit_notes.post_refunds(
                conn, posted_after, ev.get("posted_before"), dry_run=bool(ev.get("dry_run"))
            )

    return _run("post_credit_notes", _go)


def recon_handler(event, context):
    """modes: invariants (default) | mtr | close_stuck_run.
    mtr event: {"mode": "mtr", "s3_bucket": ..., "s3_key": ...,
    "period_from": "YYYY-MM-DD", "period_to": "YYYY-MM-DD"}.
    close_stuck_run event: {"mode": "close_stuck_run", "run_id": int,
    "status": "error"?, "error": str?} -- a handler that dies via hard
    timeout (Sandbox.Timedout) never reaches finish_sync_log, leaving its
    amazon_us_sync_log row permanently stuck at status='running'. This
    closes it out manually; status defaults to "error"."""
    mode = (event or {}).get("mode", "invariants")

    def _go():
        with store.connect() as conn:
            if mode == "invariants":
                return recon.run_invariants(conn)
            if mode == "mtr":
                return recon.run_mtr_recon(
                    conn, event["s3_bucket"], event["s3_key"], event["period_from"], event["period_to"]
                )
            if mode == "close_stuck_run":
                run_id = int(event["run_id"])
                store.finish_sync_log(
                    conn, run_id, event.get("status", "error"), error=event.get("error")
                )
                return {"closed_run_id": run_id}
            raise ValueError(f"Unknown recon mode: {mode}")

    return _run(f"recon[{mode}]", _go)


# ── Dashboard HTTP API (read-only + a couple of manual-retry actions) ──────

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


def dashboard_handler(event, context):
    method = _http_method(event)
    if method == "OPTIONS":
        return _json_response(200, {"ok": True})

    path = _path(event)
    q = _query_params(event)

    try:
        if path.endswith("/v1/amazon/retry-invoices") and method == "POST":
            with store.connect() as conn:
                limit = int(q.get("limit") or 50)
                return _json_response(200, post_invoices.retry_failed_orders(conn, limit=limit))

        if path.endswith("/v1/amazon/retry-cn") and method == "POST":
            with store.connect() as conn:
                posted_after = q.get("posted_after") or (
                    datetime.now(timezone.utc) - timedelta(days=45)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                dry_run = (q.get("dry_run") or "").lower() in ("1", "true")
                return _json_response(
                    200,
                    post_credit_notes.post_refunds(
                        conn, posted_after, q.get("posted_before"), dry_run=dry_run
                    ),
                )

        if path.endswith("/v1/amazon/order-detail") and not (q.get("amazon_order_id") or "").strip():
            return _json_response(400, {"error": "amazon_order_id is required"})

        with store.connect() as conn:
            if path.endswith("/v1/amazon/stats"):
                return _json_response(200, dashboard_queries.fetch_stats(conn))

            if path.endswith("/v1/amazon/orders"):
                limit = dashboard_queries._clamp_limit(q.get("limit"))
                offset = dashboard_queries._clamp_offset(q.get("offset"))
                return _json_response(
                    200,
                    dashboard_queries.fetch_orders(
                        conn,
                        limit=limit,
                        offset=offset,
                        stage=q.get("stage") or None,
                        amazon_order_id=(q.get("amazon_order_id") or "").strip() or None,
                        order_date_from=q.get("order_date_from") or None,
                        order_date_to=q.get("order_date_to") or None,
                        sort_by=q.get("sort_by") or None,
                        sort_dir=q.get("sort_dir") or None,
                    ),
                )

            if path.endswith("/v1/amazon/order-detail"):
                oid = (q.get("amazon_order_id") or "").strip()
                detail = dashboard_queries.fetch_order_detail(conn, oid)
                if detail["header"] is None:
                    return _json_response(404, {"error": "Order not found", "amazon_order_id": oid})
                return _json_response(200, detail)

            if path.endswith("/v1/amazon/invoice-failures"):
                limit = dashboard_queries._clamp_limit(q.get("limit"), default=50)
                return _json_response(200, dashboard_queries.fetch_invoice_failures(conn, limit=limit))

            if path.endswith("/v1/amazon/cn-log"):
                limit = dashboard_queries._clamp_limit(q.get("limit"))
                return _json_response(
                    200, dashboard_queries.fetch_cn_log(conn, limit=limit, status=q.get("status") or None)
                )

            if path.endswith("/v1/amazon/sync-log"):
                limit = dashboard_queries._clamp_limit(q.get("limit"), default=20)
                items = dashboard_queries.fetch_sync_log(conn, limit=limit, mode=q.get("mode") or None)
                return _json_response(200, {"items": items})

        return _json_response(404, {"error": "Not found", "path": path})
    except Exception as exc:
        logger.error("dashboard_api_error path=%s error=%s", path, exc)
        return _json_response(500, {"error": str(exc)})
