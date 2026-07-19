-- County Surcharge Tax flag for dashboard filters (run once on existing DB).
ALTER TABLE walmart_order_headers
  ADD COLUMN IF NOT EXISTS has_county_surcharge_tax boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_walmart_order_headers_county_surcharge
  ON walmart_order_headers (has_county_surcharge_tax);

-- Backfill from stored Walmart JSON (re-sync will refine via order_pricing).
UPDATE walmart_order_headers
SET has_county_surcharge_tax = true
WHERE has_county_surcharge_tax = false
  AND raw_payload::text ILIKE '%County Surcharge Tax%';

