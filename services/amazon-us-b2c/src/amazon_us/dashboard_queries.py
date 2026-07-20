"""Read-only queries + thin action wrappers for the Amazon US dashboard API."""

from __future__ import annotations

from typing import Any

import psycopg

from amazon_us.store import connect as get_db_connection

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_ORDER_SORT_COLUMNS = {
    "purchase_date": "purchase_date",
    "actual_ship_date": "actual_ship_date",
    "updated_at": "updated_at",
    "order_total_amount": "order_total_amount",
}


def _clamp_limit(raw: str | None, default: int = DEFAULT_LIMIT) -> int:
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_LIMIT))


def _clamp_offset(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _rows_to_dicts(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    columns = [desc[0] for desc in cur.description] if cur.description else []
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def fetch_stats(conn: psycopg.Connection) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stage, COUNT(*)::int AS count FROM amazon_us_orders GROUP BY stage"
        )
        by_stage = {r["stage"]: r["count"] for r in _rows_to_dicts(cur)}

        cur.execute("SELECT COUNT(*) FROM amazon_us_orders")
        total_orders = int(cur.fetchone()[0])

        cur.execute(
            "SELECT status, COUNT(*)::int AS count FROM amazon_us_cn_log GROUP BY status"
        )
        cn_by_status = {r["status"]: r["count"] for r in _rows_to_dicts(cur)}

        cur.execute(
            """
            SELECT DISTINCT ON (mode) mode, status, started_at, finished_at,
                   window_from, window_to, counts, error_message
            FROM amazon_us_sync_log
            ORDER BY mode, started_at DESC
            """
        )
        last_run_by_mode = {r["mode"]: r for r in _rows_to_dicts(cur)}

    return {
        "total_orders": total_orders,
        "by_stage": by_stage,
        "cn_by_status": cn_by_status,
        "last_run_by_mode": last_run_by_mode,
    }


def fetch_orders(
    conn: psycopg.Connection,
    *,
    limit: int,
    offset: int,
    stage: str | None = None,
    amazon_order_id: str | None = None,
    order_date_from: str | None = None,
    order_date_to: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
) -> dict[str, Any]:
    where = []
    params: list[Any] = []
    if stage:
        where.append("stage = %s")
        params.append(stage)
    if amazon_order_id:
        where.append("amazon_order_id = %s")
        params.append(amazon_order_id)
    if order_date_from:
        where.append("purchase_date >= %s")
        params.append(order_date_from)
    if order_date_to:
        where.append("purchase_date <= %s")
        params.append(order_date_to)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    sort_col = _ORDER_SORT_COLUMNS.get(sort_by or "", "updated_at")
    sort_direction = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"

    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM amazon_us_orders {where_sql}", params)
        total = int(cur.fetchone()[0])

        cur.execute(
            f"""
            SELECT amazon_order_id, order_status, stage, purchase_date, actual_ship_date,
                   order_total_amount, ship_city, ship_state, ship_country, buyer_name,
                   sap_doc_entry, sap_doc_num, posted_at, last_error, attempt_count, updated_at
            FROM amazon_us_orders
            {where_sql}
            ORDER BY {sort_col} {sort_direction}
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        )
        items = _rows_to_dicts(cur)

    return {"total": total, "items": items, "limit": limit, "offset": offset}


def fetch_order_detail(conn: psycopg.Connection, amazon_order_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM amazon_us_orders WHERE amazon_order_id = %s", (amazon_order_id,)
        )
        header_rows = _rows_to_dicts(cur)
        header = header_rows[0] if header_rows else None

        cur.execute(
            """
            SELECT seller_sku, asin, title, quantity, unit_price, item_price_amount,
                   item_tax_amount, shipping_price_amount, shipping_tax_amount
            FROM amazon_us_order_lines
            WHERE amazon_order_id = %s
            ORDER BY id
            """,
            (amazon_order_id,),
        )
        lines = _rows_to_dicts(cur)

        cur.execute(
            """
            SELECT status, sap_doc_entry, sap_doc_num, error_message, created_at
            FROM amazon_us_invoice_log
            WHERE amazon_order_id = %s
            ORDER BY created_at DESC
            """,
            (amazon_order_id,),
        )
        invoice_log = _rows_to_dicts(cur)

        cur.execute(
            """
            SELECT refund_id, posted_date, status, skip_reason, sap_doc_entry, sap_doc_num,
                   error_message, updated_at
            FROM amazon_us_cn_log
            WHERE amazon_order_id = %s
            ORDER BY updated_at DESC
            """,
            (amazon_order_id,),
        )
        cn_log = _rows_to_dicts(cur)

    return {"header": header, "lines": lines, "invoice_log": invoice_log, "cn_log": cn_log}


def fetch_invoice_failures(conn: psycopg.Connection, *, limit: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM amazon_us_orders WHERE stage = 'failed'")
        total = int(cur.fetchone()[0])

        cur.execute(
            """
            SELECT amazon_order_id, order_status, purchase_date, actual_ship_date,
                   last_error, attempt_count, updated_at
            FROM amazon_us_orders
            WHERE stage = 'failed'
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        items = _rows_to_dicts(cur)
    return {"total": total, "items": items, "limit": limit}


def fetch_cn_log(conn: psycopg.Connection, *, limit: int, status: str | None = None) -> dict[str, Any]:
    where_sql = "WHERE status = %s" if status else ""
    params = [status] if status else []
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM amazon_us_cn_log {where_sql}", params)
        total = int(cur.fetchone()[0])

        cur.execute(
            f"""
            SELECT refund_id, amazon_order_id, posted_date, status, skip_reason,
                   sap_doc_entry, sap_doc_num, error_message, attempt_count, updated_at
            FROM amazon_us_cn_log
            {where_sql}
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            [*params, limit],
        )
        items = _rows_to_dicts(cur)
    return {"total": total, "items": items, "limit": limit}


def fetch_sync_log(conn: psycopg.Connection, *, limit: int, mode: str | None = None) -> list[dict[str, Any]]:
    where_sql = "WHERE mode = %s" if mode else ""
    params = [mode] if mode else []
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, mode, status, started_at, finished_at, window_from, window_to,
                   counts, error_message
            FROM amazon_us_sync_log
            {where_sql}
            ORDER BY id DESC
            LIMIT %s
            """,
            [*params, limit],
        )
        return _rows_to_dicts(cur)
