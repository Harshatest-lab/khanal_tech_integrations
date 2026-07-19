-- Backfill has_county_surcharge_tax from raw_payload (safe to re-run).
UPDATE walmart_order_headers
SET has_county_surcharge_tax = true
WHERE has_county_surcharge_tax = false
  AND raw_payload::text ILIKE '%County Surcharge Tax%';
