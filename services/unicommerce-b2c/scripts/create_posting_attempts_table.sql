-- One-off DDL: run once per database (dev and prod) with a privileged connection; the app role cannot create
-- tables. Append-only history of every attempt to post an invoice or credit note to SAP, so a retry no longer
-- overwrites what the previous attempt sent and what SAP answered. Safe to run before or after deploying: the
-- code records attempts in a separate best-effort step and carries on without the table.

CREATE TABLE IF NOT EXISTS unicommerce_posting_attempts (
    id            bigserial PRIMARY KEY,
    doc_type      text        NOT NULL,   -- 'invoice' | 'credit_note'
    doc_id        text        NOT NULL,   -- uniware_id for an invoice, uc_return_id for a credit note
    outcome       text        NOT NULL,   -- 'posted' | 'failed' | 'deferred' (timeout: SAP may have processed it)
    sap_doc_entry text,
    request_json  jsonb,                  -- the document we sent (absent when the payload could not be built)
    response_json jsonb,                  -- SAP's reply when it gave one
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_posting_attempts_doc ON unicommerce_posting_attempts (doc_type, doc_id, created_at DESC);

-- The posting app writes; the dashboards backend only reads. Adjust the role names if yours differ.
GRANT SELECT, INSERT ON unicommerce_posting_attempts TO unicommerce_b2c_prod_app;
GRANT USAGE ON SEQUENCE unicommerce_posting_attempts_id_seq TO unicommerce_b2c_prod_app;
-- GRANT SELECT ON unicommerce_posting_attempts TO <the role in the dashboards backend's UNICOMMERCE_DATABASE_URL>;
