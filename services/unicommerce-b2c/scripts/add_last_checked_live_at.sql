-- One-off DDL: this app's DB role has no ALTER TABLE grant (least-privilege,
-- RDS hardening 2026-08-18), so this must be run once against each database
-- (dev's unicommerce_dev schema and prod) by a privileged connection before
-- the 2026-08-28 promote_fetched_to_ready fix can work. See
-- src/unicommerce/store.py's defer_fetched/mark_live_checked docstrings and
-- src/unicommerce/fetch_sync.py's promote_fetched_to_ready for why this
-- column has to be separate from updated_at.
--
-- Additive and nullable -- no backfill needed. NULL means "never verified
-- live", which get_fetched_orders' ORDER BY ... NULLS FIRST already treats
-- as highest priority, so every existing 'fetched' row is naturally
-- eligible for an immediate live recheck the first time this fix runs.

ALTER TABLE unicommerce_orders
  ADD COLUMN IF NOT EXISTS last_checked_live_at timestamptz;
