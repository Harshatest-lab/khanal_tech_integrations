-- SAP A/R Invoice payloads prepared/posted for Walmart B2C (run once per environment).

CREATE TABLE IF NOT EXISTS walmart_sap_invoice_log (
  id                  bigserial   PRIMARY KEY,
  purchase_order_id   text        NOT NULL UNIQUE,
  customer_order_id   text,
  status              text        NOT NULL DEFAULT 'prepared',
  sap_payload         jsonb       NOT NULL,
  sap_doc_entry       integer,
  sap_doc_num         integer,
  error_message       text,
  error_phase         text,
  sap_response        jsonb,
  attempt_count       integer     NOT NULL DEFAULT 0,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_walmart_sap_invoice_log_status
  ON walmart_sap_invoice_log (status);

CREATE INDEX IF NOT EXISTS idx_walmart_sap_invoice_log_updated_at
  ON walmart_sap_invoice_log (updated_at DESC);
