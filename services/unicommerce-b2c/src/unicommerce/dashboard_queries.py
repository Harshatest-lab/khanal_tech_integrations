"""Read-only queries for the Unicommerce dashboard API.

Mirrors amazon-us-b2c/src/amazon_us/dashboard_queries.py's shape (same
clamp helpers, same {total, items, limit, offset} envelope) against the
unicommerce_orders / unicommerce_cn_staging / unicommerce_sync_log
schema. All read-only -- no SAP or UC calls, no writes.
"""

from __future__ import annotations

from typing import Any

import psycopg

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_ORDER_SORT_COLUMNS = {
    "order_date": "order_date",
    "updated_at": "updated_at",
    "created_at": "created_at",
    "uc_total": "uc_total",
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


# A "cancelled" order is one Unicommerce itself cancelled (status CANCELLED).
# The pipeline never posts these (mappers.order_ready_for_invoice requires
# COMPLETE), so they sit in 'fetched' forever and inflate every count that
# is meant to show work-in-flight. An order that is already 'posted' is never
# treated as cancelled here: if a posted order later turns CANCELLED in UC
# that is a real anomaly (an SAP invoice exists) and must stay visible.
CANCELLED_SQL = "(UPPER(COALESCE(status, '')) LIKE 'CANCEL%%' AND pipeline_status <> 'posted')"


def _rows_to_dicts(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    columns = [desc[0] for desc in cur.description] if cur.description else []
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def fetch_stats(conn: psycopg.Connection) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT pipeline_status, COUNT(*)::int AS count FROM unicommerce_orders GROUP BY pipeline_status")
        by_status = {r["pipeline_status"]: r["count"] for r in _rows_to_dicts(cur)}

        cur.execute("SELECT COUNT(*) FROM unicommerce_orders")
        total_orders = int(cur.fetchone()[0])

        cur.execute("SELECT channel_name, COUNT(*)::int AS count FROM unicommerce_orders GROUP BY channel_name")
        by_channel = {r["channel_name"]: r["count"] for r in _rows_to_dicts(cur)}

        cur.execute("SELECT status, COUNT(*)::int AS count FROM unicommerce_cn_staging GROUP BY status")
        cn_by_status = {r["status"]: r["count"] for r in _rows_to_dicts(cur)}

        # One grouped pass splits every order into cancelled vs active, so the
        # "excluding cancelled" view below is always internally consistent
        # (active + cancelled == total_orders).
        cur.execute(
            f"""
            SELECT pipeline_status, channel_name, {CANCELLED_SQL} AS cancelled, COUNT(*)::int AS count
            FROM unicommerce_orders
            GROUP BY pipeline_status, channel_name, {CANCELLED_SQL}
            """
        )
        cancelled_orders = 0
        active_orders = 0
        active_by_status: dict[str, int] = {}
        active_by_channel: dict[str, int] = {}
        for r in _rows_to_dicts(cur):
            if r["cancelled"]:
                cancelled_orders += r["count"]
                continue
            active_orders += r["count"]
            active_by_status[r["pipeline_status"]] = active_by_status.get(r["pipeline_status"], 0) + r["count"]
            active_by_channel[r["channel_name"]] = active_by_channel.get(r["channel_name"], 0) + r["count"]

        cur.execute(
            """
            SELECT DISTINCT ON (mode) mode, status, started_at, finished_at,
                   window_from, window_to, counts, error_message
            FROM unicommerce_sync_log
            ORDER BY mode, started_at DESC
            """
        )
        last_run_by_mode = {r["mode"]: r for r in _rows_to_dicts(cur)}

    return {
        "total_orders": total_orders,
        "by_pipeline_status": by_status,
        "by_channel": by_channel,
        "cn_by_status": cn_by_status,
        "last_run_by_mode": last_run_by_mode,
        # Additive: every field above is unchanged, so an older frontend keeps
        # working. The console reads these to show counts that leave out orders
        # Unicommerce cancelled (never posted, so not "work in flight").
        "cancelled_orders": cancelled_orders,
        "excluding_cancelled": {
            "total_orders": active_orders,
            "by_pipeline_status": active_by_status,
            "by_channel": active_by_channel,
        },
    }


def fetch_orders(
    conn: psycopg.Connection,
    *,
    limit: int,
    offset: int,
    pipeline_status: str | None = None,
    channel_name: str | None = None,
    uniware_id: str | None = None,
    order_date_from: str | None = None,
    order_date_to: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    exclude_cancelled: bool = False,
    order_status: str | None = None,
) -> dict[str, Any]:
    where = []
    params: list[Any] = []
    if exclude_cancelled:
        where.append(f"NOT {CANCELLED_SQL}")
    if order_status:
        where.append("UPPER(status) = %s")
        params.append(order_status.strip().upper())
    if pipeline_status:
        where.append("pipeline_status = %s")
        params.append(pipeline_status)
    if channel_name:
        where.append("channel_name = %s")
        params.append(channel_name)
    if uniware_id:
        where.append("uniware_id = %s")
        params.append(uniware_id)
    if order_date_from:
        where.append("order_date >= %s")
        params.append(order_date_from)
    if order_date_to:
        where.append("order_date <= %s")
        params.append(order_date_to)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    sort_col = _ORDER_SORT_COLUMNS.get(sort_by or "", "updated_at")
    sort_direction = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"

    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM unicommerce_orders {where_sql}", params)
        total = int(cur.fetchone()[0])

        cur.execute(
            f"""
            SELECT uniware_id, channel_name, pipeline_status, order_date, status,
                   customer_name, city, state, uc_total,
                   sap_ar_invoice_docentry, sap_ar_invoice_docnum,
                   ar_invoice_tax_diff, ar_invoice_unit_price_diff, ar_invoice_reconciled,
                   attempt_count, last_error, updated_at
            FROM unicommerce_orders
            {where_sql}
            ORDER BY {sort_col} {sort_direction}
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        )
        items = _rows_to_dicts(cur)

    return {"total": total, "items": items, "limit": limit, "offset": offset}


def fetch_order_detail(conn: psycopg.Connection, uniware_id: str) -> dict[str, Any]:
    """SELECT * on all three tables -- every stored column, not a curated
    subset. Includes the large jsonb blobs (order_json, payload_json,
    shippingpackages, returnitems) as-is; the dashboard renders those as
    raw/expandable JSON rather than picking fields out of them."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM unicommerce_orders WHERE uniware_id = %s", (uniware_id,))
        header_rows = _rows_to_dicts(cur)
        header = header_rows[0] if header_rows else None

        lines: list[dict[str, Any]] = []
        cn_rows: list[dict[str, Any]] = []
        if header is not None:
            cur.execute("SELECT * FROM unicommerce_order_lines WHERE uniware_id = %s ORDER BY id", (uniware_id,))
            lines = _rows_to_dicts(cur)

            cur.execute(
                "SELECT * FROM unicommerce_cn_staging WHERE uc_order_id = %s ORDER BY created_at DESC",
                (uniware_id,),
            )
            cn_rows = _rows_to_dicts(cur)

    return {"header": header, "lines": lines, "credit_notes": cn_rows}


def fetch_returns(conn: psycopg.Connection, *, limit: int, status: str | None = None) -> dict[str, Any]:
    where = []
    params: list[Any] = []
    if status:
        where.append("status = %s")
        params.append(status)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM unicommerce_cn_staging {where_sql}", params)
        total = int(cur.fetchone()[0])
        cur.execute(
            f"""
            SELECT uc_return_id, uc_order_id, parent_sap_doc_entry, return_type, return_date,
                   status, sap_cn_docentry, sap_cn_docnum, attempt_count, last_error, updated_at, items_json
            FROM unicommerce_cn_staging
            {where_sql}
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            [*params, limit],
        )
        items = _rows_to_dicts(cur)
    return {"total": total, "items": items, "limit": limit}


def fetch_flex_cn_log(conn: psycopg.Connection, *, limit: int, status: str | None = None) -> dict[str, Any]:
    """AMAZON_FLEX's own Credit Note log -- a separate table
    (unicommerce_flex_cn_log) from unicommerce_cn_staging every other
    channel's returns use, since Flex refunds come from Amazon's own
    Finances API (flex_refunds.py), not a UC return record. Same shape as
    fetch_returns so callers/reports can treat both sources uniformly."""
    where = []
    params: list[Any] = []
    if status:
        where.append("status = %s")
        params.append(status)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM unicommerce_flex_cn_log {where_sql}", params)
        total = int(cur.fetchone()[0])
        cur.execute(
            f"""
            SELECT refund_id, amazon_order_id, posted_date, status, doc_entry, doc_num,
                   base_doc_entry, attempt_count, error, skip_reason, updated_at
            FROM unicommerce_flex_cn_log
            {where_sql}
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            [*params, limit],
        )
        items = _rows_to_dicts(cur)
    return {"total": total, "items": items, "limit": limit}


def fetch_sync_log(conn: psycopg.Connection, *, limit: int, mode: str | None = None) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if mode:
        where.append("mode = %s")
        params.append(mode)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, mode, status, started_at, finished_at, window_from, window_to, counts, error_message
            FROM unicommerce_sync_log
            {where_sql}
            ORDER BY started_at DESC
            LIMIT %s
            """,
            [*params, limit],
        )
        return _rows_to_dicts(cur)


def fetch_recon_gaps(conn: psycopg.Connection, *, limit: int, min_diff: float = 1.0) -> dict[str, Any]:
    """Posted orders whose SAP invoice tax or unit-price total differs from
    expected (diffs are computed inline at post time and refreshed
    periodically by Recon -- read-only view of them)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT uniware_id, channel_name, sap_ar_invoice_docentry,
                   ar_invoice_tax_expected, ar_invoice_tax_actual, ar_invoice_tax_diff,
                   ar_invoice_unit_price_expected, ar_invoice_unit_price_actual, ar_invoice_unit_price_diff,
                   ar_invoice_mismatch_reason, updated_at
            FROM unicommerce_orders
            WHERE pipeline_status = 'posted'
              AND (COALESCE(ar_invoice_tax_diff, 0) >= %s OR COALESCE(ar_invoice_unit_price_diff, 0) >= %s)
            ORDER BY GREATEST(COALESCE(ar_invoice_tax_diff, 0), COALESCE(ar_invoice_unit_price_diff, 0)) DESC,
                     updated_at DESC
            LIMIT %s
            """,
            (min_diff, min_diff, limit),
        )
        items = _rows_to_dicts(cur)
    return {"items": items, "limit": limit, "min_diff": min_diff}
