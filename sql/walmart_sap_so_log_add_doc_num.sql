-- Extend SAP SO log with DocNum and error phase (run once on existing DB).

ALTER TABLE walmart_sap_so_log
  ADD COLUMN IF NOT EXISTS sap_doc_num integer,
  ADD COLUMN IF NOT EXISTS error_phase text,
  ADD COLUMN IF NOT EXISTS sap_response jsonb;
