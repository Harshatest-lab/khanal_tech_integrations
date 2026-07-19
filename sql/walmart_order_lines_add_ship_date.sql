-- Ship date from Walmart trackingInfo.shipDateTime (US Pacific calendar date).
ALTER TABLE walmart_order_lines
  ADD COLUMN IF NOT EXISTS ship_date_time_ms bigint,
  ADD COLUMN IF NOT EXISTS ship_date_pacific date;

CREATE INDEX IF NOT EXISTS idx_walmart_order_lines_ship_date_pacific
  ON walmart_order_lines (ship_date_pacific DESC);

CREATE INDEX IF NOT EXISTS idx_walmart_order_lines_ship_date_time_ms
  ON walmart_order_lines (ship_date_time_ms DESC)
  WHERE ship_date_time_ms IS NOT NULL;
