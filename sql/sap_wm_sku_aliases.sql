-- Temporary Walmart SKU aliases when one SAP item maps to multiple Walmart SKUs.
-- Not touched by HANA sync (load_sap_fg_items.py). Run once:
--   psql "$DATABASE_URL" -f sql/sap_wm_sku_aliases.sql
--
-- Example (duplicate listing until Walmart removes the extra SKU):
--   INSERT INTO sap_wm_sku_aliases (wm_sku, item_code, note)
--   VALUES ('Turmeric Medium', 'FGDC0835', 'Temp alias until Walmart removes duplicate')
--   ON CONFLICT (wm_sku) DO UPDATE SET item_code = EXCLUDED.item_code, note = EXCLUDED.note;

CREATE TABLE IF NOT EXISTS sap_wm_sku_aliases (
  wm_sku     text PRIMARY KEY,
  item_code  text NOT NULL,
  note       text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sap_wm_sku_aliases_item_code
  ON sap_wm_sku_aliases (item_code);
