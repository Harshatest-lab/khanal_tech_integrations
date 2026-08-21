-- One-off DDL: this app's DB role has no CREATE TABLE grant (least-privilege,
-- RDS hardening 2026-08-18), so this must be run once against each database
-- (dev and prod) by a privileged connection before flex_refunds.post_flex_refunds
-- can run. See src/unicommerce/flex_refunds.py / store.py's Flex refund
-- credit-note functions.

CREATE TABLE IF NOT EXISTS unicommerce_flex_cn_log (
    refund_id text PRIMARY KEY,
    amazon_order_id text NOT NULL,
    posted_date date,
    status text NOT NULL DEFAULT 'pending',
    doc_entry text,
    doc_num text,
    base_doc_entry text,
    payload_json jsonb,
    response_json jsonb,
    error text,
    skip_reason text,
    attempt_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_flex_cn_log_order ON unicommerce_flex_cn_log (amazon_order_id);

GRANT SELECT, INSERT, UPDATE ON unicommerce_flex_cn_log TO unicommerce_b2c_prod_app;
-- If running against dev, replace the GRANT target with the dev app role
-- (e.g. unicommerce_b2c_dev_app / whatever UC_DEV_DATABASE_URL's role is).
