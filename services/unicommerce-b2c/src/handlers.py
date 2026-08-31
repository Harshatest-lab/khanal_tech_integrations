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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Lambda package root is ``src/``; local modules use ``from unicommerce.*``.
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from unicommerce import constants, credit_notes, dashboard_queries, fetch_sync, flex_fetch, flex_refunds, infra_status, numatcard_backfill, pogrn, post_invoices, recon, store
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
        # Lambda's runtime JSON-marshals whatever gets returned here --
        # a date/datetime/Decimal anywhere in it crashes that step even
        # though the handler's actual work (e.g. a DB write) already
        # committed by this point. Round-trip through json.dumps(...,
        # default=str) so the return value is always marshal-safe, same
        # convention dashboard_handler's _json_response already uses.
        # Confirmed real 2026-08-25: a delete handler's response (raw DB
        # rows with date columns) crashed here post-commit, losing the
        # caller's only copy of what was deleted until it was recovered
        # from the log line just above.
        return json.loads(json.dumps(result, default=str))
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


def debug_find_invoice_by_order_handler(event, context):
    """{"uniware_ids": [str, ...]}. Read-only: checks SAP directly (via
    sap_client.find_invoice_by_uc_order_id -- HANA primary, Service Layer
    Comments-filter fallback) for whether each order ALREADY has a real
    posted invoice, independent of what our own DB's pipeline_status says.
    One-off: built 2026-08-31 to cross-check "dead" orders after HANA
    connectivity was found broken service-wide (see the HANA_USE_SSM fix
    same day) -- with the primary lookup down, the idempotency guard in
    _post_order was relying solely on the SL fallback, and a user manually
    found at least one "dead" order that SAP already had a real invoice
    for. No writes; safe to run against any order list. Invoke manually
    (aws lambda invoke)."""
    from unicommerce import sap_client

    ev = event or {}
    uniware_ids = ev.get("uniware_ids") or []

    def _go():
        found = []
        not_found = []
        errors = []
        for uid in uniware_ids:
            try:
                existing = sap_client.find_invoice_by_uc_order_id(uid)
                if existing:
                    found.append({"uniware_id": uid, "doc_entry": existing["doc_entry"], "doc_num": existing["doc_num"]})
                else:
                    not_found.append(uid)
            except Exception as exc:
                errors.append({"uniware_id": uid, "error": _scrub(str(exc))})
        return {"checked": len(uniware_ids), "found": found, "not_found": not_found, "errors": errors}

    return _run("debug_find_invoice_by_order", _go)


def numatcard_backfill_handler(event, context):
    """{"limit": int? (default 10), "offset": int? (default 0), "apply": bool?
    (default false -- dry-run only)}. One-off: backfill NumAtCard onto
    already-posted historical invoices, batched. Not scheduled -- invoke
    manually (aws lambda invoke), same network path the real posting
    Lambda uses (confirmed live 2026-08-20 this SAP call fails from outside
    AWS but succeeds from here)."""
    ev = event or {}
    limit = int(ev.get("limit") or 10)
    offset = int(ev.get("offset") or 0)
    apply = bool(ev.get("apply") or False)

    def _go():
        return numatcard_backfill.run_backfill(limit=limit, offset=offset, apply=apply)

    return _run("numatcard_backfill", _go)


def debug_batch_inventory_handler(event, context):
    """{"warehouse_code": str, "item_codes": [str, ...]} -- read-only HANA
    batch-inventory lookup (unicommerce.hana_access.
    bulk_batch_inventory_by_warehouse), same query post_invoices.py's own
    FEFO allocation uses. Not scheduled, ops tooling only -- invoke
    manually to check what SAP actually has on hand for an item/warehouse
    when a batch_hold/shortage looks wrong (e.g. real stock exists but a
    posting attempt reported none)."""
    from unicommerce import hana_access

    ev = event or {}
    warehouse_code = ev.get("warehouse_code")
    item_codes = ev.get("item_codes") or []
    if not warehouse_code or not item_codes:
        return {"status": "error", "error": "warehouse_code and item_codes are required"}

    def _go():
        return {"rows": hana_access.bulk_batch_inventory_by_warehouse(warehouse_code, item_codes)}

    return _run("debug_batch_inventory", _go)


def flex_fetch_handler(event, context):
    """{"lookback_days": int? (default 2)} -- pulls the Amazon SP-API
    fulfilled-shipments report, filters to PRCJ, stages synthetic orders
    into unicommerce_orders/order_lines (channel_name='AMAZON_FLEX',
    pipeline_status='fetched'). Posting itself happens on the next
    post_invoices_handler cycle, same as every other channel -- this
    handler only fetches/stages."""
    ev = event or {}
    lookback_days = int(ev.get("lookback_days") or 2)

    def _go():
        return flex_fetch.fetch_flex_shipments(lookback_days=lookback_days)

    return _run("flex_fetch", _go)


def flex_refunds_handler(event, context):
    """{"posted_after": "YYYY-MM-DDTHH:MM:SSZ"?, "posted_before": str?,
    "dry_run": bool? (default false)} -- pulls Amazon Finances API refund
    events, matches each to its already-posted AMAZON_FLEX invoice, and
    posts a SAP AR Credit Note (WithoutInventoryMovement, same pattern as
    amazon-us-b2c). posted_after defaults to a rolling last-45-days window
    when omitted -- same default amazon-us-b2c's own scheduled refund
    poster uses, since Finances PostedDate lags the actual refund by up
    to 1-2 weeks and the duplicate/amendment guards make re-processing
    the same window free. dry_run logs what would post without writing
    to SAP or the local log table. v1 does not handle CFG bundle refunds
    or shipping-only refunds -- see flex_credit_note_payload.py."""
    ev = event or {}
    posted_after = ev.get("posted_after") or (
        datetime.now(timezone.utc) - timedelta(days=45)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _go():
        with store.connect() as conn:
            return flex_refunds.post_flex_refunds(
                conn, posted_after=posted_after, posted_before=ev.get("posted_before"), dry_run=bool(ev.get("dry_run") or False),
            )

    return _run("flex_refunds", _go)


def post_invoices_handler(event, context):
    """{"limit": 300?, "channel_name": str?, "start_date": "YYYY-MM-DD"?,
    "end_date": "YYYY-MM-DD"?} — bulk posting run (promote fetched->ready +
    recovery + verify + ready-order posting, same as the scheduled cron;
    the date range keys off the order_date, matching the Ops Center UX).
    Default bumped 50->300 on 2026-08-21: promote_fetched_to_ready has no
    channel filter and orders oldest-fetched-first, so channels with a
    long-lived stale 'fetched' backlog (orders that never complete
    upstream in Unicommerce) were starving newer, smaller channels like
    AMAZON_FLEX out of every cron cycle's batch. 300 covers the current
    ~200-row cross-channel backlog with headroom; cost scales with actual
    eligible rows, not this ceiling, and the 900s Lambda timeout has
    plenty of margin against the 30-min cron cadence.
    start_date/end_date are unset by default (no date filter) as of
    2026-08-28 -- they used to default to a rolling last-14-days window
    to fight promote_fetched_to_ready's oldest-first starvation (see
    2026-08-21/2026-08-24 history below), but that only capped the
    symptom: confirmed live 2026-08-28 that with the 14-day default, 405
    'fetched' rows sat inside the window against the 300-row limit, so
    the ~105 newest never got scanned at all, on top of anything older
    than 14 days being dropped from the sweep forever as the window slid
    forward. The actual starvation cause -- ordering by order_date ASC,
    so a chronically-stuck row permanently occupies the front of the
    queue -- is fixed at the root in get_fetched_orders/
    promote_fetched_to_ready (ordered by last_checked_live_at instead, so
    a row that keeps failing cycles to the back after each live recheck).
    With that fixed, narrowing the default window no longer serves a
    purpose and only hid real backlog from the scheduled cron; still
    accept both as optional overrides for a manually scoped Ops Center
    rerun.
    {"uniware_id": str, "force": bool?} — manual single-order retry,
    bypassing the limit. Enforces POSTING_CUTOFF_DATE same as the
    scheduled cron unless force=true is explicitly passed (see
    post_invoices.post_one_order's docstring for why this guard exists).
    {"trigger": "manual_retry"} — set by dashboard_handler's async invoke;
    logs to unicommerce_sync_log as "retry_invoices" instead of
    "post_invoices" so the dashboard's sync-log panel can tell a manual
    dashboard retry apart from the scheduled cron run.
    {"reset_dead_ids": [str, ...]} — manual unstick for orders sitting at
    'dead' (batch-hold 48h/10-attempt cap) or still at 'batch_hold' (see
    store.reset_dead_orders_for_retry), once the underlying stock
    shortage has cleared -- also skips batch_hold's own 30-min recovery
    cooldown for exactly these ids. Resets them to 'ready' with a fresh
    attempt/hold budget, then runs the normal ready-order sweep in
    the same invocation so they get retried immediately instead of
    waiting for the next cron tick.
    {"delete_pre_cutoff_ready": true} — one-off cleanup, user-requested
    2026-08-25: permanently deletes every 'ready' order with order_date
    before POSTING_CUTOFF_DATE (see store.delete_pre_cutoff_ready_orders
    -- these can never be posted, get_ready_orders' own cutoff excludes
    them forever). Returns every deleted row's full data in the response
    -- the caller must persist that as the only remaining record, since
    this is irreversible and nothing else keeps a copy once it commits."""
    ev = event or {}
    start_date = ev.get("start_date")

    def _go():
        with store.connect() as conn:
            if ev.get("uniware_id"):
                return post_invoices.post_one_order(conn, ev["uniware_id"], force=bool(ev.get("force") or False))
            if ev.get("delete_pre_cutoff_ready"):
                deleted = store.delete_pre_cutoff_ready_orders(conn, constants.POSTING_CUTOFF_DATE)
                return {"deleted_count": len(deleted), "deleted_orders": deleted}
            reset_count = store.reset_dead_orders_for_retry(conn, ev.get("reset_dead_ids") or [])
            sync_mode = "retry_invoices" if (ev.get("trigger") == "manual_retry" or ev.get("reset_dead_ids")) else "post_invoices"
            result = post_invoices.post_ready_orders(
                conn,
                limit=int(ev.get("limit") or 300),
                channel_name=ev.get("channel_name"),
                start_date=start_date,
                end_date=ev.get("end_date"),
                sync_mode=sync_mode,
            )
            result["reset_dead"] = reset_count
            return result

    return _run("post_invoices", _go)


def admin_debug_get_cn_handler(event, context):
    """TEMPORARY, invoke-only, read-only. {"doc_entry": int} -- GETs a
    live SAP Credit Note by DocEntry, raw. Built 2026-08-28 to inspect
    the orphaned Credit Notes (DocEntry 12717-12732) created when 12
    dead-CN retries all hit the 2026-08-19 response-verification guard
    (credit_notes.py's U_UCReturnID check, PR from the DocEntry
    cross-attribution incident) -- every retry's POST response came back
    with U_UCReturnID=None, so nothing got recorded locally even though
    SAP assigned a real, incrementing DocEntry each time. This checks
    whether SAP actually persisted the field (a GET-vs-POST-response
    discrepancy) or never registered it at all for Credit Notes. Remove
    once this investigation is done."""
    from unicommerce import sap_client

    ev = event or {}
    doc_entry = ev.get("doc_entry")
    if not doc_entry:
        return {"error": "doc_entry is required"}

    def _go():
        return sap_client.get_credit_note_by_doc_entry(int(doc_entry))

    return _run("admin_debug_get_cn", _go)


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

        if path.endswith("/v1/unicommerce/infra"):
            return _json_response(200, infra_status.fetch_infra_status(os.environ.get("STAGE") or "dev"))

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
