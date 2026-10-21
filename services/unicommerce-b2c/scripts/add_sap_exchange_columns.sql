-- One-off DDL (this app's DB role cannot ALTER, so run it once per database, dev and prod, with a privileged
-- connection). Keeps what SAP actually answered, so the dashboard's "SAP result" tab can show it.
-- Safe to run before or after deploying: the code writes these columns in a separate best-effort step and
-- carries on without them, so a missing column can never fail a posting.

ALTER TABLE unicommerce_orders     ADD COLUMN IF NOT EXISTS sap_response_json jsonb;  -- SAP's reply to the AR Invoice POST
ALTER TABLE unicommerce_cn_staging ADD COLUMN IF NOT EXISTS payload_json      jsonb;  -- Credit Note we sent
ALTER TABLE unicommerce_cn_staging ADD COLUMN IF NOT EXISTS sap_response_json jsonb;  -- SAP's reply to the Credit Note POST
