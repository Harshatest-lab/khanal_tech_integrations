"""Read-only queries for the Walmart orders dashboard API."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from walmart.credentials import DATABASE_URL
from walmart.dates import ms_to_pacific_date
from walmart.order_pricing import line_display_status, line_status_quantities
from walmart.walmart_store import line_ship_date_time_ms, order_shipping_state

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


def _utc_day_start_ts(date_str: str) -> datetime:
    return datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _utc_day_end_exclusive_ts(date_str: str) -> datetime:
    return _utc_day_start_ts(date_str) + timedelta(days=1)


SORT_KEYS = {
    "po": "h.purchase_order_id",
    "order": "h.customer_order_id",
    "order_date": "sort_ms",
    "order_date_pacific": "sort_ms",
    "item": "sort_product_name",
    "status": "sort_status",
    "sku": "sort_sku",
    "qty": "sort_qty_min",
    "status_date": "sort_status_date_ms",
    "updated": "header_updated_at",
    "sap_sync": "sap_sync_status",
}

_COUNTY_SURCHARGE_TAX_PATTERN = "%County Surcharge Tax%"


def _qty_numeric_sql(column: str) -> str:
    return (
        f"NULLIF(regexp_replace(COALESCE({column}, ''), '[^0-9.]', '', 'g'), '')::numeric"
    )


def _parse_quantity_filter(raw: str | None) -> float | None:
    if not raw or not raw.strip():
        return None
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _sort_column(sort_key: str, sort_direction: str) -> str:
    if sort_key == "qty":
        return "sort_qty_max" if sort_direction == "desc" else "sort_qty_min"
    return SORT_KEYS[sort_key]


def _parse_sort(sort_by: str | None, sort_dir: str | None) -> tuple[str, str]:
    key = (sort_by or "order_date").strip().lower()
    direction = (sort_dir or "desc").strip().lower()
    if direction not in {"asc", "desc"}:
        direction = "desc"
    if key not in SORT_KEYS:
        key = "order_date"
    return key, direction


def _order_filter_clauses(
    *,
    status: str | None = None,
    purchase_order_id: str | None = None,
    customer_order_id: str | None = None,
    order_date_from: str | None = None,
    order_date_to: str | None = None,
    has_shipping: str | None = None,
    county_surcharge_tax: str | None = None,
    promotion_discount: str | None = None,
    line_count: str | None = None,
    sap_sync: str | None = None,
    sku: str | None = None,
    product_name: str | None = None,
    quantity: str | None = None,
    status_date_from: str | None = None,
    status_date_to: str | None = None,
    updated_from: str | None = None,
    updated_to: str | None = None,
    has_return: str | None = None,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if purchase_order_id and purchase_order_id.strip():
        clauses.append("h.purchase_order_id ILIKE %s")
        params.append(f"%{purchase_order_id.strip()}%")

    if customer_order_id and customer_order_id.strip():
        clauses.append("h.customer_order_id ILIKE %s")
        params.append(f"%{customer_order_id.strip()}%")

    if sku and sku.strip():
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM walmart_order_lines lf
                WHERE lf.purchase_order_id = h.purchase_order_id
                  AND lf.sku ILIKE %s
            )
            """
        )
        params.append(f"%{sku.strip()}%")

    if product_name and product_name.strip():
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM walmart_order_lines lf
                WHERE lf.purchase_order_id = h.purchase_order_id
                  AND lf.product_name ILIKE %s
            )
            """
        )
        params.append(f"%{product_name.strip()}%")

    if quantity and quantity.strip():
        qty_num = _parse_quantity_filter(quantity)
        if qty_num is not None:
            qty_sql = _qty_numeric_sql("lf.quantity_amount")
            clauses.append(
                f"""
            EXISTS (
                SELECT 1
                FROM walmart_order_lines lf
                WHERE lf.purchase_order_id = h.purchase_order_id
                  AND {qty_sql} = %s
            )
            """
            )
            params.append(qty_num)
        else:
            clauses.append(
                """
            EXISTS (
                SELECT 1
                FROM walmart_order_lines lf
                WHERE lf.purchase_order_id = h.purchase_order_id
                  AND TRIM(lf.quantity_amount) = %s
            )
            """
            )
            params.append(quantity.strip())

    if status_date_from and status_date_from.strip():
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM walmart_order_lines lf
                WHERE lf.purchase_order_id = h.purchase_order_id
                  AND lf.status_date >= %s
            )
            """
        )
        params.append(_utc_day_start_ts(status_date_from.strip()))

    if status_date_to and status_date_to.strip():
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM walmart_order_lines lf
                WHERE lf.purchase_order_id = h.purchase_order_id
                  AND lf.status_date < %s
            )
            """
        )
        params.append(_utc_day_end_exclusive_ts(status_date_to.strip()))

    if updated_from and updated_from.strip():
        clauses.append("h.updated_at >= %s")
        params.append(_utc_day_start_ts(updated_from.strip()))

    if updated_to and updated_to.strip():
        clauses.append("h.updated_at < %s")
        params.append(_utc_day_end_exclusive_ts(updated_to.strip()))

    if status and status.strip().lower() in {"partial", "delivered_partial"}:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof(h.raw_payload #> '{order,orderLines,orderLine}') = 'array'
                            THEN h.raw_payload #> '{order,orderLines,orderLine}'
                        WHEN h.raw_payload #> '{order,orderLines,orderLine}' IS NOT NULL
                            THEN jsonb_build_array(h.raw_payload #> '{order,orderLines,orderLine}')
                        ELSE '[]'::jsonb
                    END
                ) AS partial_line
                WHERE EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(
                        CASE
                            WHEN jsonb_typeof(partial_line #> '{orderLineStatuses,orderLineStatus}') = 'array'
                                THEN partial_line #> '{orderLineStatuses,orderLineStatus}'
                            WHEN partial_line #> '{orderLineStatuses,orderLineStatus}' IS NOT NULL
                                THEN jsonb_build_array(partial_line #> '{orderLineStatuses,orderLineStatus}')
                            ELSE '[]'::jsonb
                        END
                    ) AS fulfilled_status
                    WHERE fulfilled_status->>'status' IN ('Shipped', 'Delivered', 'Dispensed')
                      AND COALESCE(
                            NULLIF(
                                regexp_replace(
                                    COALESCE(fulfilled_status #>> '{statusQuantity,amount}', ''),
                                    '[^0-9.]', '', 'g'
                                ),
                                ''
                            ),
                            '0'
                          )::numeric > 0
                )
                AND EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(
                        CASE
                            WHEN jsonb_typeof(partial_line #> '{orderLineStatuses,orderLineStatus}') = 'array'
                                THEN partial_line #> '{orderLineStatuses,orderLineStatus}'
                            WHEN partial_line #> '{orderLineStatuses,orderLineStatus}' IS NOT NULL
                                THEN jsonb_build_array(partial_line #> '{orderLineStatuses,orderLineStatus}')
                            ELSE '[]'::jsonb
                        END
                    ) AS cancelled_status
                    WHERE cancelled_status->>'status' IN ('Cancelled', 'Canceled')
                      AND COALESCE(
                            NULLIF(
                                regexp_replace(
                                    COALESCE(cancelled_status #>> '{statusQuantity,amount}', ''),
                                    '[^0-9.]', '', 'g'
                                ),
                                ''
                            ),
                            '0'
                          )::numeric > 0
                )
            )
            """
        )
    elif status:
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

    surcharge = (county_surcharge_tax or "").strip().lower()
    if surcharge in {"yes", "true", "1", "with"}:
        clauses.append("h.raw_payload::text ILIKE %s")
        params.append(_COUNTY_SURCHARGE_TAX_PATTERN)
    elif surcharge in {"no", "false", "0", "without"}:
        clauses.append("h.raw_payload::text NOT ILIKE %s")
        params.append(_COUNTY_SURCHARGE_TAX_PATTERN)

    promo = (promotion_discount or "").strip().lower()
    if promo in {"yes", "true", "1", "with"}:
        clauses.append("h.has_promotion_discount = true")
    elif promo in {"no", "false", "0", "without"}:
        clauses.append("h.has_promotion_discount = false")

    lines_filter = (line_count or "").strip().lower()
    if lines_filter in {"single", "1", "one"}:
        clauses.append(
            """
            (SELECT COUNT(*)::int FROM walmart_order_lines lc
             WHERE lc.purchase_order_id = h.purchase_order_id) = 1
            """
        )
    elif lines_filter in {"multiple", "multi", "2plus", "2+"}:
        clauses.append(
            """
            (SELECT COUNT(*)::int FROM walmart_order_lines lc
             WHERE lc.purchase_order_id = h.purchase_order_id) > 1
            """
        )

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

    has_ret = (has_return or "").strip().lower()
    if has_ret in {"yes", "true", "1"}:
        clauses.append("h.has_return = true")
    elif has_ret in {"no", "false", "0"}:
        clauses.append("h.has_return = false")

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


def _raw_line_quantities(raw_payload: Any, line_number: Any) -> dict[str, Any]:
    parsed = _parse_raw_payload(raw_payload)
    order = (parsed or {}).get("order") or {}
    raw_lines = (order.get("orderLines") or {}).get("orderLine")
    lines = raw_lines if isinstance(raw_lines, list) else ([raw_lines] if raw_lines else [])
    for raw_line in lines:
        if str((raw_line or {}).get("lineNumber")) == str(line_number):
            return {
                **line_status_quantities(raw_line),
                "status": line_display_status(raw_line),
            }
    return {}


def _group_rows_by_po(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    current_po: str | None = None
    current: dict[str, Any] | None = None

    for row in rows:
        po = row["purchase_order_id"]
        quantities = _raw_line_quantities(row.get("raw_payload"), row.get("line_number"))
        line = {
            "line_number": row.get("line_number"),
            "status": quantities.get("status", row.get("status")),
            "status_date": row.get("status_date"),
            "sku": row.get("sku"),
            "product_name": row.get("product_name"),
            "quantity_amount": row.get("quantity_amount"),
            "delivered_quantity": quantities.get("delivered"),
            "invoiceable_quantity": quantities.get("invoiceable"),
            "cancelled_quantity": quantities.get("cancelled"),
            "is_partial": quantities.get("is_partial", False),
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
                "has_return": bool(row.get("has_return")),
                "ship_to_state": row.get("ship_to_state"),
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
    customer_order_id: str | None = None,
    order_date_from: str | None = None,
    order_date_to: str | None = None,
    has_shipping: str | None = None,
    county_surcharge_tax: str | None = None,
    promotion_discount: str | None = None,
    line_count: str | None = None,
    sap_sync: str | None = None,
    sku: str | None = None,
    product_name: str | None = None,
    quantity: str | None = None,
    status_date_from: str | None = None,
    status_date_to: str | None = None,
    updated_from: str | None = None,
    updated_to: str | None = None,
    has_return: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
) -> dict[str, Any]:
    sort_key, sort_direction = _parse_sort(sort_by, sort_dir)
    sort_column = _sort_column(sort_key, sort_direction)
    nulls = "NULLS LAST"

    clauses, params = _order_filter_clauses(
        status=status,
        purchase_order_id=purchase_order_id,
        customer_order_id=customer_order_id,
        order_date_from=order_date_from,
        order_date_to=order_date_to,
        has_shipping=has_shipping,
        county_surcharge_tax=county_surcharge_tax,
        promotion_discount=promotion_discount,
        line_count=line_count,
        sap_sync=sap_sync,
        sku=sku,
        product_name=product_name,
        quantity=quantity,
        status_date_from=status_date_from,
        status_date_to=status_date_to,
        updated_from=updated_from,
        updated_to=updated_to,
        has_return=has_return,
    )
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM walmart_order_headers h {where_sql}",
            params,
        )
        total = int(cur.fetchone()[0])

        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM walmart_order_lines l
            WHERE l.purchase_order_id IN (
                SELECT h.purchase_order_id
                FROM walmart_order_headers h
                {where_sql}
            )
            """,
            params,
        )
        total_lines = int(cur.fetchone()[0])

        data_params = [*params, limit, offset]
        sql = f"""
            WITH po_candidates AS (
                SELECT
                    h.purchase_order_id,
                    h.customer_order_id,
                    h.order_date_ms,
                    h.updated_at AS header_updated_at,
                    COALESCE(h.order_date_ms, MAX(l.status_date_ms)) AS sort_ms,
                    MIN(l.product_name) AS sort_product_name,
                    MIN(l.status) AS sort_status,
                    MIN(l.sku) AS sort_sku,
                    MIN({_qty_numeric_sql("l.quantity_amount")}) AS sort_qty_min,
                    MAX({_qty_numeric_sql("l.quantity_amount")}) AS sort_qty_max,
                    MAX(l.status_date_ms) AS sort_status_date_ms,
                    MAX(sap.status) AS sap_sync_status
                FROM walmart_order_headers h
                INNER JOIN walmart_order_lines l
                  ON l.purchase_order_id = h.purchase_order_id
                LEFT JOIN walmart_sap_invoice_log sap
                  ON sap.purchase_order_id = h.purchase_order_id
                {where_sql}
                GROUP BY
                    h.purchase_order_id,
                    h.customer_order_id,
                    h.order_date_ms,
                    h.updated_at
            ),
            po_page AS (
                SELECT purchase_order_id, page_rank
                FROM (
                    SELECT
                        purchase_order_id,
                        ROW_NUMBER() OVER (
                            ORDER BY {sort_column} {sort_direction.upper()} {nulls},
                                     purchase_order_id ASC
                        ) AS page_rank
                    FROM po_candidates
                ) ranked
                ORDER BY page_rank
                LIMIT %s OFFSET %s
            )
            SELECT
                h.purchase_order_id,
                h.customer_order_id,
                h.ship_node_type,
                h.order_date_ms,
                h.has_shipping_charges,
                h.has_return,
                h.ship_to_state,
                h.raw_payload,
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
            ORDER BY p.page_rank, l.line_number
        """
        cur.execute(sql, data_params)
        items = _group_rows_by_po(_rows_to_dicts(cur))

    return {
        "items": items,
        "count": len(items),
        "total": total,
        "total_lines": total_lines,
        "offset": offset,
        "limit": limit,
        "sort_by": sort_key,
        "sort_dir": sort_direction,
    }


def _parse_raw_payload(raw_payload: Any) -> dict[str, Any] | None:
    if not raw_payload:
        return None
    try:
        parsed = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_lines_by_number(order_json: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    order = (order_json or {}).get("order") or {}
    raw_lines = (order.get("orderLines") or {}).get("orderLine")
    if not raw_lines:
        return {}
    if isinstance(raw_lines, list):
        lines = raw_lines
    else:
        lines = [raw_lines]
    by_number: dict[str, dict[str, Any]] = {}
    for line in lines:
        if not isinstance(line, dict):
            continue
        line_number = line.get("lineNumber")
        if line_number is not None:
            by_number[str(line_number)] = line
    return by_number


def _enrich_lines_ship_dates(
    lines: list[dict[str, Any]],
    order_json: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Fill ship_date_* from raw_payload when DB columns are still null (pre-deploy sync)."""
    json_by_line = _json_lines_by_number(order_json)
    enriched: list[dict[str, Any]] = []
    for line in lines:
        row = dict(line)
        if row.get("ship_date_time_ms") is None:
            json_line = json_by_line.get(str(row.get("line_number")))
            if json_line:
                ship_ms = line_ship_date_time_ms(json_line)
                if ship_ms is not None:
                    row["ship_date_time_ms"] = ship_ms
                    row["ship_date_pacific"] = ms_to_pacific_date(ship_ms).isoformat()
        enriched.append(row)
    return enriched


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
                                    **(
                                        {"shipDateTime": line.get("ship_date_time_ms")}
                                        if line.get("ship_date_time_ms") is not None
                                        else {}
                                    ),
                                }
                                if line.get("tracking_number")
                                or line.get("carrier_name")
                                or line.get("ship_date_time_ms") is not None
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
                h.has_return,
                h.ship_to_state,
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
            "delivered_quantity": _raw_line_quantities(
                header.get("raw_payload"), row.get("line_number")
            ).get("delivered"),
            "invoiceable_quantity": _raw_line_quantities(
                header.get("raw_payload"), row.get("line_number")
            ).get("invoiceable"),
            "cancelled_quantity": _raw_line_quantities(
                header.get("raw_payload"), row.get("line_number")
            ).get("cancelled"),
            "is_partial": _raw_line_quantities(
                header.get("raw_payload"), row.get("line_number")
            ).get("is_partial", False),
            "tracking_number": row.get("tracking_number"),
            "tracking_url": row.get("tracking_url"),
            "carrier_name": row.get("carrier_name"),
            "updated_at": row.get("line_updated_at"),
        }
        for row in rows
        if row.get("line_number") is not None
    ]
    for line in lines:
        quantities = _raw_line_quantities(
            header.get("raw_payload"), line.get("line_number")
        )
        if quantities.get("status"):
            line["status"] = quantities["status"]

    order_json = _order_json_from_payload(_parse_raw_payload(header.get("raw_payload")))
    if order_json is None and lines:
        order_json = _order_json_from_rows(header, lines)

    lines = _enrich_lines_ship_dates(lines, order_json)

    ship_to_state = header.get("ship_to_state")
    if not ship_to_state and order_json:
        ship_to_state = order_shipping_state(order_json.get("order") or {})

    sap_sync = fetch_sap_invoice_sync_detail(conn, purchase_order_id)
    linked_returns = fetch_returns_for_purchase_order(conn, purchase_order_id)

    result = {
        "purchase_order_id": purchase_order_id,
        "customer_order_id": header.get("customer_order_id"),
        "ship_node_type": header.get("ship_node_type"),
        "order_date_ms": header.get("order_date_ms"),
        "has_shipping_charges": header.get("has_shipping_charges"),
        "has_return": bool(header.get("has_return")),
        "ship_to_state": ship_to_state,
        "line_count": len(lines),
        "lines": lines,
        "raw_payload": header.get("raw_payload"),
        "order_json": order_json,
        "sap_sync": sap_sync,
        "returns": linked_returns,
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


def fetch_sap_cn_sync_detail(
    conn: psycopg.Connection, return_order_id: str
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              return_order_id,
              customer_order_id,
              status,
              sap_doc_entry,
              sap_doc_num,
              base_doc_entry,
              error_message,
              error_phase,
              skip_reason,
              sap_payload,
              sap_response,
              attempt_count,
              created_at,
              updated_at
            FROM walmart_sap_cn_log
            WHERE return_order_id = %s
            """,
            (return_order_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None

    columns = [
        "return_order_id",
        "customer_order_id",
        "status",
        "sap_doc_entry",
        "sap_doc_num",
        "base_doc_entry",
        "error_message",
        "error_phase",
        "skip_reason",
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


def _serialize_dt(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _return_lines_from_raw(raw_json: Any) -> list[dict[str, Any]]:
    parsed = _parse_raw_payload(raw_json)
    if not parsed:
        return []
    raw_lines = parsed.get("returnOrderLines") or []
    if not isinstance(raw_lines, list):
        raw_lines = [raw_lines] if raw_lines else []

    lines: list[dict[str, Any]] = []
    for line in raw_lines:
        if not isinstance(line, dict):
            continue
        item = line.get("item") or {}
        qty = line.get("quantity") or {}
        unit_price = line.get("unitPrice") or {}
        lines.append(
            {
                "return_order_line_number": line.get("returnOrderLineNumber"),
                "purchase_order_id": line.get("purchaseOrderId"),
                "sku": item.get("sku"),
                "product_name": item.get("productName"),
                "return_reason": line.get("returnReason"),
                "quantity": qty.get("measurementValue"),
                "unit_price": unit_price.get("currencyAmount"),
                "unit_price_currency": unit_price.get("currencyUnit"),
                "status": line.get("status"),
                "status_time": line.get("statusTime"),
                "current_delivery_status": line.get("currentDeliveryStatus"),
                "current_refund_status": line.get("currentRefundStatus"),
            }
        )
    return lines


def _return_row_public(row: dict[str, Any], *, include_raw: bool = False) -> dict[str, Any]:
    out = {
        "return_order_id": row.get("return_order_id"),
        "customer_order_id": row.get("customer_order_id"),
        "return_order_date": _serialize_dt(row.get("return_order_date")),
        "refund_mode": row.get("refund_mode"),
        "total_refund_amount": row.get("total_refund_amount"),
        "total_refund_currency": row.get("total_refund_currency"),
        "purchase_order_ids": row.get("purchase_order_ids"),
        "return_reasons": row.get("return_reasons"),
        "status": row.get("status"),
        "status_time": _serialize_dt(row.get("status_time")),
        "current_delivery_status": row.get("current_delivery_status"),
        "current_refund_status": row.get("current_refund_status"),
        "credit_note_status": row.get("credit_note_status"),
        "ar_invoice_posted": bool(row.get("ar_invoice_posted")),
        "fetched_at": _serialize_dt(row.get("fetched_at")),
        "created_at": _serialize_dt(row.get("created_at")),
        "updated_at": _serialize_dt(row.get("updated_at")),
    }
    if include_raw:
        out["raw_json"] = row.get("raw_json")
        out["return_order_lines"] = _return_lines_from_raw(row.get("raw_json"))
    return out


def _returns_filter_clauses(
    *,
    return_order_id: str | None = None,
    purchase_order_id: str | None = None,
    status: str | None = None,
    current_refund_status: str | None = None,
    current_delivery_status: str | None = None,
    credit_note_status: str | None = None,
    return_date_from: str | None = None,
    return_date_to: str | None = None,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if return_order_id and return_order_id.strip():
        clauses.append("r.return_order_id ILIKE %s")
        params.append(f"%{return_order_id.strip()}%")

    if purchase_order_id and purchase_order_id.strip():
        clauses.append("r.purchase_order_ids ILIKE %s")
        params.append(f"%{purchase_order_id.strip()}%")

    if status and status.strip():
        clauses.append("r.status = %s")
        params.append(status.strip())

    if current_refund_status and current_refund_status.strip():
        clauses.append("r.current_refund_status = %s")
        params.append(current_refund_status.strip())

    if current_delivery_status and current_delivery_status.strip():
        clauses.append("r.current_delivery_status = %s")
        params.append(current_delivery_status.strip())

    if credit_note_status and credit_note_status.strip():
        clauses.append("r.credit_note_status = %s")
        params.append(credit_note_status.strip())

    if return_date_from and return_date_from.strip():
        clauses.append("r.return_order_date >= %s")
        params.append(_utc_day_start_ts(return_date_from.strip()))

    if return_date_to and return_date_to.strip():
        clauses.append("r.return_order_date < %s")
        params.append(_utc_day_end_exclusive_ts(return_date_to.strip()))

    return clauses, params


def fetch_returns(
    conn: psycopg.Connection,
    *,
    limit: int,
    offset: int = 0,
    return_order_id: str | None = None,
    purchase_order_id: str | None = None,
    status: str | None = None,
    current_refund_status: str | None = None,
    current_delivery_status: str | None = None,
    credit_note_status: str | None = None,
    return_date_from: str | None = None,
    return_date_to: str | None = None,
) -> dict[str, Any]:
    clauses, params = _returns_filter_clauses(
        return_order_id=return_order_id,
        purchase_order_id=purchase_order_id,
        status=status,
        current_refund_status=current_refund_status,
        current_delivery_status=current_delivery_status,
        credit_note_status=credit_note_status,
        return_date_from=return_date_from,
        return_date_to=return_date_to,
    )
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*)::int FROM walmart_returns r {where_sql}",
            params,
        )
        total = int(cur.fetchone()[0])

        cur.execute(
            f"""
            SELECT
              COUNT(*) FILTER (WHERE r.status = 'INITIATED')::int AS initiated_count,
              COUNT(*) FILTER (WHERE r.status = 'COMPLETED')::int AS completed_count
            FROM walmart_returns r
            {where_sql}
            """,
            params,
        )
        status_row = cur.fetchone() or (0, 0)
        initiated_count = int(status_row[0] or 0)
        completed_count = int(status_row[1] or 0)

        cur.execute(
            f"""
            SELECT
              r.return_order_id,
              r.customer_order_id,
              r.return_order_date,
              r.refund_mode,
              r.total_refund_amount,
              r.total_refund_currency,
              r.purchase_order_ids,
              r.return_reasons,
              r.status,
              r.status_time,
              r.current_delivery_status,
              r.current_refund_status,
              r.credit_note_status,
              r.fetched_at,
              r.created_at,
              r.updated_at,
              EXISTS (
                SELECT 1
                FROM walmart_sap_invoice_log s
                INNER JOIN walmart_order_headers h
                  ON h.purchase_order_id = s.purchase_order_id
                WHERE h.customer_order_id = r.customer_order_id
                  AND s.status = 'posted'
              ) AS ar_invoice_posted
            FROM walmart_returns r
            {where_sql}
            ORDER BY r.return_order_date DESC NULLS LAST, r.return_order_id ASC
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        )
        rows = _rows_to_dicts(cur)

    items = [_return_row_public(row) for row in rows]
    return {
        "items": items,
        "count": len(items),
        "total": total,
        "offset": offset,
        "limit": limit,
        "initiated_count": initiated_count,
        "completed_count": completed_count,
    }


def fetch_return_detail(
    conn: psycopg.Connection, return_order_id: str
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              r.return_order_id,
              r.customer_order_id,
              r.return_order_date,
              r.refund_mode,
              r.total_refund_amount,
              r.total_refund_currency,
              r.purchase_order_ids,
              r.return_reasons,
              r.status,
              r.status_time,
              r.current_delivery_status,
              r.current_refund_status,
              r.credit_note_status,
              r.raw_json,
              r.fetched_at,
              r.created_at,
              r.updated_at,
              EXISTS (
                SELECT 1
                FROM walmart_sap_invoice_log s
                INNER JOIN walmart_order_headers h
                  ON h.purchase_order_id = s.purchase_order_id
                WHERE h.customer_order_id = r.customer_order_id
                  AND s.status = 'posted'
              ) AS ar_invoice_posted
            FROM walmart_returns r
            WHERE r.return_order_id = %s
            """,
            (return_order_id,),
        )
        rows = _rows_to_dicts(cur)
    if not rows:
        return None
    detail = _return_row_public(rows[0], include_raw=True)
    detail["cn_sync"] = fetch_sap_cn_sync_detail(conn, return_order_id)
    return detail


def fetch_returns_for_purchase_order(
    conn: psycopg.Connection, purchase_order_id: str
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              return_order_id,
              customer_order_id,
              return_order_date,
              refund_mode,
              total_refund_amount,
              total_refund_currency,
              purchase_order_ids,
              return_reasons,
              status,
              status_time,
              current_delivery_status,
              current_refund_status,
              credit_note_status,
              fetched_at,
              created_at,
              updated_at
            FROM walmart_returns
            WHERE purchase_order_ids ILIKE %s
            ORDER BY return_order_date DESC NULLS LAST, return_order_id ASC
            """,
            (f"%{purchase_order_id}%",),
        )
        rows = _rows_to_dicts(cur)
    return [_return_row_public(row) for row in rows]


def fetch_returns_sync_log(
    conn: psycopg.Connection, *, limit: int
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              id,
              status,
              sync_mode,
              from_date,
              to_date,
              record_count,
              max_status_time_ms,
              next_last_modified_start,
              message,
              created_at
            FROM walmart_return_sync_log
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = _rows_to_dicts(cur)
    for row in rows:
        row["from_date"] = _serialize_dt(row.get("from_date"))
        row["to_date"] = _serialize_dt(row.get("to_date"))
        row["created_at"] = _serialize_dt(row.get("created_at"))
    return rows


from walmart.http_response import json_response
