-- Add shipping flag for dashboard filters (run once on existing DB).
ALTER TABLE walmart_order_headers
  ADD COLUMN IF NOT EXISTS has_shipping_charges boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_walmart_order_headers_has_shipping
  ON walmart_order_headers (has_shipping_charges);

CREATE INDEX IF NOT EXISTS idx_walmart_order_headers_order_date_ms
  ON walmart_order_headers (order_date_ms DESC);
