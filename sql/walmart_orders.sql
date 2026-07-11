-- Walmart B2C order sync (run once per environment).
-- Parent/child model: walmart_order_headers + walmart_order_lines + walmart_log.

-- One row per purchase order (PO). Full Walmart JSON stored once.
CREATE TABLE IF NOT EXISTS walmart_order_headers (
  purchase_order_id text        PRIMARY KEY,
  customer_order_id text,
  ship_node_type    text,
  order_date_ms     bigint,
  has_shipping_charges boolean   NOT NULL DEFAULT false,
  raw_payload       jsonb       NOT NULL DEFAULT '{}'::jsonb,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_walmart_order_headers_order_date
  ON walmart_order_headers (order_date_ms DESC);

CREATE INDEX IF NOT EXISTS idx_walmart_order_headers_has_shipping
  ON walmart_order_headers (has_shipping_charges);

CREATE INDEX IF NOT EXISTS idx_walmart_order_headers_updated_at
  ON walmart_order_headers (updated_at DESC);

-- One row per line item. Links to header via purchase_order_id.
CREATE TABLE IF NOT EXISTS walmart_order_lines (
  id                bigserial   PRIMARY KEY,
  purchase_order_id text        NOT NULL
    REFERENCES walmart_order_headers (purchase_order_id) ON DELETE CASCADE,
  line_number       text        NOT NULL,
  status            text        NOT NULL,
  status_date_ms    bigint      NOT NULL,
  status_date       timestamptz NOT NULL,
  sku               text,
  product_name      text,
  quantity_amount   text,
  tracking_number   text,
  tracking_url      text,
  carrier_name      text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (purchase_order_id, line_number)
);

CREATE INDEX IF NOT EXISTS idx_walmart_order_lines_purchase_order_id
  ON walmart_order_lines (purchase_order_id);

CREATE INDEX IF NOT EXISTS idx_walmart_order_lines_status_date
  ON walmart_order_lines (status_date_ms DESC);

CREATE INDEX IF NOT EXISTS idx_walmart_order_lines_status
  ON walmart_order_lines (status);

CREATE TABLE IF NOT EXISTS walmart_log (
  id                       bigserial PRIMARY KEY,
  sync_started_at          timestamptz NOT NULL DEFAULT now(),
  sync_finished_at         timestamptz,
  status                   text        NOT NULL DEFAULT 'running',
  last_modified_start_date timestamptz NOT NULL,
  last_modified_end_date   timestamptz NOT NULL,
  max_status_date_ms       bigint,
  next_last_modified_start timestamptz,
  ship_node_type           text        NOT NULL DEFAULT 'WFSFulfilled',
  pages_fetched            integer     NOT NULL DEFAULT 0,
  orders_seen              integer     NOT NULL DEFAULT 0,
  lines_stored             integer     NOT NULL DEFAULT 0,
  error_message            text,
  meta                     jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_walmart_log_finished_status
  ON walmart_log (sync_finished_at DESC)
  WHERE status = 'success';

-- ---------------------------------------------------------------------------
-- One-time migration from legacy walmart_orders (single flat table).
-- Run only if walmart_orders exists and new tables are empty.
-- After verifying data, drop the legacy table:
--   DROP TABLE IF EXISTS walmart_orders;
-- ---------------------------------------------------------------------------
-- INSERT INTO walmart_order_headers (
--   purchase_order_id, customer_order_id, ship_node_type,
--   order_date_ms, raw_payload, created_at, updated_at
-- )
-- SELECT DISTINCT ON (purchase_order_id)
--   purchase_order_id, customer_order_id, ship_node_type,
--   order_date_ms, raw_payload, created_at, updated_at
-- FROM walmart_orders
-- ORDER BY purchase_order_id, updated_at DESC
-- ON CONFLICT (purchase_order_id) DO NOTHING;
--
-- INSERT INTO walmart_order_lines (
--   purchase_order_id, line_number, status, status_date_ms, status_date,
--   sku, product_name, quantity_amount, tracking_number, tracking_url,
--   carrier_name, created_at, updated_at
-- )
-- SELECT
--   purchase_order_id, line_number, status, status_date_ms, status_date,
--   sku, product_name, quantity_amount, tracking_number, tracking_url,
--   carrier_name, created_at, updated_at
-- FROM walmart_orders
-- ON CONFLICT (purchase_order_id, line_number) DO NOTHING;
