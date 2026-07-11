"""HTTP API for Walmart orders dashboard (read-only RDS queries)."""

from __future__ import annotations

import logging
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
    fetch_sap_invoice_sync_detail,
    fetch_sap_invoice_failures,
    fetch_sync_log,
    get_db_connection,
    json_response,
    _clamp_limit,
    _clamp_offset,
)

logger = logging.getLogger(__name__)


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

        with get_db_connection() as conn:
            if path.endswith("/v1/walmart/stats"):
                return json_response(200, fetch_order_stats(conn))

            if path.endswith("/v1/walmart/sync-log"):
                limit = _clamp_limit(q.get("limit"), default=20)
                return json_response(200, {"items": fetch_sync_log(conn, limit=limit)})

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
                    order_date_from=q.get("order_date_from") or None,
                    order_date_to=q.get("order_date_to") or None,
                    has_shipping=q.get("has_shipping") or None,
                    sap_sync=q.get("sap_sync") or None,
                )
                return json_response(200, page)

        return json_response(404, {"error": "Not found", "path": path})
    except Exception as exc:
        log_json(logger, logging.ERROR, "walmart_api_error", path=path, error=str(exc))
        return json_response(500, {"error": str(exc)})
