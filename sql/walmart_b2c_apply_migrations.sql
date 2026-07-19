-- Run once on khanalfoodsserverless (all pending Walmart B2C dashboard/sync columns).
-- Usage: psql "$DATABASE_URL" -f sql/walmart_b2c_apply_migrations.sql

-- Shipping filter flag
ALTER TABLE walmart_order_headers
  ADD COLUMN IF NOT EXISTS has_shipping_charges boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_walmart_order_headers_has_shipping
  ON walmart_order_headers (has_shipping_charges);

-- County Surcharge Tax filter flag
ALTER TABLE walmart_order_headers
  ADD COLUMN IF NOT EXISTS has_county_surcharge_tax boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_walmart_order_headers_county_surcharge
  ON walmart_order_headers (has_county_surcharge_tax);

UPDATE walmart_order_headers
SET has_county_surcharge_tax = true
WHERE has_county_surcharge_tax = false
  AND raw_payload::text ILIKE '%County Surcharge Tax%';

-- Promotion / discount filter flag
ALTER TABLE walmart_order_headers
  ADD COLUMN IF NOT EXISTS has_promotion_discount boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_walmart_order_headers_promotion_discount
  ON walmart_order_headers (has_promotion_discount);

UPDATE walmart_order_headers
SET has_promotion_discount = true
WHERE has_promotion_discount = false
  AND raw_payload::text ILIKE '%"chargeName": "Promotion"%';

-- Ship-to state (SAP PayToCode / ShipToCode)
ALTER TABLE walmart_order_headers
  ADD COLUMN IF NOT EXISTS ship_to_state text;

CREATE INDEX IF NOT EXISTS idx_walmart_order_headers_ship_to_state
  ON walmart_order_headers (ship_to_state);

UPDATE walmart_order_headers
SET ship_to_state = UPPER(TRIM(raw_payload #>> '{order,shippingInfo,postalAddress,state}'))
WHERE ship_to_state IS NULL
  AND NULLIF(TRIM(raw_payload #>> '{order,shippingInfo,postalAddress,state}'), '') IS NOT NULL;

-- Ship date (Pacific) on order lines
ALTER TABLE walmart_order_lines
  ADD COLUMN IF NOT EXISTS ship_date_time_ms bigint,
  ADD COLUMN IF NOT EXISTS ship_date_pacific date;

CREATE INDEX IF NOT EXISTS idx_walmart_order_lines_ship_date_pacific
  ON walmart_order_lines (ship_date_pacific DESC);

CREATE INDEX IF NOT EXISTS idx_walmart_order_lines_ship_date_time_ms
  ON walmart_order_lines (ship_date_time_ms DESC)
  WHERE ship_date_time_ms IS NOT NULL;

-- Backfill ship dates from raw_payload
UPDATE walmart_order_lines ol
SET
  ship_date_time_ms = src.ship_ms,
  ship_date_pacific = (
    timezone('America/Los_Angeles', to_timestamp(src.ship_ms / 1000.0))
  )::date
FROM (
  SELECT
    ol2.id,
    (
      SELECT MAX((status_elem->'trackingInfo'->>'shipDateTime')::bigint)
      FROM walmart_order_headers h
      CROSS JOIN LATERAL (
        SELECT jsonb_array_elements(
          CASE
            WHEN jsonb_typeof(h.raw_payload #> '{order,orderLines,orderLine}') = 'array'
              THEN h.raw_payload #> '{order,orderLines,orderLine}'
            WHEN h.raw_payload #> '{order,orderLines,orderLine}' IS NOT NULL
              THEN jsonb_build_array(h.raw_payload #> '{order,orderLines,orderLine}')
            ELSE '[]'::jsonb
          END
        ) AS line_elem
      ) lines
      CROSS JOIN LATERAL (
        SELECT jsonb_array_elements(
          CASE
            WHEN jsonb_typeof(line_elem #> '{orderLineStatuses,orderLineStatus}') = 'array'
              THEN line_elem #> '{orderLineStatuses,orderLineStatus}'
            WHEN line_elem #> '{orderLineStatuses,orderLineStatus}' IS NOT NULL
              THEN jsonb_build_array(line_elem #> '{orderLineStatuses,orderLineStatus}')
            ELSE '[]'::jsonb
          END
        ) AS status_elem
      ) statuses
      WHERE h.purchase_order_id = ol2.purchase_order_id
        AND (line_elem->>'lineNumber') = ol2.line_number
        AND status_elem->'trackingInfo'->>'shipDateTime' IS NOT NULL
    ) AS ship_ms
  FROM walmart_order_lines ol2
  WHERE ol2.ship_date_time_ms IS NULL
) src
WHERE ol.id = src.id
  AND src.ship_ms IS NOT NULL;
