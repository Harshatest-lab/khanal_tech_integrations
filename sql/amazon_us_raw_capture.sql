-- Amazon US B2C: keep everything Amazon sent us, and every credit-note attempt (run once per environment).
-- Both tables are append-only. Writes are best effort in the Lambdas: a failure to save here never stops a sync
-- or a posting.

-- Every record fetched from Amazon, exactly as received.
--   source: order | order_items | orders_report_rows | fba_shipment_rows | refund_event
--   ref:    the Amazon order id (refund id for refund_event)
-- payload_hash makes it de-duplicated by content: fetching an unchanged record again adds no row, while a
-- changed one (status moved, items added, refund grew) is kept as a new version. amazon_us_orders.raw_payload
-- is overwritten on each refresh; this table is the history.
CREATE TABLE IF NOT EXISTS amazon_us_raw_events (
  id           bigserial   PRIMARY KEY,
  source       text        NOT NULL,
  ref          text        NOT NULL,
  payload_hash text        NOT NULL,
  payload      jsonb       NOT NULL,
  fetched_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, ref, payload_hash)
);
CREATE INDEX IF NOT EXISTS idx_amazon_us_raw_events_ref ON amazon_us_raw_events (ref, source, fetched_at DESC);

-- Every credit-note attempt with what was sent to SAP and SAP's answer. amazon_us_cn_log keeps only the latest
-- state of each refund; this keeps the history (status: posted | skipped | failed | failed_permanent).
CREATE TABLE IF NOT EXISTS amazon_us_cn_attempts (
  id              bigserial   PRIMARY KEY,
  refund_id       text        NOT NULL,
  amazon_order_id text        NOT NULL,
  status          text        NOT NULL,
  sap_doc_entry   integer,
  sap_doc_num     integer,
  sap_payload     jsonb,
  sap_response    jsonb,
  error_message   text,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_amazon_us_cn_attempts_order ON amazon_us_cn_attempts (amazon_order_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_amazon_us_cn_attempts_refund ON amazon_us_cn_attempts (refund_id, created_at DESC);

-- Run after creating the tables:
--   1. the Amazon Lambdas' database role needs INSERT on both tables and USAGE on their sequences:
--        GRANT INSERT, SELECT ON amazon_us_raw_events, amazon_us_cn_attempts TO <lambda_role>;
--        GRANT USAGE ON SEQUENCE amazon_us_raw_events_id_seq, amazon_us_cn_attempts_id_seq TO <lambda_role>;
--   2. the dashboards backend reads them:
--        GRANT SELECT ON amazon_us_raw_events, amazon_us_cn_attempts TO unicommerce_b2c_prod_app;
