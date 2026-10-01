"""Walmart order updation — fetch orders modified since the last sync watermark."""

from __future__ import annotations

import argparse
import json
import logging
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import psycopg

from walmart.credentials import DATABASE_URL, WM_API_BASE, WM_BASIC_AUTH, WM_SVC_NAME
from walmart.walmart_store import (
    datetime_to_utc_api_string,
    fetch_last_watermark,
    persist_creation_sync,
    persist_single_order,
    utc_iso_string_to_datetime,
    valid_order_date_ms,
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
    last_modified_start_date: str | datetime,
    last_modified_end_date: str | datetime,
    ship_node_type: str,
    limit: int,
) -> str:
    start_utc = to_utc_date_string(last_modified_start_date)
    end_utc = to_utc_date_string(last_modified_end_date)
    query = urlencode(
        {
            "lastModifiedStartDate": start_utc,
            "lastModifiedEndDate": end_utc,
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


def fetch_single_order(
    purchase_order_id: str,
    *,
    ship_node_type: str = DEFAULT_SHIP_NODE_TYPE,
    access_token: str | None = None,
) -> dict[str, Any]:
    """GET /v3/orders/{purchaseOrderId}; retries without shipNodeType if the filtered call fails."""
    po = str(purchase_order_id).strip()
    if not po:
        raise ValueError("purchase_order_id is required")
    token = access_token or get_access_token()
    base = f"{WM_API_BASE}/orders/{po}"
    urls: list[str] = []
    if ship_node_type:
        urls.append(f"{base}?{urlencode({'productInfo': 'true', 'shipNodeType': ship_node_type})}")
    urls.append(f"{base}?{urlencode({'productInfo': 'true'})}")

    last_exc: Exception | None = None
    for url in urls:
        try:
            # Two attempts must fit inside the 30s API Gateway limit on the dashboard route.
            payload = _http_json("GET", url, headers=_order_headers(token), timeout_sec=12.0)
        except RuntimeError as exc:
            last_exc = exc
            continue
        order = payload.get("order")
        if isinstance(order, dict) and order.get("purchaseOrderId"):
            return order
        last_exc = RuntimeError(f"Walmart response missing order for PO {po}: {payload}")
    raise last_exc or RuntimeError(f"Walmart order {po} not found")


def repair_invalid_order_dates(
    orders: list[dict[str, Any]],
    *,
    ship_node_type: str,
    access_token: str,
) -> list[str]:
    """Replace orders whose list-API ``orderDate`` is missing/zero with the single-order response."""
    repaired: list[str] = []
    for idx, order in enumerate(orders):
        if valid_order_date_ms(order.get("orderDate")) is not None:
            continue
        po = str(order.get("purchaseOrderId") or "").strip()
        if not po:
            continue
        try:
            fresh = fetch_single_order(po, ship_node_type=ship_node_type, access_token=access_token)
        except Exception as exc:
            logger.warning("orderDate repair failed po=%s error=%s", po, exc)
            continue
        if valid_order_date_ms(fresh.get("orderDate")) is None:
            logger.warning("orderDate still invalid after single-order fetch po=%s", po)
            continue
        orders[idx] = fresh
        repaired.append(po)
    if repaired:
        logger.info("Repaired invalid orderDate via single-order fetch: %s", repaired)
    return repaired


def refetch_order(
    purchase_order_id: str,
    *,
    ship_node_type: str = DEFAULT_SHIP_NODE_TYPE,
) -> dict[str, Any]:
    """Re-pull one order from Walmart and overwrite the stored header/lines/raw_payload."""
    order = fetch_single_order(purchase_order_id, ship_node_type=ship_node_type)
    with get_db_connection() as conn:
        stored = persist_single_order(conn, order, ship_node_type=ship_node_type)
    return {
        "ok": True,
        **stored,
        "walmart_order_date_ms": order.get("orderDate"),
        "statuses": sorted(
            {
                str(s.get("status") or "")
                for line in _as_list(order.get("orderLines", {}).get("orderLine"))
                for s in _as_list(line.get("orderLineStatuses", {}).get("orderLineStatus"))
            }
        ),
    }


def fetch_updated_orders(
    last_modified_start_date: str | datetime,
    last_modified_end_date: str | datetime,
    *,
    ship_node_type: str = DEFAULT_SHIP_NODE_TYPE,
    limit: int = DEFAULT_LIMIT,
    access_token: str | None = None,
) -> dict[str, Any]:
    """Fetch Walmart orders modified in a UTC date range, following nextCursor pages."""
    start_utc = to_utc_date_string(last_modified_start_date)
    end_utc = to_utc_date_string(last_modified_end_date)
    logger.info("Fetching updated orders UTC range %s -> %s", start_utc, end_utc)

    token = access_token or get_access_token()
    headers = _order_headers(token)

    url: str | None = _orders_url(
        last_modified_start_date=start_utc,
        last_modified_end_date=end_utc,
        ship_node_type=ship_node_type,
        limit=limit,
    )

    orders: list[dict[str, Any]] = []
    page_num = 0

    while url:
        page_num += 1
        logger.info("Fetching Walmart updated orders page=%s url=%s", page_num, url)
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
        "Updation fetch complete: total_orders=%s total_order_lines=%s status_counts=%s",
        summary["total_orders"],
        summary["total_order_lines"],
        summary["status_counts"],
    )

    return {"orders": orders, "summary": summary, "pages_fetched": page_num}


def get_db_connection() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)


def resolve_sync_window(
    conn: psycopg.Connection,
    *,
    last_modified_start_date: str | datetime | None = None,
    last_modified_end_date: str | datetime | None = None,
) -> tuple[str, str]:
    """
    Resolve ``lastModifiedStartDate`` / ``lastModifiedEndDate`` for the Walmart API.

  - Start: latest successful ``walmart_log`` watermark, or an explicit override.
  - End: explicit override, or current UTC time.
    """
    if last_modified_start_date is not None:
        start_utc = to_utc_date_string(last_modified_start_date)
    else:
        watermark = fetch_last_watermark(conn)
        if watermark is None:
            raise RuntimeError(
                "No walmart_log watermark found. Run order creation sync first, "
                "or pass --last-modified-start-date."
            )
        start_utc = watermark["next_last_modified_start"]
        logger.info(
            "Using watermark from walmart_log: max_status_date_ms=%s start=%s",
            watermark["max_status_date_ms"],
            start_utc,
        )

    if last_modified_end_date is not None:
        end_utc = to_utc_date_string(last_modified_end_date)
    else:
        end_utc = datetime_to_utc_api_string(datetime.now(timezone.utc))
        logger.info("Using current UTC as lastModifiedEndDate: %s", end_utc)

    return start_utc, end_utc


def store_updation_sync(
    conn: psycopg.Connection,
    result: dict[str, Any],
    *,
    last_modified_start_date: str,
    last_modified_end_date: str,
    ship_node_type: str = DEFAULT_SHIP_NODE_TYPE,
) -> dict[str, Any]:
    """Upsert fetched orders and write walmart_log with max statusDate watermark."""
    return persist_creation_sync(
        conn,
        created_start_date=last_modified_start_date,
        created_end_date=last_modified_end_date,
        ship_node_type=ship_node_type,
        orders=result["orders"],
        summary=result["summary"],
        pages_fetched=result["pages_fetched"],
        sync_kind="updation",
    )


def run_updation_sync(
    *,
    last_modified_start_date: str | datetime | None = None,
    last_modified_end_date: str | datetime | None = None,
    ship_node_type: str = DEFAULT_SHIP_NODE_TYPE,
    limit: int = DEFAULT_LIMIT,
    store: bool = True,
) -> dict[str, Any]:
    """Fetch modified orders since the last log watermark and optionally persist them."""
    with get_db_connection() as conn:
        start_utc, end_utc = resolve_sync_window(
            conn,
            last_modified_start_date=last_modified_start_date,
            last_modified_end_date=last_modified_end_date,
        )

        result = fetch_updated_orders(
            start_utc,
            end_utc,
            ship_node_type=ship_node_type,
            limit=limit,
        )

        output: dict[str, Any] = {
            "last_modified_start_date": start_utc,
            "last_modified_end_date": end_utc,
            **result["summary"],
            "pages_fetched": result["pages_fetched"],
        }

        if store:
            db_result = store_updation_sync(
                conn,
                result,
                last_modified_start_date=start_utc,
                last_modified_end_date=end_utc,
                ship_node_type=ship_node_type,
            )
            output["db"] = db_result
            output["lines_created"] = db_result.get("created", 0)
            output["lines_updated"] = db_result.get("updated", 0)
            output["lines_changed"] = db_result.get("changed", 0)
            output["lines_refreshed"] = db_result.get("refreshed", 0)
            output["lines_stored"] = db_result.get("lines_stored", 0)
            output["created_lines"] = db_result.get("created_lines", [])
            output["changed_lines"] = db_result.get("changed_lines", [])
            output["refreshed_lines"] = db_result.get("refreshed_lines", [])
            output["updated_lines"] = db_result.get("updated_lines", [])

        return output


def _truncate(text: str | None, max_len: int = 72) -> str:
    if not text:
        return "-"
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _format_line_changes(line: dict[str, Any]) -> list[str]:
    changes: list[str] = []
    prev_status = line.get("previous_status")
    if prev_status is not None and prev_status != line.get("status"):
        changes.append(f"Status : {prev_status} -> {line.get('status')}")
    prev_date = line.get("previous_status_date")
    if prev_date is not None and prev_date != line.get("status_date"):
        changes.append(f"Date   : {prev_date} -> {line.get('status_date')}")
    prev_tracking = line.get("previous_tracking_number")
    tracking = line.get("tracking_number")
    if prev_tracking != tracking and (prev_tracking or tracking):
        changes.append(f"Tracking : {prev_tracking or '-'} -> {tracking or '-'}")
    prev_carrier = line.get("previous_carrier_name")
    carrier = line.get("carrier_name")
    if prev_carrier != carrier and (prev_carrier or carrier):
        changes.append(f"Carrier  : {prev_carrier or '-'} -> {carrier or '-'}")
    return changes


def _print_line_block(line: dict[str, Any], *, refreshed: bool = False) -> None:
    print(f"  PO          : {line['purchase_order_id']}")
    print(f"  Line        : {line['line_number']}")
    print(f"  Status      : {line.get('status') or '-'}")
    print(f"  Status date : {line.get('status_date') or '-'}")
    print(f"  SKU         : {line.get('sku') or '-'}")
    print(f"  Product     : {_truncate(line.get('product_name'))}")
    if line.get("tracking_number"):
        print(f"  Tracking    : {line['tracking_number']}")
    if refreshed:
        print("  Note        : re-synced from Walmart (no field changes)")
    else:
        for change in _format_line_changes(line):
            print(f"  {change}")
    print()


def print_updation_summary(output: dict[str, Any]) -> None:
    """Print a human-readable summary of an updation sync run."""
    db = output.get("db") or {}
    log_id = db.get("log_id", "-")
    start = output.get("last_modified_start_date", "-")
    end = output.get("last_modified_end_date", "-")
    total_orders = output.get("total_orders", 0)
    total_lines = output.get("total_order_lines", 0)
    created = output.get("lines_created", 0)
    changed = output.get("lines_changed", 0)
    refreshed = output.get("lines_refreshed", 0)
    stored = output.get("lines_stored", created + changed + refreshed)

    print()
    print("--- Walmart updation sync ---")
    print(f"Log id  : {log_id}")
    print(f"Window  : {start} -> {end}")
    print(f"Fetched : {total_orders} order(s), {total_lines} line(s)")
    print(f"Stored  : {stored} line(s)")
    print(f"  Created   : {created}")
    print(f"  Changed   : {changed}")
    print(f"  Refreshed : {refreshed}")

    created_lines = output.get("created_lines") or []
    changed_lines = output.get("changed_lines") or []
    refreshed_lines = output.get("refreshed_lines") or []

    if created_lines:
        print()
        print("--- New lines ---")
        for line in created_lines:
            _print_line_block(line)

    if changed_lines:
        print("--- Status / field changes ---")
        for line in changed_lines:
            _print_line_block(line)

    if refreshed_lines:
        print("--- Refreshed lines (no changes) ---")
        for line in refreshed_lines:
            _print_line_block(line, refreshed=True)


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Walmart orders modified since the last walmart_log watermark."
    )
    parser.add_argument(
        "--last-modified-start-date",
        default=None,
        help="UTC start override (ISO-8601 with Z). Default: latest walmart_log watermark.",
    )
    parser.add_argument(
        "--last-modified-end-date",
        default=None,
        help="UTC end override (ISO-8601 with Z). Default: current UTC time.",
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
        "--no-store",
        action="store_true",
        help="Fetch only; do not write to walmart_order_headers / walmart_order_lines / walmart_log",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full JSON output (default: human-readable summary only)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    output = run_updation_sync(
        last_modified_start_date=args.last_modified_start_date,
        last_modified_end_date=args.last_modified_end_date,
        ship_node_type=args.ship_node_type,
        limit=args.limit,
        store=not args.no_store,
    )

    if args.json:
        print(json.dumps(output, indent=2))

    if not args.no_store:
        print_updation_summary(output)
    elif not args.json:
        print()
        print("--- Fetch summary (no store) ---")
        print(f"Window  : {output.get('last_modified_start_date')} -> {output.get('last_modified_end_date')}")
        print(f"Fetched : {output.get('total_orders', 0)} order(s), {output.get('total_order_lines', 0)} line(s)")
        print(f"Status  : {output.get('status_counts', {})}")


if __name__ == "__main__":
    _main()
