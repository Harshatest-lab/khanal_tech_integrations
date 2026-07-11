-- One-time migration: legacy walmart_orders -> walmart_order_headers + walmart_order_lines
-- Run AFTER creating the new tables from walmart_orders.sql
-- Verify counts, then: DROP TABLE IF EXISTS walmart_orders;

INSERT INTO walmart_order_headers (
  purchase_order_id, customer_order_id, ship_node_type,
  order_date_ms, raw_payload, created_at, updated_at
)
SELECT DISTINCT ON (purchase_order_id)
  purchase_order_id, customer_order_id, ship_node_type,
  order_date_ms, raw_payload, created_at, updated_at
FROM walmart_orders
ORDER BY purchase_order_id, updated_at DESC
ON CONFLICT (purchase_order_id) DO NOTHING;

INSERT INTO walmart_order_lines (
  purchase_order_id, line_number, status, status_date_ms, status_date,
  sku, product_name, quantity_amount, tracking_number, tracking_url,
  carrier_name, created_at, updated_at
)
SELECT
  purchase_order_id, line_number, status, status_date_ms, status_date,
  sku, product_name, quantity_amount, tracking_number, tracking_url,
  carrier_name, created_at, updated_at
FROM walmart_orders
ON CONFLICT (purchase_order_id, line_number) DO NOTHING;

-- Sanity checks:
-- SELECT COUNT(DISTINCT purchase_order_id) FROM walmart_orders;
-- SELECT COUNT(*) FROM walmart_order_headers;
-- SELECT COUNT(*) FROM walmart_orders;
-- SELECT COUNT(*) FROM walmart_order_lines;
