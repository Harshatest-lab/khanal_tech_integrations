-- Promotion / discount flag for dashboard filters (run once on existing DB).
ALTER TABLE walmart_order_headers
  ADD COLUMN IF NOT EXISTS has_promotion_discount boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_walmart_order_headers_promotion_discount
  ON walmart_order_headers (has_promotion_discount);

-- Backfill from stored Walmart JSON (re-sync will refine via order_pricing).
UPDATE walmart_order_headers
SET has_promotion_discount = true
WHERE has_promotion_discount = false
  AND raw_payload::text ILIKE '%"chargeName": "Promotion"%';
