-- Backfill ship_date_* from raw_payload for lines synced before ship-date code deployed.
-- Safe to run multiple times (only updates rows where ship_date_time_ms IS NULL).

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
