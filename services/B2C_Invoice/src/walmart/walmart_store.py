"""PostgreSQL persistence for Walmart order creation sync."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

UTC_API_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"


def ms_to_utc_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def ms_to_utc_api_string(ms: int) -> str:
    """Walmart API UTC param, e.g. ``2026-06-30T11:32:15.925Z`` or ``...T11:32:15Z``."""
    dt = ms_to_utc_datetime(ms)
    millis = ms % 1000
    if millis:
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"
    return dt.strftime(UTC_API_DATE_FMT)


def datetime_to_utc_api_string(dt: datetime) -> str:
    """Convert UTC datetime to Walmart ``lastModified*Date`` string (``Z`` suffix)."""
    utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    millis = utc.microsecond // 1000
    if millis:
        return utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"
    return utc.strftime(UTC_API_DATE_FMT)


def utc_iso_string_to_datetime(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_list(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


FETCH_LAST_WATERMARK = """
SELECT max_status_date_ms, next_last_modified_start
FROM walmart_log
WHERE status = 'success' AND max_status_date_ms IS NOT NULL
ORDER BY id DESC
LIMIT 1
"""


def fetch_last_watermark(conn: Any) -> dict[str, Any] | None:
    """Return the latest sync watermark for order updation (``lastModifiedStartDate``)."""
    with conn.cursor() as cur:
        cur.execute(FETCH_LAST_WATERMARK)
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    max_ms = int(row[0])
    return {
        "max_status_date_ms": max_ms,
        "next_last_modified_start": ms_to_utc_api_string(max_ms),
    }


def max_status_date_ms(orders: list[dict[str, Any]]) -> int | None:
    max_ms: int | None = None
    for order in orders:
        for line in _as_list(order.get("orderLines", {}).get("orderLine")):
            raw = line.get("statusDate")
            if raw is None:
                continue
            try:
                ms = int(raw)
            except (TypeError, ValueError):
                continue
            if max_ms is None or ms > max_ms:
                max_ms = ms
    return max_ms


def _line_status(line: dict[str, Any]) -> str:
    statuses = _as_list(line.get("orderLineStatuses", {}).get("orderLineStatus"))
    if not statuses:
        return "Unknown"
    return str(statuses[-1].get("status") or "Unknown")


def order_has_shipping_charges(order: dict[str, Any]) -> bool:
    for line in _as_list(order.get("orderLines", {}).get("orderLine")):
        for charge in _as_list(line.get("charges", {}).get("charge")):
            charge_type = str(charge.get("chargeType") or "").upper()
            charge_name = str(charge.get("chargeName") or "")
            if charge_type != "SHIPPING" and charge_name != "Shipping":
                continue
            amount = (charge.get("chargeAmount") or {}).get("amount")
            try:
                if amount is not None and float(amount) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _line_tracking(line: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    statuses = _as_list(line.get("orderLineStatuses", {}).get("orderLineStatus"))
    if not statuses:
        return None, None, None
    tracking = statuses[-1].get("trackingInfo") or {}
    carrier = tracking.get("carrierName") or {}
    carrier_name = carrier.get("carrier") or carrier.get("otherCarrier")
    tracking_number = tracking.get("trackingNumber") or tracking.get("trackingId")
    tracking_url = tracking.get("trackingURL")
    return (
        str(tracking_number) if tracking_number else None,
        str(tracking_url) if tracking_url else None,
        str(carrier_name) if carrier_name else None,
    )


def extract_orders_for_storage(
    orders: list[dict[str, Any]],
    *,
    ship_node_type: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    headers: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []

    for order in orders:
        purchase_order_id = str(order.get("purchaseOrderId") or "").strip()
        if not purchase_order_id:
            continue

        customer_order_id = order.get("customerOrderId")
        order_date_ms = order.get("orderDate")
        node_type = (order.get("shipNode") or {}).get("type") or ship_node_type
        order_lines = _as_list(order.get("orderLines", {}).get("orderLine"))
        full_raw_payload = json.dumps({"order": order})

        headers.append(
            {
                "purchase_order_id": purchase_order_id,
                "customer_order_id": customer_order_id,
                "ship_node_type": node_type,
                "order_date_ms": int(order_date_ms) if order_date_ms is not None else None,
                "has_shipping_charges": order_has_shipping_charges(order),
                "raw_payload": full_raw_payload,
            }
        )

        for line in order_lines:
            line_number = str(line.get("lineNumber") or "").strip()
            if not line_number:
                continue

            status_date_ms = int(line["statusDate"])
            item = line.get("item") or {}
            tracking_number, tracking_url, carrier_name = _line_tracking(line)
            qty = (line.get("orderLineQuantity") or {}).get("amount")

            lines.append(
                {
                    "purchase_order_id": purchase_order_id,
                    "line_number": line_number,
                    "status": _line_status(line),
                    "status_date_ms": status_date_ms,
                    "status_date": ms_to_utc_datetime(status_date_ms),
                    "sku": item.get("sku"),
                    "product_name": item.get("productName"),
                    "quantity_amount": str(qty) if qty is not None else None,
                    "tracking_number": tracking_number,
                    "tracking_url": tracking_url,
                    "carrier_name": carrier_name,
                }
            )

    return headers, lines


def extract_order_line_rows(
    orders: list[dict[str, Any]],
    *,
    ship_node_type: str,
) -> list[dict[str, Any]]:
    """Backward-compatible helper returning flat line rows with header fields."""
    headers, lines = extract_orders_for_storage(orders, ship_node_type=ship_node_type)
    header_by_po = {row["purchase_order_id"]: row for row in headers}
    flat_rows: list[dict[str, Any]] = []

    for line in lines:
        header = header_by_po[line["purchase_order_id"]]
        flat_rows.append(
            {
                **line,
                "customer_order_id": header.get("customer_order_id"),
                "ship_node_type": header.get("ship_node_type"),
                "order_date_ms": header.get("order_date_ms"),
                "raw_payload": header.get("raw_payload"),
            }
        )

    return flat_rows


INSERT_SYNC_LOG = """
INSERT INTO walmart_log (
  status, last_modified_start_date, last_modified_end_date, ship_node_type
) VALUES ('running', %(start_date)s, %(end_date)s, %(ship_node_type)s)
RETURNING id
"""

FINISH_SYNC_LOG_SUCCESS = """
UPDATE walmart_log SET
  sync_finished_at = now(),
  status = 'success',
  max_status_date_ms = %(max_status_date_ms)s,
  next_last_modified_start = %(next_last_modified_start)s,
  pages_fetched = %(pages_fetched)s,
  orders_seen = %(orders_seen)s,
  lines_stored = %(lines_stored)s,
  meta = %(meta)s::jsonb
WHERE id = %(log_id)s
"""

FINISH_SYNC_LOG_FAILURE = """
UPDATE walmart_log SET
  sync_finished_at = now(),
  status = 'failed',
  pages_fetched = %(pages_fetched)s,
  orders_seen = %(orders_seen)s,
  lines_stored = %(lines_stored)s,
  error_message = %(error_message)s,
  meta = %(meta)s::jsonb
WHERE id = %(log_id)s
"""

UPSERT_ORDER_HEADER = """
INSERT INTO walmart_order_headers (
  purchase_order_id, customer_order_id, ship_node_type,
  order_date_ms, has_shipping_charges, raw_payload, updated_at
) VALUES (
  %(purchase_order_id)s, %(customer_order_id)s, %(ship_node_type)s,
  %(order_date_ms)s, %(has_shipping_charges)s, %(raw_payload)s::jsonb, now()
)
ON CONFLICT (purchase_order_id) DO UPDATE SET
  customer_order_id = EXCLUDED.customer_order_id,
  ship_node_type = EXCLUDED.ship_node_type,
  order_date_ms = EXCLUDED.order_date_ms,
  has_shipping_charges = EXCLUDED.has_shipping_charges,
  raw_payload = EXCLUDED.raw_payload,
  updated_at = now()
"""

UPSERT_ORDER_LINE = """
INSERT INTO walmart_order_lines (
  purchase_order_id, line_number, status,
  status_date_ms, status_date, sku, product_name, quantity_amount,
  tracking_number, tracking_url, carrier_name, updated_at
) VALUES (
  %(purchase_order_id)s, %(line_number)s, %(status)s,
  %(status_date_ms)s, %(status_date)s, %(sku)s, %(product_name)s, %(quantity_amount)s,
  %(tracking_number)s, %(tracking_url)s, %(carrier_name)s, now()
)
ON CONFLICT (purchase_order_id, line_number) DO UPDATE SET
  status = EXCLUDED.status,
  status_date_ms = EXCLUDED.status_date_ms,
  status_date = EXCLUDED.status_date,
  sku = EXCLUDED.sku,
  product_name = EXCLUDED.product_name,
  quantity_amount = EXCLUDED.quantity_amount,
  tracking_number = EXCLUDED.tracking_number,
  tracking_url = EXCLUDED.tracking_url,
  carrier_name = EXCLUDED.carrier_name,
  updated_at = now()
RETURNING
  (xmax = 0) AS inserted,
  purchase_order_id,
  line_number,
  status,
  sku,
  product_name,
  status_date_ms
"""


def _fetch_existing_lines(
    conn: Any, rows: list[dict[str, Any]]
) -> dict[tuple[str, str], dict[str, Any]]:
    if not rows:
        return {}

    pairs = list({(r["purchase_order_id"], r["line_number"]) for r in rows})
    po_ids = [po for po, _ in pairs]
    line_nos = [ln for _, ln in pairs]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.purchase_order_id, o.line_number, o.status, o.status_date_ms,
                   o.sku, o.product_name, o.tracking_number, o.carrier_name
            FROM walmart_order_lines o
            INNER JOIN unnest(%s::text[], %s::text[]) AS k(po, ln)
              ON o.purchase_order_id = k.po AND o.line_number = k.ln
            """,
            (po_ids, line_nos),
        )
        return {
            (str(row[0]), str(row[1])): {
                "status": row[2],
                "status_date_ms": row[3],
                "sku": row[4],
                "product_name": row[5],
                "tracking_number": row[6],
                "carrier_name": row[7],
            }
            for row in cur.fetchall()
        }


def _line_meaningfully_changed(
    previous: dict[str, Any] | None, current: dict[str, Any]
) -> bool:
    if previous is None:
        return True
    for field in (
        "status",
        "status_date_ms",
        "tracking_number",
        "carrier_name",
        "sku",
    ):
        if previous.get(field) != current.get(field):
            return True
    return False


def _line_detail_from_row(
    row: dict[str, Any], *, previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    status_date_ms = int(row["status_date_ms"])
    detail: dict[str, Any] = {
        "purchase_order_id": row["purchase_order_id"],
        "line_number": row["line_number"],
        "status": row["status"],
        "sku": row["sku"],
        "product_name": row["product_name"],
        "status_date": ms_to_utc_api_string(status_date_ms),
        "tracking_number": row.get("tracking_number"),
        "carrier_name": row.get("carrier_name"),
    }
    if previous is not None:
        prev_ms = previous.get("status_date_ms")
        detail["previous_status"] = previous.get("status")
        detail["previous_status_date"] = (
            ms_to_utc_api_string(int(prev_ms)) if prev_ms is not None else None
        )
        detail["previous_tracking_number"] = previous.get("tracking_number")
        detail["previous_carrier_name"] = previous.get("carrier_name")
    return detail


def upsert_order_headers(
    conn: Any, headers: list[dict[str, Any]], *, commit: bool = True
) -> int:
    if not headers:
        return 0

    with conn.cursor() as cur:
        for header in headers:
            cur.execute(UPSERT_ORDER_HEADER, header)

    if commit:
        conn.commit()

    return len(headers)


def upsert_order_lines(
    conn: Any, rows: list[dict[str, Any]], *, commit: bool = True
) -> dict[str, Any]:
    empty = {
        "lines_stored": 0,
        "created": 0,
        "updated": 0,
        "changed": 0,
        "refreshed": 0,
        "created_lines": [],
        "changed_lines": [],
        "refreshed_lines": [],
        "updated_lines": [],
    }
    if not rows:
        return empty

    existing = _fetch_existing_lines(conn, rows)
    created_lines: list[dict[str, Any]] = []
    changed_lines: list[dict[str, Any]] = []
    refreshed_lines: list[dict[str, Any]] = []

    with conn.cursor() as cur:
        for row in rows:
            cur.execute(UPSERT_ORDER_LINE, row)
            result = cur.fetchone()
            if result is None:
                continue

            inserted, po_id, line_no, status, sku, product_name, status_date_ms = result
            line_detail = {
                "purchase_order_id": po_id,
                "line_number": line_no,
                "status": status,
                "sku": sku,
                "product_name": product_name,
                "status_date_ms": status_date_ms,
                "tracking_number": row.get("tracking_number"),
                "carrier_name": row.get("carrier_name"),
            }
            key = (str(po_id), str(line_no))
            if inserted:
                created_lines.append(_line_detail_from_row(line_detail))
                continue

            previous = existing.get(key)
            detail = _line_detail_from_row(line_detail, previous=previous)
            if _line_meaningfully_changed(previous, line_detail):
                changed_lines.append(detail)
            else:
                refreshed_lines.append(detail)

    if commit:
        conn.commit()

    updated_lines = changed_lines + refreshed_lines
    return {
        "lines_stored": len(rows),
        "created": len(created_lines),
        "updated": len(updated_lines),
        "changed": len(changed_lines),
        "refreshed": len(refreshed_lines),
        "created_lines": created_lines,
        "changed_lines": changed_lines,
        "refreshed_lines": refreshed_lines,
        "updated_lines": updated_lines,
    }


def persist_creation_sync(
    conn: Any,
    *,
    created_start_date: str,
    created_end_date: str,
    ship_node_type: str,
    orders: list[dict[str, Any]],
    summary: dict[str, Any],
    pages_fetched: int,
    sync_kind: str = "creation",
) -> dict[str, Any]:
    """Store fetched orders and write walmart_log with max statusDate watermark."""
    start_dt = utc_iso_string_to_datetime(created_start_date)
    end_dt = utc_iso_string_to_datetime(created_end_date)
    line_rows = extract_orders_for_storage(orders, ship_node_type=ship_node_type)
    header_rows, line_rows_only = line_rows
    max_ms = max_status_date_ms(orders)
    next_start = ms_to_utc_datetime(max_ms) if max_ms is not None else None

    log_id: int | None = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                INSERT_SYNC_LOG,
                {
                    "start_date": start_dt,
                    "end_date": end_dt,
                    "ship_node_type": ship_node_type,
                },
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("walmart_log insert did not return id")
            log_id = int(row[0])

        upsert_order_headers(conn, header_rows, commit=False)
        upsert_counts = upsert_order_lines(conn, line_rows_only, commit=False)

        sap_invoice_results: list[dict[str, Any]] = []
        if sync_kind == "updation":
            try:
                from walmart.sap_invoice_sync import process_shipped_orders_for_sap_invoice

                sap_invoice_results = process_shipped_orders_for_sap_invoice(
                    conn, orders, upsert_counts, post_to_sap=True
                )
            except Exception as exc:
                logger.exception("SAP invoice batch failed")
                sap_invoice_results = [{"status": "batch_error", "error": str(exc)}]

        with conn.cursor() as cur:
            cur.execute(
                FINISH_SYNC_LOG_SUCCESS,
                {
                    "log_id": log_id,
                    "max_status_date_ms": max_ms,
                    "next_last_modified_start": next_start,
                    "pages_fetched": pages_fetched,
                    "orders_seen": summary.get("total_orders", len(orders)),
                    "lines_stored": upsert_counts["lines_stored"],
                    "meta": json.dumps(
                        {
                            "status_counts": summary.get("status_counts", {}),
                            "lines_created": upsert_counts["created"],
                            "lines_updated": upsert_counts["updated"],
                            "lines_changed": upsert_counts["changed"],
                            "lines_refreshed": upsert_counts["refreshed"],
                            "created_lines": upsert_counts["created_lines"],
                            "changed_lines": upsert_counts["changed_lines"],
                            "refreshed_lines": upsert_counts["refreshed_lines"],
                            "updated_lines": upsert_counts["updated_lines"],
                            "sap_invoice_results": sap_invoice_results,
                            "next_last_modified_start": (
                                ms_to_utc_api_string(max_ms) if max_ms is not None else None
                            ),
                        }
                    ),
                },
            )
        conn.commit()

        result = {
            "log_id": log_id,
            **upsert_counts,
            "sap_invoice_results": sap_invoice_results,
            "max_status_date_ms": max_ms,
            # UTC millis from Walmart statusDate; use as lastModifiedStartDate on next sync.
            "next_last_modified_start": ms_to_utc_api_string(max_ms) if max_ms is not None else None,
        }
        logger.info(
            "Walmart %s sync stored: log_id=%s lines_stored=%s created=%s "
            "changed=%s refreshed=%s max_status_date_ms=%s",
            sync_kind,
            log_id,
            upsert_counts["lines_stored"],
            upsert_counts["created"],
            upsert_counts["changed"],
            upsert_counts["refreshed"],
            max_ms,
        )
        return result
    except Exception as exc:
        conn.rollback()
        if log_id is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        FINISH_SYNC_LOG_FAILURE,
                        {
                            "log_id": log_id,
                            "pages_fetched": pages_fetched,
                            "orders_seen": len(orders),
                            "lines_stored": 0,
                            "error_message": str(exc),
                            "meta": json.dumps({"status_counts": summary.get("status_counts", {})}),
                        },
                    )
                conn.commit()
            except Exception:
                conn.rollback()
        raise
