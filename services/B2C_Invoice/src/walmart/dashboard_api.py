"""Read-only queries for the Walmart orders dashboard API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import psycopg

from walmart.credentials import DATABASE_URL

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def get_db_connection() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)


def _clamp_limit(raw: str | None, default: int = DEFAULT_LIMIT) -> int:
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_LIMIT))


def _rows_to_dicts(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    columns = [desc[0] for desc in cur.description] if cur.description else []
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def fetch_order_stats(conn: psycopg.Connection) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM walmart_order_lines")
        total_lines = int(cur.fetchone()[0])

        cur.execute("SELECT COUNT(*) FROM walmart_order_headers")
        total_orders = int(cur.fetchone()[0])

        cur.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM walmart_order_lines
            GROUP BY status
            ORDER BY count DESC, status ASC
            """
        )
        by_status = _rows_to_dicts(cur)

        cur.execute(
            """
            SELECT id, status, sync_finished_at, orders_seen, lines_stored,
                   max_status_date_ms, last_modified_start_date, last_modified_end_date
            FROM walmart_log
            ORDER BY id DESC
            LIMIT 1
            """
        )
        last_sync_rows = _rows_to_dicts(cur)
        last_sync = last_sync_rows[0] if last_sync_rows else None

        cur.execute(
            """
            SELECT status, COUNT(*)::int AS count
            FROM walmart_sap_invoice_log
            GROUP BY status
            """
        )
        sap_rows = _rows_to_dicts(cur)
        sap_by_status = {str(r["status"]): int(r["count"]) for r in sap_rows}

    return {
        "total_lines": total_lines,
        "total_orders": total_orders,
        "by_status": by_status,
        "last_sync": last_sync,
        "sap_sync": {
            "failed": sap_by_status.get("failed", 0),
            "posted": sap_by_status.get("posted", 0),
            "prepared": sap_by_status.get("prepared", 0),
        },
    }


def _clamp_offset(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _utc_day_start_ms(date_str: str) -> int:
    day = datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(day.timestamp() * 1000)


def _utc_day_end_exclusive_ms(date_str: str) -> int:
    return _utc_day_start_ms(date_str) + 24 * 60 * 60 * 1000


def _header_filter_clauses(
    *,
    status: str | None = None,
    purchase_order_id: str | None = None,
    order_date_from: str | None = None,
    order_date_to: str | None = None,
    has_shipping: str | None = None,
    sap_sync: str | None = None,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if purchase_order_id and purchase_order_id.strip():
        clauses.append("h.purchase_order_id ILIKE %s")
        params.append(f"%{purchase_order_id.strip()}%")

    if status:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM walmart_order_lines lf
                WHERE lf.purchase_order_id = h.purchase_order_id
                  AND lf.status = %s
            )
            """
        )
        params.append(status)

    if order_date_from and order_date_from.strip():
        clauses.append("h.order_date_ms >= %s")
        params.append(_utc_day_start_ms(order_date_from.strip()))

    if order_date_to and order_date_to.strip():
        clauses.append("h.order_date_ms < %s")
        params.append(_utc_day_end_exclusive_ms(order_date_to.strip()))

    shipping = (has_shipping or "").strip().lower()
    if shipping in {"yes", "true", "1", "applicable"}:
        clauses.append("h.has_shipping_charges = true")
    elif shipping in {"no", "false", "0", "not_applicable", "na"}:
        clauses.append("h.has_shipping_charges = false")

    sap = (sap_sync or "").strip().lower()
    if sap == "failed":
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM walmart_sap_invoice_log sf
                WHERE sf.purchase_order_id = h.purchase_order_id
                  AND sf.status = 'failed'
            )
            """
        )
    elif sap == "posted":
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM walmart_sap_invoice_log sp
                WHERE sp.purchase_order_id = h.purchase_order_id
                  AND sp.status = 'posted'
            )
            """
        )
    elif sap in {"pending", "none", "not_synced"}:
        clauses.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM walmart_sap_invoice_log sn
                WHERE sn.purchase_order_id = h.purchase_order_id
            )
            """
        )

    return clauses, params


def _sap_sync_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    status = row.get("sap_sync_status")
    if not status:
        return None
    return {
        "status": status,
        "sap_doc_entry": row.get("sap_doc_entry"),
        "sap_doc_num": row.get("sap_doc_num"),
        "error_message": row.get("sap_error_message"),
        "error_phase": row.get("sap_error_phase"),
        "attempt_count": row.get("sap_attempt_count"),
        "updated_at": row.get("sap_sync_updated_at"),
    }


def _group_rows_by_po(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    current_po: str | None = None
    current: dict[str, Any] | None = None

    for row in rows:
        po = row["purchase_order_id"]
        line = {
            "line_number": row.get("line_number"),
            "status": row.get("status"),
            "status_date": row.get("status_date"),
            "sku": row.get("sku"),
            "product_name": row.get("product_name"),
            "quantity_amount": row.get("quantity_amount"),
            "tracking_number": row.get("tracking_number"),
            "carrier_name": row.get("carrier_name"),
            "updated_at": row.get("line_updated_at") or row.get("updated_at"),
        }

        if po != current_po:
            if current is not None:
                orders.append(current)
            current_po = po
            current = {
                "purchase_order_id": po,
                "customer_order_id": row.get("customer_order_id"),
                "ship_node_type": row.get("ship_node_type"),
                "order_date_ms": row.get("order_date_ms"),
                "has_shipping_charges": row.get("has_shipping_charges"),
                "status_date": row.get("status_date"),
                "updated_at": row.get("header_updated_at") or row.get("updated_at"),
                "line_count": 1,
                "lines": [line],
                "sap_sync": _sap_sync_from_row(row),
            }
            continue

        assert current is not None
        current["lines"].append(line)
        current["line_count"] += 1
        if row.get("status_date") and (
            not current.get("status_date")
            or str(row["status_date"]) > str(current["status_date"])
        ):
            current["status_date"] = row["status_date"]
        header_updated = row.get("header_updated_at") or row.get("updated_at")
        if header_updated and (
            not current.get("updated_at")
            or str(header_updated) > str(current["updated_at"])
        ):
            current["updated_at"] = header_updated

    if current is not None:
        orders.append(current)

    return orders


def fetch_orders(
    conn: psycopg.Connection,
    *,
    limit: int,
    offset: int = 0,
    status: str | None = None,
    purchase_order_id: str | None = None,
    order_date_from: str | None = None,
    order_date_to: str | None = None,
    has_shipping: str | None = None,
    sap_sync: str | None = None,
) -> dict[str, Any]:
    clauses, params = _header_filter_clauses(
        status=status,
        purchase_order_id=purchase_order_id,
        order_date_from=order_date_from,
        order_date_to=order_date_to,
        has_shipping=has_shipping,
        sap_sync=sap_sync,
    )
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM walmart_order_headers h {where_sql}",
            params,
        )
        total = int(cur.fetchone()[0])

        data_params = [*params, limit, offset]
        sql = f"""
            WITH po_page AS (
                SELECT
                    h.purchase_order_id,
                    COALESCE(h.order_date_ms, MAX(l.status_date_ms)) AS sort_ms
                FROM walmart_order_headers h
                INNER JOIN walmart_order_lines l
                  ON l.purchase_order_id = h.purchase_order_id
                {where_sql}
                GROUP BY h.purchase_order_id, h.order_date_ms
                ORDER BY sort_ms DESC
                LIMIT %s OFFSET %s
            )
            SELECT
                h.purchase_order_id,
                h.customer_order_id,
                h.ship_node_type,
                h.order_date_ms,
                h.has_shipping_charges,
                h.updated_at AS header_updated_at,
                sap.status AS sap_sync_status,
                sap.sap_doc_entry,
                sap.sap_doc_num,
                sap.error_message AS sap_error_message,
                sap.error_phase AS sap_error_phase,
                sap.attempt_count AS sap_attempt_count,
                sap.updated_at AS sap_sync_updated_at,
                l.line_number,
                l.status,
                l.status_date,
                l.sku,
                l.product_name,
                l.quantity_amount,
                l.tracking_number,
                l.carrier_name,
                l.updated_at AS line_updated_at
            FROM po_page p
            INNER JOIN walmart_order_headers h
              ON h.purchase_order_id = p.purchase_order_id
            LEFT JOIN walmart_sap_invoice_log sap
              ON sap.purchase_order_id = h.purchase_order_id
            INNER JOIN walmart_order_lines l
              ON l.purchase_order_id = h.purchase_order_id
            ORDER BY p.sort_ms DESC, l.line_number
        """
        cur.execute(sql, data_params)
        items = _group_rows_by_po(_rows_to_dicts(cur))

    return {
        "items": items,
        "count": len(items),
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def _parse_raw_payload(raw_payload: Any) -> dict[str, Any] | None:
    if not raw_payload:
        return None
    try:
        parsed = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _order_json_from_payload(parsed: dict[str, Any] | None) -> dict[str, Any] | None:
    if not parsed:
        return None
    if parsed.get("order"):
        return parsed
    if parsed.get("purchaseOrderId"):
        # Legacy per-line snapshot migrated into the header table.
        return {"order": parsed}
    return None


def _order_json_from_rows(header: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
    order_lines = []
    for line in lines:
        order_lines.append(
            {
                "lineNumber": line.get("line_number"),
                "item": {
                    "sku": line.get("sku"),
                    "productName": line.get("product_name"),
                },
                "orderLineQuantity": {"amount": line.get("quantity_amount")},
                "statusDate": line.get("status_date"),
                "orderLineStatuses": {
                    "orderLineStatus": [
                        {
                            "status": line.get("status"),
                            "trackingInfo": (
                                {
                                    "trackingNumber": line.get("tracking_number"),
                                    "trackingURL": line.get("tracking_url"),
                                    "carrierName": {"otherCarrier": line.get("carrier_name")},
                                }
                                if line.get("tracking_number") or line.get("carrier_name")
                                else None
                            ),
                        }
                    ]
                },
            }
        )

    return {
        "order": {
            "purchaseOrderId": header.get("purchase_order_id"),
            "customerOrderId": header.get("customer_order_id"),
            "shipNode": {"type": header.get("ship_node_type")},
            "orderLines": {"orderLine": order_lines},
        }
    }


def fetch_order_detail(conn: psycopg.Connection, purchase_order_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                h.purchase_order_id,
                h.customer_order_id,
                h.ship_node_type,
                h.order_date_ms,
                h.has_shipping_charges,
                h.raw_payload,
                h.updated_at AS header_updated_at,
                l.line_number,
                l.status,
                l.status_date,
                l.sku,
                l.product_name,
                l.quantity_amount,
                l.tracking_number,
                l.tracking_url,
                l.carrier_name,
                l.updated_at AS line_updated_at
            FROM walmart_order_headers h
            LEFT JOIN walmart_order_lines l
              ON l.purchase_order_id = h.purchase_order_id
            WHERE h.purchase_order_id = %s
            ORDER BY l.line_number
            """,
            (purchase_order_id,),
        )
        rows = _rows_to_dicts(cur)

    if not rows:
        return {"purchase_order_id": purchase_order_id, "lines": [], "line_count": 0}

    header = rows[0]
    lines = [
        {
            "line_number": row.get("line_number"),
            "status": row.get("status"),
            "status_date": row.get("status_date"),
            "sku": row.get("sku"),
            "product_name": row.get("product_name"),
            "quantity_amount": row.get("quantity_amount"),
            "tracking_number": row.get("tracking_number"),
            "tracking_url": row.get("tracking_url"),
            "carrier_name": row.get("carrier_name"),
            "updated_at": row.get("line_updated_at"),
        }
        for row in rows
        if row.get("line_number") is not None
    ]

    order_json = _order_json_from_payload(_parse_raw_payload(header.get("raw_payload")))
    if order_json is None and lines:
        order_json = _order_json_from_rows(header, lines)

    sap_sync = fetch_sap_invoice_sync_detail(conn, purchase_order_id)

    result = {
        "purchase_order_id": purchase_order_id,
        "customer_order_id": header.get("customer_order_id"),
        "ship_node_type": header.get("ship_node_type"),
        "order_date_ms": header.get("order_date_ms"),
        "has_shipping_charges": header.get("has_shipping_charges"),
        "line_count": len(lines),
        "lines": lines,
        "raw_payload": header.get("raw_payload"),
        "order_json": order_json,
        "sap_sync": sap_sync,
    }
    return result


def fetch_sap_invoice_sync_detail(
    conn: psycopg.Connection, purchase_order_id: str
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              purchase_order_id,
              customer_order_id,
              status,
              sap_doc_entry,
              sap_doc_num,
              error_message,
              error_phase,
              sap_payload,
              sap_response,
              attempt_count,
              created_at,
              updated_at
            FROM walmart_sap_invoice_log
            WHERE purchase_order_id = %s
            """,
            (purchase_order_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None

    columns = [
        "purchase_order_id",
        "customer_order_id",
        "status",
        "sap_doc_entry",
        "sap_doc_num",
        "error_message",
        "error_phase",
        "sap_payload",
        "sap_response",
        "attempt_count",
        "created_at",
        "updated_at",
    ]
    detail = dict(zip(columns, row))
    for key in ("created_at", "updated_at"):
        if detail.get(key) is not None:
            detail[key] = detail[key].isoformat()
    return detail


def fetch_sap_invoice_failures(
    conn: psycopg.Connection, *, limit: int = 50
) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit), 100))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*)::int FROM walmart_sap_invoice_log WHERE status = 'failed'"
        )
        total = int(cur.fetchone()[0])
        cur.execute(
            """
            SELECT
              purchase_order_id,
              customer_order_id,
              error_message,
              error_phase,
              attempt_count,
              updated_at
            FROM walmart_sap_invoice_log
            WHERE status = 'failed'
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (safe_limit,),
        )
        items = _rows_to_dicts(cur)
    for item in items:
        if item.get("updated_at") is not None:
            item["updated_at"] = item["updated_at"].isoformat()
    return {"total": total, "items": items, "limit": safe_limit}


def fetch_sync_log(conn: psycopg.Connection, *, limit: int) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, sync_started_at, sync_finished_at,
                   last_modified_start_date, last_modified_end_date,
                   pages_fetched, orders_seen, lines_stored, error_message
            FROM walmart_log
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return _rows_to_dicts(cur)


def json_response(status_code: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }
