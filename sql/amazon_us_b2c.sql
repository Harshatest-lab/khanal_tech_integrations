-- Amazon US B2C order sync + SAP posting (run once per environment).
-- Parent/child model mirrors walmart_orders.sql: headers + lines + logs.

-- One row per Amazon order. stage drives the posting pipeline:
--   fetched -> ready -> posting -> posted   (failed on error)
-- Refunds are tracked per refund event in amazon_us_cn_log, NOT as an
-- order stage: one order can have several partial refunds.
CREATE TABLE IF NOT EXISTS amazon_us_orders (
  amazon_order_id     text        PRIMARY KEY,
  order_status        text        NOT NULL DEFAULT '',
  stage               text        NOT NULL DEFAULT 'fetched',
  purchase_date       timestamp,             -- naive US/Pacific wall time (MTR-aligned)
  purchase_date_utc   timestamptz,           -- original SP-API value, audit
  actual_ship_date    date,                  -- AFN: FBA shipments report. MFN: Amazon has no
                                              -- true ship-date field, so this is last-updated-date
                                              -- at the Shipped transition (our own shipment
                                              -- confirmation, timestamped by Amazon). SAP DocDate.
  fulfillment_channel text        NOT NULL DEFAULT '',  -- AFN | MFN, from the Orders report/API
  order_total_amount  numeric(12,2) NOT NULL DEFAULT 0,
  ship_city           text        NOT NULL DEFAULT '',
  ship_state          text        NOT NULL DEFAULT '',   -- raw Amazon value; normalized at payload time
  ship_postal_code    text        NOT NULL DEFAULT '',
  ship_country        text        NOT NULL DEFAULT '',
  buyer_name          text        NOT NULL DEFAULT '',
  sap_doc_entry       integer,               -- posted AR Invoice
  sap_doc_num         integer,
  posted_at           timestamptz,
  last_error          text,
  attempt_count       integer     NOT NULL DEFAULT 0,
  raw_payload         jsonb       NOT NULL DEFAULT '{}'::jsonb,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Idempotent for the already-migrated shared RDS (added after the initial
-- rollout, once MFN orders turned out to need their own ship-date source).
ALTER TABLE amazon_us_orders ADD COLUMN IF NOT EXISTS fulfillment_channel text NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_amazon_us_orders_stage
  ON amazon_us_orders (stage);
CREATE INDEX IF NOT EXISTS idx_amazon_us_orders_ship_date
  ON amazon_us_orders (actual_ship_date);
CREATE INDEX IF NOT EXISTS idx_amazon_us_orders_purchase_date
  ON amazon_us_orders (purchase_date DESC);
CREATE INDEX IF NOT EXISTS idx_amazon_us_orders_updated_at
  ON amazon_us_orders (updated_at DESC);

-- One row per order line. order_item_id is Amazon's line id (report and
-- Orders API both carry it); fall back to seller_sku when absent.
CREATE TABLE IF NOT EXISTS amazon_us_order_lines (
  id                    bigserial   PRIMARY KEY,
  amazon_order_id       text        NOT NULL
    REFERENCES amazon_us_orders (amazon_order_id) ON DELETE CASCADE,
  order_item_id         text        NOT NULL DEFAULT '',
  seller_sku            text        NOT NULL,
  asin                  text        NOT NULL DEFAULT '',
  title                 text        NOT NULL DEFAULT '',
  quantity              numeric(10,3) NOT NULL,
  unit_price            numeric(12,5) NOT NULL DEFAULT 0,  -- GROSS: item_price / qty
  item_price_amount     numeric(12,2) NOT NULL DEFAULT 0,
  item_tax_amount       numeric(12,2) NOT NULL DEFAULT 0,
  shipping_price_amount numeric(12,2) NOT NULL DEFAULT 0,
  shipping_tax_amount   numeric(12,2) NOT NULL DEFAULT 0,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (amazon_order_id, order_item_id, seller_sku)
);

CREATE INDEX IF NOT EXISTS idx_amazon_us_order_lines_order
  ON amazon_us_order_lines (amazon_order_id);
CREATE INDEX IF NOT EXISTS idx_amazon_us_order_lines_sku
  ON amazon_us_order_lines (seller_sku);

-- Audit log of SAP AR Invoice attempts (payload + response per order).
-- amazon_us_orders.stage/sap_doc_entry stay authoritative for pipeline
-- state; this table answers "what exactly did we send and get back".
CREATE TABLE IF NOT EXISTS amazon_us_invoice_log (
  id              bigserial   PRIMARY KEY,
  amazon_order_id text        NOT NULL,
  status          text        NOT NULL,   -- posted | recovered | failed | skipped
  sap_doc_entry   integer,
  sap_doc_num     integer,
  sap_payload     jsonb,
  sap_response    jsonb,
  error_message   text,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_amazon_us_invoice_log_order
  ON amazon_us_invoice_log (amazon_order_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_amazon_us_invoice_log_status
  ON amazon_us_invoice_log (status);

-- One row per refund event (refund_id = "{order_id}-{PostedDate}" = SAP
-- NumAtCard). Authoritative local record of posted credit notes.
CREATE TABLE IF NOT EXISTS amazon_us_cn_log (
  refund_id       text        PRIMARY KEY,
  amazon_order_id text        NOT NULL,
  posted_date     date,
  status          text        NOT NULL,   -- posted | skipped | failed | failed_permanent
  skip_reason     text,
  sap_doc_entry   integer,
  sap_doc_num     integer,
  base_doc_entry  integer,                -- invoice the CN reverses
  sap_payload     jsonb,
  sap_response    jsonb,
  error_message   text,
  attempt_count   integer     NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_amazon_us_cn_log_order
  ON amazon_us_cn_log (amazon_order_id);
CREATE INDEX IF NOT EXISTS idx_amazon_us_cn_log_status
  ON amazon_us_cn_log (status);

-- seller_sku -> SAP ItemCode. source='hana' rows are replaced wholesale by
-- the hourly refresh from OITM.U_Amz_SKU (control chars stripped, SKUs
-- mapped to >1 item excluded); source='override' rows are manual and
-- always win. Resolution: override first, then hana.
CREATE TABLE IF NOT EXISTS amazon_us_sku_map (
  seller_sku    text        NOT NULL,
  sap_item_code text        NOT NULL,
  source        text        NOT NULL DEFAULT 'hana',   -- hana | override
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (seller_sku, source)
);

-- Sync run history + watermarks (mirrors walmart_log).
CREATE TABLE IF NOT EXISTS amazon_us_sync_log (
  id               bigserial   PRIMARY KEY,
  mode             text        NOT NULL,   -- incremental | report | backfill | fba | post | cn | recon | sku_refresh
  started_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz,
  status           text        NOT NULL DEFAULT 'running',   -- running | success | error
  window_from      text,
  window_to        text,
  counts           jsonb       NOT NULL DEFAULT '{}'::jsonb,
  error_message    text
);

CREATE INDEX IF NOT EXISTS idx_amazon_us_sync_log_mode
  ON amazon_us_sync_log (mode, started_at DESC);

CREATE TABLE IF NOT EXISTS amazon_us_watermarks (
  name       text        PRIMARY KEY,
  value      text        NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
