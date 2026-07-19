-- Ship-to US state for dashboard + SAP PayToCode / ShipToCode (run once on existing DB).
ALTER TABLE walmart_order_headers
  ADD COLUMN IF NOT EXISTS ship_to_state text;

CREATE INDEX IF NOT EXISTS idx_walmart_order_headers_ship_to_state
  ON walmart_order_headers (ship_to_state);

UPDATE walmart_order_headers
SET ship_to_state = UPPER(TRIM(raw_payload #>> '{order,shippingInfo,postalAddress,state}'))
WHERE ship_to_state IS NULL
  AND NULLIF(TRIM(raw_payload #>> '{order,shippingInfo,postalAddress,state}'), '') IS NOT NULL;
