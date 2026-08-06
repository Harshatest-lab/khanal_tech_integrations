"""Load SAP FG items from HANA into Postgres — local scripts only (not used by Lambda)."""

from __future__ import annotations

from typing import Any

import psycopg

from walmart.credentials import DATABASE_URL
from walmart.sap_hana import fetch_fg_items

UPSERT_SQL = """
INSERT INTO sap_fg_items (item_code, amz_sku, wm_sku, updated_at)
VALUES (%(item_code)s, %(amz_sku)s, %(wm_sku)s, now())
ON CONFLICT (item_code) DO UPDATE SET
  amz_sku = EXCLUDED.amz_sku,
  wm_sku = EXCLUDED.wm_sku,
  updated_at = now()
"""


def upsert_sap_fg_items(
    conn: psycopg.Connection, items: list[dict[str, Any]], *, commit: bool = True
) -> int:
    """Upsert rows into ``sap_fg_items``. Returns number of rows written."""
    if not items:
        return 0

    def _clean(value: Any) -> str | None:
        if value is None:
            return None
        cleaned = str(value).replace("\r", "").replace("\n", "").strip()
        return cleaned or None

    rows = [
        {
            "item_code": item.get("item_code"),
            "amz_sku": _clean(item.get("amz_sku")),
            "wm_sku": _clean(item.get("wm_sku")),
        }
        for item in items
        if item.get("item_code")
    ]

    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, rows)
    if commit:
        conn.commit()
    return len(rows)


def load_sap_fg_items_from_hana() -> dict[str, Any]:
    """Fetch FG items from SAP HANA and store them in Postgres."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set (env or credentials_local.py)")

    hana_data = fetch_fg_items()
    items = hana_data.get("items") or []

    with psycopg.connect(DATABASE_URL) as conn:
        stored = upsert_sap_fg_items(conn, items)

    return {
        "fetched_from_hana": len(items),
        "stored_in_postgres": stored,
        "hana_database": hana_data.get("database"),
        "hana_schema": hana_data.get("schema"),
    }
