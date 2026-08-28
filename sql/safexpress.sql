-- SafeExpress POD/waybill tracking (run once against the dedicated
-- safexpress_prod database — NOT the shared khanal_serverless database;
-- see docs/architecture/PLATFORM_ARCHITECTURE.md §5 for why this service
-- gets its own database rather than a schema in the shared one).
--
-- Deliberately thin: one row per AR Invoice DocEntry, tracking/POD state
-- and retry bookkeeping only. This is NOT an invoice replica — no
-- customer, totals, or line-item columns — SAP stays the source of truth
-- for all of that. See services/safexpress/src/safexpress/store.py.

CREATE TABLE IF NOT EXISTS safexpress_pod_tracking (
  doc_entry       integer     PRIMARY KEY,      -- SAP AR Invoice DocEntry
  doc_num         integer     NOT NULL,
  series_name     text        NOT NULL DEFAULT '',
  waybill_number  text        NOT NULL DEFAULT '',
  pod_url         text,
  pod_date        date,
  pod_status      text        NOT NULL DEFAULT 'pending',
    -- pending | waybill_found | pending_sap | completed | no_waybill | failed | stale
    -- stale = given up after STALE_AFTER_DAYS open with no resolution;
    -- revived to pending if a later HANA scan rediscovers it (still no
    -- U_TrackingNo in SAP) — see safexpress/store.py.
  last_error      text,
  attempt_count   integer     NOT NULL DEFAULT 0,
  checked_at      timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_safexpress_pod_tracking_status
  ON safexpress_pod_tracking (pod_status);
CREATE INDEX IF NOT EXISTS idx_safexpress_pod_tracking_updated_at
  ON safexpress_pod_tracking (updated_at DESC);

-- Least-privilege role for the Lambda's own DATABASE_URL, same pattern as
-- amazon_us_b2c_prod_app / unicommerce_b2c_prod_app (RDS hardening,
-- 2026-08-18). Create the role separately (CREATE ROLE safexpress_prod_app
-- LOGIN PASSWORD '...') before running this grant.
GRANT SELECT, INSERT, UPDATE ON safexpress_pod_tracking TO safexpress_prod_app;
