"""Lambda entrypoints for Amazon UK B2C. Thin: parse event -> run worker -> report.


  fetch_handler              SP-API orders + shipment report -> amazonuk.* (RDS)
  post_invoices_handler      Shipped, not-yet-posted orders -> SAP B1 AR Invoices
  fetch_refunds_handler      Finances API RELEASED refunds -> amazonuk.* (RDS)
  post_credit_notes_handler  Not-yet-posted refunds -> SAP B1 AR Credit Memos
  dashboard_handler          Cognito-protected READ-ONLY /api/* (static frontend lives on S3, calls this cross-origin)

Fetch and post run ONLY from their EventBridge schedules (serverless.yml) —
the dashboard cannot trigger them. The event modes below (backfill, single
order, retry) are AWS-side break-glass, invokable only with AWS credentials.

Fetch and post are separate Lambdas on purpose: the order fetch paces at
~180 s/page and the shipment report can poll for ~10 min, so sharing one 900 s
budget with SAP posting meant a slow fetch could starve posting entirely.

Batch handlers alert via SES on an unhandled crash and re-raise, so the
invocation is marked failed in CloudWatch (EventBridge metrics stay honest).
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path

from khanal_shared.logging import configure_logging

# Lambda package root is ``src/``; local modules use ``from amazon_uk.*``.
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from amazon_uk import dashboard, post_credit_notes_to_sap, post_to_sap, sync_amazonuk, sync_refunds  # noqa: E402
from amazon_uk.alerts import scrub, send_alert, send_report  # noqa: E402

configure_logging()  # LOG_LEVEL-aware; handles the Lambda root-logger pitfall
logger = logging.getLogger("amz_sync")


def _run(label: str, fn):
    try:
        result = fn()
        logger.info("%s result: %s", label, result)
    except Exception as e:
        send_alert(f"{label} crashed", scrub(f"{e}\n\n{traceback.format_exc()[-3000:]}"))
        raise
    send_report(label, result)  # best-effort; never raises
    return result


def fetch_handler(event, context):
    """modes: incremental (default) | backfill.

    incremental: watermark sync (orders updated since the last run, window
    capped at 48 h so a gap drains over consecutive runs, + trailing
    shipment report). The scheduled event is just {"mode": "incremental"}.
    Optional {"initial_lookback_days": N} only shortens the very first run.

    backfill: {"mode": "backfill", "from_date": "YYYY-MM-DD",
    "to_date": "YYYY-MM-DD"} — manual createdAfter/Before range sync.
    """
    ev = event or {}
    mode = ev.get("mode", "incremental")

    def _go():
        if mode == "incremental":
            lookback = ev.get("initial_lookback_days")
            return sync_amazonuk.sync_incremental(int(lookback) if lookback is not None else None)
        if mode == "backfill":
            from_date, to_date = ev.get("from_date"), ev.get("to_date")
            if not from_date or not to_date:
                raise ValueError("backfill requires from_date and to_date (YYYY-MM-DD)")
            return sync_amazonuk.sync(from_date, to_date)
        raise ValueError(f"Unknown fetch mode: {mode}")

    return _run(f"fetch[{mode}]", _go)


def post_invoices_handler(event, context):
    """{} or {"mode": "post"}         — post every Shipped, not-yet-posted order.
    {"mode": "retry"}                 — re-post every recorded failure.
    {"amazon_order_id": "..."}        — post exactly that one order.
    """
    ev = event or {}
    mode = ev.get("mode", "post")

    def _go():
        order_id = (ev.get("amazon_order_id") or "").strip()
        if order_id:
            return post_to_sap.post_single_order(order_id)
        if mode == "retry":
            return post_to_sap.retry_failed_orders()
        if mode == "post":
            return post_to_sap.post_all_shipped_orders()
        raise ValueError(f"Unknown post mode: {mode}")

    return _run(f"post_invoices[{mode}]", _go)


def fetch_refunds_handler(event, context):
    """modes: incremental (default) | backfill.

    incremental: watermark sync (RELEASED refunds posted since the last
    run, window capped at 180 days by Finances' own retention). The
    scheduled event is just {"mode": "incremental"}.
    Optional {"initial_lookback_days": N} only shortens the very first run.

    backfill: {"mode": "backfill", "from_date": "YYYY-MM-DD",
    "to_date": "YYYY-MM-DD"} — manual postedAfter/Before range sync.
    """
    ev = event or {}
    mode = ev.get("mode", "incremental")

    def _go():
        if mode == "incremental":
            lookback = ev.get("initial_lookback_days")
            return sync_refunds.sync_incremental(int(lookback) if lookback is not None else None)
        if mode == "backfill":
            from_date, to_date = ev.get("from_date"), ev.get("to_date")
            if not from_date or not to_date:
                raise ValueError("backfill requires from_date and to_date (YYYY-MM-DD)")
            return sync_refunds.sync(from_date, to_date)
        raise ValueError(f"Unknown fetch mode: {mode}")

    return _run(f"fetch_refunds[{mode}]", _go)


def post_credit_notes_handler(event, context):
    """{} or {"mode": "post"}         — post every not-yet-posted RELEASED refund.
    {"mode": "retry"}                 — re-post every recorded failure.
    {"transaction_id": "..."}         — post exactly that one refund transaction.
    """
    ev = event or {}
    mode = ev.get("mode", "post")

    def _go():
        transaction_id = (ev.get("transaction_id") or "").strip()
        if transaction_id:
            return post_credit_notes_to_sap.post_single_refund(transaction_id)
        if mode == "retry":
            return post_credit_notes_to_sap.retry_failed_refunds()
        if mode == "post":
            return post_credit_notes_to_sap.post_all_refunds()
        raise ValueError(f"Unknown post mode: {mode}")

    return _run(f"post_credit_notes[{mode}]", _go)


# ── Dashboard HTTP API ─────────────────────────────────────────────────────
# Static frontend is served separately from an S3 website bucket
# (serverless.yml AmazonUkDashboardSiteBucket) and calls this cross-origin;
# only /api/{proxy+} is routed here (see serverless.yml), so every request
# this handler sees is already an API call.

def _api_response(status: int, body: str) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": body}


def dashboard_handler(event, context):
    """API Gateway HTTP API (payload v2.0): Cognito-protected /api/* only."""
    http = (event.get("requestContext") or {}).get("http") or {}
    method = str(http.get("method") or "GET").upper()
    path = str(http.get("path") or event.get("rawPath") or "/")

    try:
        status, body_str = dashboard.dispatch(method, path, event.get("rawQueryString") or "")
        return _api_response(status, body_str)
    except Exception as exc:
        scrubbed = scrub(str(exc))
        logger.error("dashboard_api_error path=%s error=%s", path, scrubbed)
        return _api_response(500, json.dumps({"error": scrubbed}))
