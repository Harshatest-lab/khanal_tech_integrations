"""HTTP API for Walmart orders dashboard."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from khanal_shared.logging import configure_logging, log_json

from walmart.dashboard_api import (
    fetch_order_stats,
    fetch_orders,
    fetch_order_detail,
    fetch_return_detail,
    fetch_returns,
    fetch_returns_sync_log,
    fetch_sap_invoice_sync_detail,
    fetch_sap_invoice_failures,
    fetch_sync_log,
    get_db_connection,
    json_response,
    _clamp_limit,
    _clamp_offset,
)
from walmart.sap_invoice_sync import run_sap_invoice_post_single, run_sap_invoice_retry_sync
from walmart.sap_credit_note_sync import (
    cancel_posted_credit_note,
    mark_credit_note_manual,
    preview_credit_note_payload,
    run_sap_cn_post_single,
    unmark_credit_note,
)

logger = logging.getLogger(__name__)

_RETURN_DETAIL_PATH = re.compile(r"/v1/walmart/returns/([^/]+)$")


def _http_method(event: dict[str, Any]) -> str:
    ctx = event.get("requestContext") or {}
    http = ctx.get("http") or {}
    return str(http.get("method") or event.get("httpMethod") or "GET").upper()


def _path(event: dict[str, Any]) -> str:
    return str(event.get("rawPath") or event.get("path") or "")


def _query_params(event: dict[str, Any]) -> dict[str, str]:
    params = event.get("queryStringParameters") or {}
    if isinstance(params, dict):
        return {str(k): str(v) for k, v in params.items() if v is not None}
    return {}


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    configure_logging("INFO")

    method = _http_method(event)
    if method == "OPTIONS":
        return json_response(200, {"ok": True})

    path = _path(event)
    q = _query_params(event)

    try:
        if path.endswith("/v1/walmart/sap-items"):
            from walmart.sap_hana import fetch_fg_items

            return json_response(200, fetch_fg_items())

        if path.endswith("/v1/walmart/sap-post") and method == "POST":
            po = (q.get("purchase_order_id") or "").strip()
            if not po:
                return json_response(400, {"error": "purchase_order_id is required"})
            result = run_sap_invoice_post_single(po)
            if result.get("status") == "error":
                return json_response(404, result)
            if result.get("status") == "failed":
                return json_response(502, result)
            return json_response(200, result)

        if path.endswith("/v1/walmart/sap-retry") and method == "POST":
            po = (q.get("purchase_order_id") or "").strip() or None
            limit_raw = (q.get("limit") or "").strip()
            limit = int(limit_raw) if limit_raw.isdigit() else None
            kwargs: dict[str, Any] = {
                "purchase_order_id": po,
                "post_to_sap": True,
            }
            if limit is not None:
                kwargs["limit"] = limit
            result = run_sap_invoice_retry_sync(**kwargs)
            return json_response(200, {"ok": True, **result})

        if path.endswith("/v1/walmart/sap-cn-post") and method == "POST":
            rid = (q.get("return_order_id") or "").strip()
            if not rid:
                return json_response(400, {"error": "return_order_id is required"})
            result = run_sap_cn_post_single(rid)
            if result.get("status") == "error":
                return json_response(404, result)
            if result.get("status") == "failed":
                return json_response(502, result)
            return json_response(200, result)

        if path.endswith("/v1/walmart/sap-cn-preview"):
            rid = (q.get("return_order_id") or "").strip()
            if not rid:
                return json_response(400, {"error": "return_order_id is required"})
            try:
                result = preview_credit_note_payload(rid)
            except Exception as exc:
                return json_response(502, {"ok": False, "error": str(exc), "return_order_id": rid})
            if not result.get("ok"):
                return json_response(400, result)
            return json_response(200, result)

        if path.endswith("/v1/walmart/sap-cn-mark-manual") and method == "POST":
            rid = (q.get("return_order_id") or "").strip()
            if not rid:
                return json_response(400, {"error": "return_order_id is required"})
            result = mark_credit_note_manual(rid)
            if not result.get("ok"):
                return json_response(400, result)
            return json_response(200, result)

        if path.endswith("/v1/walmart/sap-cn-unmark") and method == "POST":
            rid = (q.get("return_order_id") or "").strip()
            if not rid:
                return json_response(400, {"error": "return_order_id is required"})
            result = unmark_credit_note(rid)
            if not result.get("ok"):
                return json_response(400, result)
            return json_response(200, result)

        if path.endswith("/v1/walmart/sap-cn-cancel") and method == "POST":
            rid = (q.get("return_order_id") or "").strip()
            if not rid:
                return json_response(400, {"error": "return_order_id is required"})
            result = cancel_posted_credit_note(rid)
            if not result.get("ok"):
                return json_response(502 if "SAP" in str(result.get("error") or "") else 400, result)
            return json_response(200, result)

        with get_db_connection() as conn:
            if path.endswith("/v1/walmart/stats"):
                return json_response(200, fetch_order_stats(conn))

            if path.endswith("/v1/walmart/sync-log"):
                limit = _clamp_limit(q.get("limit"), default=20)
                return json_response(200, {"items": fetch_sync_log(conn, limit=limit)})

            if path.endswith("/v1/walmart/returns/sync-log"):
                limit = _clamp_limit(q.get("limit"), default=25)
                return json_response(
                    200, {"items": fetch_returns_sync_log(conn, limit=limit)}
                )

            if path.endswith("/v1/walmart/returns"):
                limit = _clamp_limit(q.get("limit"))
                offset = _clamp_offset(q.get("offset"))
                page = fetch_returns(
                    conn,
                    limit=limit,
                    offset=offset,
                    return_order_id=(q.get("return_order_id") or "").strip() or None,
                    purchase_order_id=(q.get("purchase_order_id") or "").strip() or None,
                    status=q.get("status") or None,
                    current_refund_status=q.get("current_refund_status") or None,
                    current_delivery_status=q.get("current_delivery_status") or None,
                    credit_note_status=q.get("credit_note_status") or None,
                    return_date_from=q.get("return_date_from") or None,
                    return_date_to=q.get("return_date_to") or None,
                )
                return json_response(200, page)

            return_detail_match = _RETURN_DETAIL_PATH.search(path)
            if return_detail_match:
                path_params = event.get("pathParameters") or {}
                return_order_id = (
                    str(path_params.get("returnOrderId") or return_detail_match.group(1) or "")
                    .strip()
                )
                if not return_order_id or return_order_id == "sync-log":
                    return json_response(404, {"error": "Not found", "path": path})
                detail = fetch_return_detail(conn, return_order_id)
                if detail is None:
                    return json_response(
                        404,
                        {"error": "Return not found", "return_order_id": return_order_id},
                    )
                return json_response(200, detail)

            if path.endswith("/v1/walmart/order-detail"):
                po = (q.get("purchase_order_id") or "").strip()
                if not po:
                    return json_response(400, {"error": "purchase_order_id is required"})
                detail = fetch_order_detail(conn, po)
                if not detail.get("lines"):
                    return json_response(404, {"error": "Order not found", "purchase_order_id": po})
                return json_response(200, detail)

            if path.endswith("/v1/walmart/sap-sync"):
                po = (q.get("purchase_order_id") or "").strip()
                if not po:
                    return json_response(400, {"error": "purchase_order_id is required"})
                detail = fetch_sap_invoice_sync_detail(conn, po)
                if detail is None:
                    return json_response(
                        404,
                        {"error": "No SAP sync record", "purchase_order_id": po},
                    )
                return json_response(200, detail)

            if path.endswith("/v1/walmart/sap-failures"):
                limit = _clamp_limit(q.get("limit"), default=50)
                return json_response(200, fetch_sap_invoice_failures(conn, limit=limit))

            if path.endswith("/v1/walmart/orders"):
                limit = _clamp_limit(q.get("limit"))
                offset = _clamp_offset(q.get("offset"))
                page = fetch_orders(
                    conn,
                    limit=limit,
                    offset=offset,
                    status=q.get("status") or None,
                    purchase_order_id=(q.get("purchase_order_id") or "").strip() or None,
                    customer_order_id=(q.get("customer_order_id") or "").strip() or None,
                    order_date_from=q.get("order_date_from") or None,
                    order_date_to=q.get("order_date_to") or None,
                    has_shipping=q.get("has_shipping") or None,
                    county_surcharge_tax=q.get("county_surcharge_tax") or None,
                    promotion_discount=q.get("promotion_discount") or None,
                    line_count=q.get("line_count") or None,
                    sap_sync=q.get("sap_sync") or None,
                    has_return=q.get("has_return") or None,
                    sku=(q.get("sku") or "").strip() or None,
                    product_name=(q.get("product_name") or "").strip() or None,
                    quantity=(q.get("quantity") or "").strip() or None,
                    status_date_from=q.get("status_date_from") or None,
                    status_date_to=q.get("status_date_to") or None,
                    updated_from=q.get("updated_from") or None,
                    updated_to=q.get("updated_to") or None,
                    sort_by=q.get("sort_by") or None,
                    sort_dir=q.get("sort_dir") or None,
                )
                return json_response(200, page)

        return json_response(404, {"error": "Not found", "path": path})
    except Exception as exc:
        log_json(logger, logging.ERROR, "walmart_api_error", path=path, error=str(exc))
        return json_response(500, {"error": str(exc)})
