"""Walmart order creation — fetch new orders from Walmart Marketplace."""

from __future__ import annotations

import argparse
import json
import logging
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import psycopg

from walmart.credentials import DATABASE_URL, WM_API_BASE, WM_BASIC_AUTH, WM_SVC_NAME
from walmart.order_updation import repair_invalid_order_dates
from walmart.walmart_store import (
    datetime_to_utc_api_string,
    persist_creation_sync,
    utc_iso_string_to_datetime,
)

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 200
DEFAULT_SHIP_NODE_TYPE = "WFSFulfilled"


def to_utc_date_string(value: str | datetime) -> str:
    """Normalize a value to Walmart UTC format (``YYYY-MM-DDTHH:MM:SS[.mmm]Z``)."""
    if isinstance(value, datetime):
        return datetime_to_utc_api_string(value)

    text = value.strip()
    if not text:
        raise ValueError("Date value cannot be empty")

    if text.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", text):
        return datetime_to_utc_api_string(utc_iso_string_to_datetime(text))

    raise ValueError(
        f"Date must be UTC ISO-8601 with Z suffix, e.g. 2026-04-01T00:00:00Z: {value!r}"
    )


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    data: bytes | None = None,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Walmart API {exc.code} {method} {url}: {body}") from exc


def get_access_token() -> str:
    """Exchange client credentials for a short-lived access token."""
    payload = _http_json(
        "POST",
        f"{WM_API_BASE}/token",
        headers={
            "Authorization": WM_BASIC_AUTH,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "WM_SVC.NAME": WM_SVC_NAME,
        },
        data=b"grant_type=client_credentials",
    )
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Token response missing access_token: {payload}")
    return str(token)


def _order_headers(access_token: str) -> dict[str, str]:
    return {
        "WM_SEC.ACCESS_TOKEN": access_token,
        "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
        "WM_SVC.NAME": WM_SVC_NAME,
        "Accept": "application/json",
    }


def _orders_url(
    *,
    created_start_date: str | datetime,
    created_end_date: str | datetime,
    ship_node_type: str,
    limit: int,
) -> str:
    start_utc = to_utc_date_string(created_start_date)
    end_utc = to_utc_date_string(created_end_date)
    query = urlencode(
        {
            "createdStartDate": start_utc,
            "createdEndDate": end_utc,
            "shipNodeType": ship_node_type,
            "limit": limit,
        }
    )
    return f"{WM_API_BASE}/orders?{query}"


def _extract_orders(page: dict[str, Any]) -> list[dict[str, Any]]:
    order_block = page.get("list", {}).get("elements", {}).get("order", [])
    if not order_block:
        return []
    if isinstance(order_block, list):
        return order_block
    return [order_block]


def _as_list(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


def summarize_orders(orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Build total and per-status counts from fetched Walmart orders."""
    status_counts: dict[str, int] = {}
    total_lines = 0

    for order in orders:
        for line in _as_list(order.get("orderLines", {}).get("orderLine")):
            total_lines += 1
            for line_status in _as_list(
                line.get("orderLineStatuses", {}).get("orderLineStatus")
            ):
                status = str(line_status.get("status") or "Unknown")
                qty_raw = line_status.get("statusQuantity", {}).get("amount", "1")
                try:
                    qty = int(qty_raw)
                except (TypeError, ValueError):
                    qty = 1
                status_counts[status] = status_counts.get(status, 0) + qty

    return {
        "total_orders": len(orders),
        "total_order_lines": total_lines,
        "status_counts": dict(sorted(status_counts.items())),
    }


def _next_orders_url(page: dict[str, Any]) -> str | None:
    next_cursor = page.get("list", {}).get("meta", {}).get("nextCursor")
    if not next_cursor:
        return None
    if next_cursor.startswith("?"):
        return f"{WM_API_BASE}/orders{next_cursor}"
    return f"{WM_API_BASE}/orders?{next_cursor}"


def fetch_orders(
    created_start_date: str | datetime,
    created_end_date: str | datetime,
    *,
    ship_node_type: str = DEFAULT_SHIP_NODE_TYPE,
    limit: int = DEFAULT_LIMIT,
    access_token: str | None = None,
) -> dict[str, Any]:
    """Fetch all Walmart orders in a UTC date range with a status-wise summary."""
    start_utc = to_utc_date_string(created_start_date)
    end_utc = to_utc_date_string(created_end_date)
    logger.info("Fetching orders UTC range %s -> %s", start_utc, end_utc)

    token = access_token or get_access_token()
    headers = _order_headers(token)

    url: str | None = _orders_url(
        created_start_date=start_utc,
        created_end_date=end_utc,
        ship_node_type=ship_node_type,
        limit=limit,
    )

    orders: list[dict[str, Any]] = []
    page_num = 0

    while url:
        page_num += 1
        logger.info("Fetching Walmart orders page=%s url=%s", page_num, url)
        page = _http_json("GET", url, headers=headers)
        batch = _extract_orders(page)
        orders.extend(batch)
        logger.info(
            "Page %s: fetched %s orders (running total=%s)",
            page_num,
            len(batch),
            len(orders),
        )
        url = _next_orders_url(page)

    repair_invalid_order_dates(orders, ship_node_type=ship_node_type, access_token=token)

    summary = summarize_orders(orders)
    logger.info(
        "Fetch complete: total_orders=%s total_order_lines=%s status_counts=%s",
        summary["total_orders"],
        summary["total_order_lines"],
        summary["status_counts"],
    )

    return {"orders": orders, "summary": summary, "pages_fetched": page_num}


def store_creation_sync(
    conn: psycopg.Connection,
    result: dict[str, Any],
    *,
    created_start_date: str | datetime,
    created_end_date: str | datetime,
    ship_node_type: str = DEFAULT_SHIP_NODE_TYPE,
) -> dict[str, Any]:
    """Persist fetch result into walmart_order_headers, walmart_order_lines, and walmart_log."""
    start_utc = to_utc_date_string(created_start_date)
    end_utc = to_utc_date_string(created_end_date)

    return persist_creation_sync(
        conn,
        created_start_date=start_utc,
        created_end_date=end_utc,
        ship_node_type=ship_node_type,
        orders=result["orders"],
        summary=result["summary"],
        pages_fetched=result["pages_fetched"],
        sync_kind="creation",
    )


def get_db_connection() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)


def run_creation_sync(
    *,
    created_start_date: str | datetime,
    created_end_date: str | datetime,
    ship_node_type: str = DEFAULT_SHIP_NODE_TYPE,
    limit: int = DEFAULT_LIMIT,
    store: bool = True,
) -> dict[str, Any]:
    """Fetch orders in a created-date range and optionally persist them."""
    start_utc = to_utc_date_string(created_start_date)
    end_utc = to_utc_date_string(created_end_date)

    result = fetch_orders(
        start_utc,
        end_utc,
        ship_node_type=ship_node_type,
        limit=limit,
    )

    output: dict[str, Any] = {
        "created_start_date": start_utc,
        "created_end_date": end_utc,
        **result["summary"],
        "pages_fetched": result["pages_fetched"],
    }

    if store:
        with get_db_connection() as conn:
            db_result = store_creation_sync(
                conn,
                result,
                created_start_date=start_utc,
                created_end_date=end_utc,
                ship_node_type=ship_node_type,
            )
        output["db"] = db_result

    return output


def _main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Walmart orders for a date range.")
    parser.add_argument(
        "--created-start-date",
        default="2026-04-01T00:00:00Z",
        help="UTC start date (ISO-8601 with Z), e.g. 2026-04-01T00:00:00Z",
    )
    parser.add_argument(
        "--created-end-date",
        default="2026-06-30T23:59:59Z",
        help="UTC end date (ISO-8601 with Z), e.g. 2026-06-30T23:59:59Z",
    )
    parser.add_argument(
        "--ship-node-type",
        default=DEFAULT_SHIP_NODE_TYPE,
        help="shipNodeType filter (default: WFSFulfilled)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Page size per request (default: 200)",
    )
    parser.add_argument(
        "--include-orders",
        action="store_true",
        help="Also print full order payloads (default: summary only)",
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="Fetch only; do not write to walmart_order_headers / walmart_order_lines / walmart_log",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    result = fetch_orders(
        args.created_start_date,
        args.created_end_date,
        ship_node_type=args.ship_node_type,
        limit=args.limit,
    )

    output: dict[str, Any] = dict(result["summary"])
    if not args.no_store:
        with get_db_connection() as conn:
            output["db"] = store_creation_sync(
                conn,
                result,
                created_start_date=args.created_start_date,
                created_end_date=args.created_end_date,
                ship_node_type=args.ship_node_type,
            )

    if args.include_orders:
        print(json.dumps({**result, "summary": output}, indent=2))
    else:
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    _main()
