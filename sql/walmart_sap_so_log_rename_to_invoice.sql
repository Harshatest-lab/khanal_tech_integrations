-- Rename legacy SAP SO log table to invoice naming (run once if walmart_sap_so_log exists).

ALTER TABLE IF EXISTS walmart_sap_so_log RENAME TO walmart_sap_invoice_log;

ALTER INDEX IF EXISTS walmart_sap_so_log_pkey RENAME TO walmart_sap_invoice_log_pkey;
ALTER INDEX IF EXISTS walmart_sap_so_log_purchase_order_id_key
  RENAME TO walmart_sap_invoice_log_purchase_order_id_key;
ALTER INDEX IF EXISTS idx_walmart_sap_so_log_status RENAME TO idx_walmart_sap_invoice_log_status;
ALTER INDEX IF EXISTS idx_walmart_sap_so_log_updated_at RENAME TO idx_walmart_sap_invoice_log_updated_at;

ALTER SEQUENCE IF EXISTS walmart_sap_so_log_id_seq RENAME TO walmart_sap_invoice_log_id_seq;
