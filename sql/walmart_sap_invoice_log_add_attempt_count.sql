-- Track how many times SAP invoice sync was attempted per PO (run once).

ALTER TABLE walmart_sap_invoice_log
  ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0;

-- Backfill: rows that already have a log entry count as at least one attempt.
UPDATE walmart_sap_invoice_log
SET attempt_count = GREATEST(attempt_count, 1)
WHERE attempt_count = 0;
