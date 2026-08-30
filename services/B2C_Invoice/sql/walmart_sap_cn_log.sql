-- SAP A/R Credit Note log for Walmart returns (run once per environment).

CREATE TABLE IF NOT EXISTS walmart_sap_cn_log (
  id                  bigserial   PRIMARY KEY,
  return_order_id     text        NOT NULL UNIQUE,
  customer_order_id   text,
  status              text        NOT NULL DEFAULT 'prepared',
  -- prepared | posting | posted | failed | skipped
  sap_payload         jsonb,
  sap_doc_entry       integer,
  sap_doc_num         integer,
  base_doc_entry      integer,
  error_message       text,
  error_phase         text,
  skip_reason         text,
  sap_response        jsonb,
  attempt_count       integer     NOT NULL DEFAULT 0,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_walmart_sap_cn_log_status
  ON walmart_sap_cn_log (status);

CREATE INDEX IF NOT EXISTS idx_walmart_sap_cn_log_updated_at
  ON walmart_sap_cn_log (updated_at DESC);
