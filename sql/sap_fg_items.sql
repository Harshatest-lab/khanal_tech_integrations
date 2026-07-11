-- SAP finished-goods SKU map (local sync from HANA OITM → Postgres).
-- Run once: psql "$DATABASE_URL" -f sql/sap_fg_items.sql

CREATE TABLE IF NOT EXISTS sap_fg_items (
  item_code   text        PRIMARY KEY,
  amz_sku     text,
  wm_sku      text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sap_fg_items_wm_sku
  ON sap_fg_items (wm_sku)
  WHERE wm_sku IS NOT NULL AND wm_sku <> '';

CREATE INDEX IF NOT EXISTS idx_sap_fg_items_amz_sku
  ON sap_fg_items (amz_sku)
  WHERE amz_sku IS NOT NULL AND amz_sku <> '';
