-- Monthly posting-period lock/unlock for unicommerce-b2c, mirroring SAP B1's
-- own posting-period lock concept but controlled entirely from our own
-- dashboard/DB (does not read SAP's own period-lock state). One row per
-- month that has ever been locked, kept for audit history on unlock rather
-- than deleted. See docs/superpowers/specs/ for the design writeup.
-- Run once against each DB: psql "$DATABASE_URL" -f sql/unicommerce_period_locks.sql

CREATE TABLE IF NOT EXISTS unicommerce_period_locks (
  period       text        PRIMARY KEY,   -- 'YYYY-MM'
  locked       boolean     NOT NULL DEFAULT true,
  locked_by    text        NOT NULL DEFAULT '',
  locked_at    timestamptz NOT NULL DEFAULT now(),
  unlocked_by  text,
  unlocked_at  timestamptz,
  note         text        NOT NULL DEFAULT '',
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_unicommerce_period_locks_locked
  ON unicommerce_period_locks (locked)
  WHERE locked;
