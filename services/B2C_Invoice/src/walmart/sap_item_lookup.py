"""Resolve Walmart SKU to SAP ItemCode via RDS cache, then HANA fallback."""

from __future__ import annotations

from typing import Any

import psycopg

from walmart.sap_fg_item_store import upsert_sap_fg_items
from walmart.sap_hana import lookup_item_by_wm_sku

SELECT_BY_WM_SKU = """
SELECT item_code, amz_sku, wm_sku
FROM sap_fg_items
WHERE lower(trim(wm_sku)) = lower(trim(%(wm_sku)s))
LIMIT 1
"""


def resolve_item_code(
    conn: psycopg.Connection,
    wm_sku: str,
    *,
    allow_hana_lookup: bool = True,
) -> str:
    """Return SAP ``ItemCode`` for a Walmart marketplace SKU."""
    sku = (wm_sku or "").strip()
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
            f"Set OITM.U_WM_SKU (or U_Amz_SKU) in SAP to this value, then re-run "
            f"scripts/load_sap_fg_items.py or preview again."
        )

    upsert_sap_fg_items(conn, [hana_item], commit=False)
    return str(hana_item["item_code"]).strip()
