"""Resolve Walmart SKU to SAP ItemCode via RDS cache, then HANA fallback."""

from __future__ import annotations

from typing import Any

import psycopg

from walmart.credentials import DATABASE_URL
from walmart.sap_fg_item_store import upsert_sap_fg_items
from walmart.sap_hana import lookup_item_by_wm_sku

SELECT_BY_WM_SKU = """
SELECT item_code, amz_sku, wm_sku
FROM sap_fg_items
WHERE lower(trim(both from replace(replace(coalesce(wm_sku, ''), E'\\r', ''), E'\\n', '')))
    = lower(trim(both from replace(replace(%(wm_sku)s, E'\\r', ''), E'\\n', '')))
LIMIT 1
"""


def _normalize_sku(value: str) -> str:
    """Strip whitespace and CR/LF that often appear in SAP UDF values."""
    return (value or "").replace("\r", "").replace("\n", "").strip()


def _persist_fg_item_cache(item: dict[str, Any]) -> None:
    """Write HANA hit into RDS on its own connection.

    Must not use the invoice-sync connection: that connection is rolled back on
    later build/post failures, which would erase the cache row.
    """
    if not DATABASE_URL:
        return
    with psycopg.connect(DATABASE_URL) as cache_conn:
        upsert_sap_fg_items(cache_conn, [item], commit=True)


def resolve_item_code(
    conn: psycopg.Connection,
    wm_sku: str,
    *,
    allow_hana_lookup: bool = True,
) -> str:
    """Return SAP ``ItemCode`` for a Walmart marketplace SKU."""
    sku = _normalize_sku(wm_sku)
    if not sku:
        raise ValueError("Walmart SKU is empty")

    with conn.cursor() as cur:
        cur.execute(SELECT_BY_WM_SKU, {"wm_sku": sku})
        row = cur.fetchone()

    if row:
        item_code = str(row[0]).strip()
        if item_code:
            return item_code

    if not allow_hana_lookup:
        raise ValueError(f"No SAP item mapping for Walmart SKU {sku!r} in sap_fg_items")

    hana_item = lookup_item_by_wm_sku(sku)
    if not hana_item or not hana_item.get("item_code"):
        raise ValueError(
            f"No SAP item mapping for Walmart SKU {sku!r}. "
            f"Set OITM.U_WM_SKU in SAP to this value, then re-run "
            f"scripts/load_sap_fg_items.py or preview again."
        )

    # Cache independently so sync rollback cannot undo the HANA → RDS write.
    try:
        _persist_fg_item_cache(hana_item)
    except Exception:
        # Still invoice with the HANA ItemCode even if cache write fails.
        upsert_sap_fg_items(conn, [hana_item], commit=False)

    return str(hana_item["item_code"]).strip()
