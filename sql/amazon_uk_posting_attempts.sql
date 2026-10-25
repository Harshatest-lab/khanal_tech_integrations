-- Amazon UK: stop retrying a failing invoice / credit note forever.
-- Run ONCE per environment BEFORE deploying the code that uses these columns:
--   psql "$DB_URL" -f sql/amazon_uk_posting_attempts.sql
-- Existing failed rows start at 0 attempts, so they get a fresh 5 tries.

ALTER TABLE amazonuk.sap_postings
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS dead     BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE amazonuk.sap_credit_notes
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS dead     BOOLEAN NOT NULL DEFAULT false;
