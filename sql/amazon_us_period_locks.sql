-- Monthly posting-period lock for amazon-us-b2c, same design as unicommerce_period_locks: controlled from our own
-- dashboard/DB (it does not read SAP's own period-lock state). One row per month that has ever been locked, kept
-- for audit history on unlock rather than deleted. A locked month blocks invoices (by ship date) and credit notes
-- (by refund date) from being sent to SAP.
-- Run once on amazon_us_b2c_prod BEFORE deploying the Lambdas that check it:
--   psql "$DATABASE_URL" -f sql/amazon_us_period_locks.sql

CREATE TABLE IF NOT EXISTS amazon_us_period_locks (
  period       text        PRIMARY KEY,   -- 'YYYY-MM'
  locked       boolean     NOT NULL DEFAULT true,
  locked_by    text        NOT NULL DEFAULT '',
  locked_at    timestamptz NOT NULL DEFAULT now(),
  unlocked_by  text,
  unlocked_at  timestamptz,
  note         text        NOT NULL DEFAULT '',
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_amazon_us_period_locks_locked
  ON amazon_us_period_locks (locked)
  WHERE locked;

-- The dashboards backend only reads this table (it locks/unlocks by invoking the Lambda), so nothing to grant it
-- beyond the SELECT it already has on the other tables:
--   GRANT SELECT ON amazon_us_period_locks TO unicommerce_b2c_prod_app;
