"""FEFO batch allocation from HANA for the AMZ-FBA warehouse.

Same API the standalone pipeline used (open_connection / close /
find_item_by_amz_sku / allocate_batches_fefo), but connections come from
``khanal_shared.hana.pool`` — the Lambda-tuned pooled hdbcli factory the
other services run — configured from plain env vars (HANA_HOST /
HANA_PORT / HANA_USER / HANA_PASSWORD / HANA_SCHEMA). Query SQL is
byte-identical to the standalone version.
"""

from __future__ import annotations

from khanal_shared.hana.pool import get_pool

from amazon_uk import config


class HanaUnavailableError(Exception):
    """HANA unreachable or the query failed. Callers must NOT mark the order
    failed for this — leave it untouched so the next run retries it."""


def open_connection():
    """Check out one pooled connection for a whole posting run — reused
    across every line instead of reconnecting per item. Caller must close()
    when done (which returns it to the pool)."""
    pool = get_pool()
    if pool is None:
        raise HanaUnavailableError(
            "HANA not configured (set HANA_HOST / HANA_USER / HANA_PASSWORD)"
        )
    try:
        conn = pool.get_connection()
    except Exception as e:
        raise HanaUnavailableError(f"HANA connection failed: {e}") from e
    return conn, conn.cursor()


def close(conn, cursor) -> None:
    """Return the pooled connection. Never raises — a close hiccup must not
    lose a posting result."""
    try:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
    except Exception:
        pass


def find_item_by_amz_sku(cursor, seller_sku: str) -> dict | None:
    """Live item-master fallback for a SKU missing from amazonuk.sku_item_map
    — looks up OITM."U_Amz_SKU" directly via HANA (reusing the connection
    already open for FEFO, no extra SAP Service Layer call). Column name is
    genuinely mixed-case ("U_Amz_SKU", not "U_AMZ_SKU") — confirmed live
    2026-09-17 against schema UK_TEST_16092026, 63 items populated. A
    frozen or non-valid item is not a match even if the UDF is still set.
    Returns {"sap_item_code", "item_description"} or None.
    Raises HanaUnavailableError if the query itself fails.
    """
    schema = config.get("HANA_SCHEMA")
    try:
        cursor.execute(
            f"""
            SELECT "ItemCode", "ItemName" FROM "{schema}"."OITM"
            WHERE "U_Amz_SKU" = ? AND "validFor" = 'Y' AND "frozenFor" = 'N'
            """,
            [seller_sku],
        )
        row = cursor.fetchone()
    except Exception as e:
        raise HanaUnavailableError(f"Item-master lookup failed for {seller_sku}: {e}") from e
    return {"sap_item_code": row[0], "item_description": row[1]} if row else None


def allocate_batches_fefo(cursor, item_code: str, required_qty: float, warehouse: str) -> list[dict]:
    """FEFO batch allocation for item_code from warehouse.
    Returns [] only if the item has NO batch records at all in this
    warehouse (genuinely not batch-tracked there) — caller omits
    BatchNumbers in that case.
    Raises HanaUnavailableError if the query itself fails.
    Raises ValueError if HANA is reachable but available stock < required_qty
    — including when every batch record exists but sits at zero quantity
    (confirmed live 2026-09-16: item FGDC1132 had two batch rows at
    AMZ-FBA, both qty 0 — the old query filtered on Quantity > 0, so it
    saw zero rows and silently treated a genuinely batch-managed,
    out-of-stock item as "not batch-managed", omitting BatchNumbers
    entirely; SAP then rejected the line with "Cannot add row without
    complete selection of batch/serial numbers" instead of a clear
    insufficient-stock error). Now every batch record is fetched
    regardless of quantity, so a real batch-managed item with zero stock
    still produces `rows` (proving it needs batches) and correctly raises
    ValueError below rather than being mistaken for a non-batch item.
    """
    schema = config.get("HANA_SCHEMA")
    try:
        cursor.execute(
            f"""
            SELECT T1."DistNumber" AS "BatchNumber", T0."Quantity" AS "AvailQty"
            FROM "{schema}"."OBTQ" T0
            JOIN "{schema}"."OBTN" T1
                ON T0."ItemCode" = T1."ItemCode" AND T0."SysNumber" = T1."SysNumber"
            WHERE T0."ItemCode" = ? AND T0."WhsCode" = ?
            ORDER BY T1."ExpDate" ASC NULLS LAST, T1."InDate" ASC
            """,
            [item_code, warehouse],
        )
        rows = cursor.fetchall() or []
    except Exception as e:
        raise HanaUnavailableError(f"FEFO query failed for {item_code}: {e}") from e

    if not rows:
        return []

    allocated = []
    remaining = float(required_qty)
    for batch_no, avail in rows:
        if remaining <= 0:
            break
        avail = float(avail or 0)
        if avail <= 0:
            continue
        take = min(avail, remaining)
        allocated.append({"BatchNumber": str(batch_no).strip(), "Quantity": take})
        remaining -= take

    if round(remaining, 3) > 0:
        total_avail = sum(float(r[1] or 0) for r in rows)
        raise ValueError(
            f"Insufficient stock for {item_code} in {warehouse}: "
            f"need {required_qty}, available {total_avail:.2f}"
        )
    return allocated
