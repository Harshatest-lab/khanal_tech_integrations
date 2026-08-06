"""Read-only SAP HANA queries for Walmart B2C."""

from __future__ import annotations

from typing import Any

from khanal_shared.hana import get_hana_params, hana_connection, qualified_table

DEFAULT_WAREHOUSE = "WM-FBA"


def _schema() -> str | None:
    params = get_hana_params()
    return params.schema if params else None


def _clean_sku(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).replace("\r", "").replace("\n", "").strip()
    return cleaned or None


def _fg_items_sql() -> str:
    oitm = qualified_table(_schema(), "OITM", "a")
    return f"""
SELECT a."ItemCode", a."U_Amz_SKU", a."U_WM_SKU"
FROM {oitm}
WHERE a."ItemCode" LIKE 'FG%'
ORDER BY a."ItemCode"
"""


def _lookup_by_wm_sku_sql() -> str:
    oitm = qualified_table(_schema(), "OITM", "a")
    # REPLACE CR/LF — SAP U_WM_SKU values sometimes contain trailing/leading \\r.
    return f"""
SELECT a."ItemCode", a."U_Amz_SKU", a."U_WM_SKU"
FROM {oitm}
WHERE UPPER(TRIM(REPLACE(REPLACE(IFNULL(a."U_WM_SKU", ''), CHAR(13), ''), CHAR(10), '')))
    = UPPER(TRIM(REPLACE(REPLACE(?, CHAR(13), ''), CHAR(10), '')))
"""


def _batch_sql() -> str:
    schema = _schema()
    obtq = qualified_table(schema, "OBTQ", "T0")
    obtb = qualified_table(schema, "OBTN", "T1")
    return f"""
SELECT T1."DistNumber" AS "BatchNumber", T0."Quantity" AS "AvailQty"
FROM {obtq}
JOIN {obtb}
  ON T0."ItemCode" = T1."ItemCode" AND T0."SysNumber" = T1."SysNumber"
WHERE T0."ItemCode" = ? AND T0."WhsCode" = ? AND T0."Quantity" > 0
ORDER BY T1."ExpDate" ASC NULLS LAST, T1."InDate" ASC
"""


def fetch_fg_items() -> dict[str, Any]:
    """Finished-goods items from SAP OITM with Amazon / Walmart SKU UDFs."""
    params = get_hana_params()
    with hana_connection() as conn:
        cur = conn.cursor()
        cur.execute(_fg_items_sql())
        columns = [col[0] for col in (cur.description or [])]
        rows = cur.fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        raw = dict(zip(columns, row))
        items.append(
            {
                "item_code": raw.get("ItemCode"),
                "amz_sku": _clean_sku(raw.get("U_Amz_SKU")),
                "wm_sku": _clean_sku(raw.get("U_WM_SKU")),
            }
        )

    return {
        "items": items,
        "count": len(items),
        "database": params.database if params else None,
        "schema": params.schema if params else None,
    }


def lookup_item_by_wm_sku(wm_sku: str) -> dict[str, Any] | None:
    """Find OITM row by Walmart SKU (``U_WM_SKU`` only).

    1) Direct HANA SQL (with CR/LF stripped)
    2) Fallback: same FG list the dashboard uses, match cleaned ``U_WM_SKU``
       in Python (handles odd characters SQL TRIM misses).
    """
    sku = _clean_sku(wm_sku)
    if not sku:
        return None
    sku_key = sku.upper()

    with hana_connection() as conn:
        cur = conn.cursor()
        cur.execute(_lookup_by_wm_sku_sql(), (sku,))
        row = cur.fetchone()
        if row:
            columns = [col[0] for col in (cur.description or [])]
            raw = dict(zip(columns, row))
            if raw.get("ItemCode"):
                return {
                    "item_code": raw.get("ItemCode"),
                    "amz_sku": _clean_sku(raw.get("U_Amz_SKU")),
                    "wm_sku": _clean_sku(raw.get("U_WM_SKU")) or sku,
                }

    # Dashboard path works; reuse it when parameterized match misses.
    for item in fetch_fg_items().get("items") or []:
        if (_clean_sku(item.get("wm_sku")) or "").upper() == sku_key:
            return {
                "item_code": item.get("item_code"),
                "amz_sku": item.get("amz_sku"),
                "wm_sku": item.get("wm_sku") or sku,
            }
    return None


def fetch_batches_for_item(
    item_code: str,
    *,
    warehouse_code: str = DEFAULT_WAREHOUSE,
) -> list[dict[str, Any]]:
    """FEFO batches for an item in a warehouse (earliest expiry first)."""
    code = (item_code or "").strip()
    if not code:
        return []

    with hana_connection() as conn:
        cur = conn.cursor()
        cur.execute(_batch_sql(), (code, warehouse_code))
        columns = [col[0] for col in (cur.description or [])]
        rows = cur.fetchall()

    batches: list[dict[str, Any]] = []
    for row in rows:
        raw = dict(zip(columns, row))
        qty = raw.get("AvailQty")
        try:
            avail = float(qty) if qty is not None else 0.0
        except (TypeError, ValueError):
            avail = 0.0
        if avail <= 0:
            continue
        batches.append(
            {
                "batch_number": raw.get("BatchNumber"),
                "avail_qty": avail,
            }
        )
    return batches
