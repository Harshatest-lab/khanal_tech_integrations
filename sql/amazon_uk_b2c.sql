-- Amazon UK B2C order sync + SAP posting (run once per environment):
--   psql "$DB_URL" -f sql/amazon_uk_b2c.sql
-- Schema `amazonuk`; used by services/amazon-uk-b2c (store.py, post_to_sap.py,
-- applog.py, dashboard.py). Idempotent (IF NOT EXISTS throughout).
-- Copied from the standalone pipeline (~/amazon-uk/schema_amazonuk.sql).

-- amazonuk schema: single order_items table merging 2 SP-API endpoint
-- families (Orders API's bulk /orders/2026-01-01/orders, which already
-- embeds orderItems so no separate GetOrderItems call is needed; and the
-- Reports API's GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL report)
-- Grain: one row per order item. Shipment columns are NULL until the report
-- arrives and upserts them in via (amazon_order_id, order_item_id).
--
-- Report data: only shipment_date/carrier/tracking_number are pulled into
-- real columns (shipment_date is what triggers the SAP invoice post). Every
-- other report field stays in raw_report_row only, not individually columned.

CREATE SCHEMA IF NOT EXISTS amazonuk;

CREATE TABLE IF NOT EXISTS amazonuk.order_items (
    -- identity
    amazon_order_id            TEXT NOT NULL,
    order_item_id               TEXT NOT NULL,

    -- order-level (Orders API; duplicated per item row of the same order)
    order_status                TEXT,
    purchase_date                TIMESTAMPTZ,
    last_update_date             TIMESTAMPTZ,
    order_total_amount           NUMERIC(12, 2),
    order_total_currency         TEXT,
    number_of_items_shipped      INTEGER,
    number_of_items_unshipped    INTEGER,
    is_prime                     BOOLEAN,
    is_business_order            BOOLEAN,
    payment_method                TEXT,
    marketplace_id                TEXT,
    fulfillment_channel            TEXT,
    sales_channel                  TEXT,
    ship_service_level              TEXT,

    -- item-level (Orders API — orderItems embedded in the same bulk call)
    asin                          TEXT,
    seller_sku                    TEXT,
    title                         TEXT,
    quantity_ordered              INTEGER,
    item_price_amount             NUMERIC(12, 2),
    item_tax_amount               NUMERIC(12, 2),
    shipping_price_amount         NUMERIC(12, 2),
    shipping_tax_amount           NUMERIC(12, 2),
    promotion_discount_amount     NUMERIC(12, 2),
    condition_id                  TEXT,
    is_gift                       BOOLEAN,

    -- shipment-level (Reports API) — the only fields pulled out of the report;
    -- everything else it carries stays in raw_report_row below
    shipment_date                 TIMESTAMPTZ,
    carrier                       TEXT,
    tracking_number               TEXT,
    ship_city                     TEXT,
    ship_postal_code              TEXT,
    ship_country                  TEXT,

    -- raw payloads for audit / re-derive without re-calling the API
    raw_order                     JSONB,
    raw_order_item                JSONB,
    raw_report_row                JSONB,

    fetched_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (amazon_order_id, order_item_id)
);

CREATE INDEX IF NOT EXISTS idx_amazonuk_order_items_purchase_date ON amazonuk.order_items (purchase_date);
CREATE INDEX IF NOT EXISTS idx_amazonuk_order_items_order_status  ON amazonuk.order_items (order_status);
CREATE INDEX IF NOT EXISTS idx_amazonuk_order_items_sku           ON amazonuk.order_items (seller_sku);
CREATE INDEX IF NOT EXISTS idx_amazonuk_order_items_shipment_date ON amazonuk.order_items (shipment_date);
CREATE INDEX IF NOT EXISTS idx_amazonuk_order_items_tracking      ON amazonuk.order_items (tracking_number);

-- Amazon seller_sku -> SAP ItemCode. Acts as a cache in front of SAP's own
-- item master (OITM."U_Amz_SKU" UDF, confirmed live 2026-09-17) — a miss
-- here triggers a live HANA lookup (invoice_payload.build_invoice_payload)
-- which writes the result back here on a hit. Only a SKU missing from BOTH
-- hard-stops posting, rather than silently dropping the line (same rule as
-- the Amazon US pipeline, after a real incident there where a silent drop
-- went unnoticed).
CREATE TABLE IF NOT EXISTS amazonuk.sku_item_map (
    seller_sku        TEXT PRIMARY KEY,
    sap_item_code     TEXT NOT NULL,
    item_description  TEXT,
    is_active         BOOLEAN NOT NULL DEFAULT true
);

-- One row per Amazon order posted (or attempted) to SAP as an AR Invoice.
-- Primary duplicate guard: post_to_sap.py checks this AND queries SAP
-- directly (NumAtCard) before every post, so a retry after a crash between
-- "SAP accepted it" and "we recorded it" never double-posts.
CREATE TABLE IF NOT EXISTS amazonuk.sap_postings (
    amazon_order_id   TEXT PRIMARY KEY,
    sap_doc_entry     TEXT,
    sap_doc_num       TEXT,
    status            TEXT NOT NULL,  -- 'posted' or 'failed'
    error             TEXT,
    -- Full SAP Service Layer response body: the created Invoice object on
    -- success, or the {"error": {...}} body on failure. `error` above stays
    -- a short human-readable summary for lists/tooltips; this is the raw
    -- response for full inspection (dashboard "View SAP Response" modal).
    sap_response      JSONB,
    posted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- failed attempts so far; dead = gave up after 5 (automated runs skip it,
    -- the dashboard's manual Post still tries). See amazon_uk_posting_attempts.sql.
    attempts          INTEGER NOT NULL DEFAULT 0,
    dead              BOOLEAN NOT NULL DEFAULT false
);

-- One row per incremental fetch run. Next run's lastUpdatedAfter cursor is
-- MAX(watermark) for the source — so a re-fetched order whose status just
-- changed (e.g. Pending -> Shipped) is picked up even if it was purchased
-- days earlier, not just orders created "today". Same pattern as the
-- sibling Amazon US pipeline's sync_watermark (Amazon US Settings), kept
-- as an append-only log here instead of a single mutable field.
CREATE TABLE IF NOT EXISTS amazonuk.fetch_log (
    id                SERIAL PRIMARY KEY,
    source            TEXT NOT NULL,  -- 'orders'
    watermark         TIMESTAMPTZ NOT NULL,
    orders_fetched    INTEGER NOT NULL,
    items_stored      INTEGER NOT NULL,
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_amazonuk_fetch_log_source_watermark
    ON amazonuk.fetch_log (source, watermark DESC);

-- Pipeline run log (sync/fetch/post progress messages) — stored in RDS via
-- applog.py, not a local file: this stack is Python + RDS + HTML/CSS, no
-- Frappe framework to keep file-log parity with.
CREATE TABLE IF NOT EXISTS amazonuk.sync_log (
    id            BIGSERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    level         TEXT NOT NULL,  -- 'INFO', 'ERROR', etc.
    message       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_amazonuk_sync_log_created_at
    ON amazonuk.sync_log (created_at DESC);

-- Refunds + FBA inventory reimbursements, via the Finances API
-- (/finances/2024-06-19/transactions). Only 'Refund' transactionType,
-- 'RELEASED' transactionStatus (final settlement) are fetched — see
-- services/amazon-uk-b2c src/amazon_uk/fetch_refunds.py.
--
-- Grain: one row per TRANSACTION (refund_events) plus one row per
-- transaction LINE ITEM (refund_items, keyed by position in items[] —
-- see src/amazon_uk/store.py's flatten_refund_transaction for the
-- adjustment-id grouping that produces item_index).
--
-- Sign convention: Amazon's totalAmount is the net cash impact, negative
-- = money out of the seller's pocket (refund). Stored as net_refund.
-- refunded_amount flips the item's ProductCharges breakdown positive =
-- the buyer-facing "given back" figure.
CREATE TABLE IF NOT EXISTS amazonuk.refund_events (
    transaction_id            TEXT PRIMARY KEY,
    transaction_type          TEXT NOT NULL,   -- 'Refund'
    transaction_status        TEXT,
    amazon_order_id           TEXT,
    refund_id                 TEXT,            -- Amazon's own refund id
    settlement_id             TEXT,
    financial_event_group_id  TEXT,
    description                TEXT,
    posted_date                TIMESTAMPTZ NOT NULL,
    marketplace_name           TEXT,
    total_amount                NUMERIC(12, 2),
    currency                    TEXT,
    raw_transaction              JSONB,
    fetched_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_amazonuk_refund_events_posted_date ON amazonuk.refund_events (posted_date);
CREATE INDEX IF NOT EXISTS idx_amazonuk_refund_events_order       ON amazonuk.refund_events (amazon_order_id);
CREATE INDEX IF NOT EXISTS idx_amazonuk_refund_events_type        ON amazonuk.refund_events (transaction_type);

CREATE TABLE IF NOT EXISTS amazonuk.refund_items (
    transaction_id             TEXT NOT NULL,
    item_index                 INTEGER NOT NULL,
    order_adjustment_item_id   TEXT,

    amazon_order_id            TEXT,
    seller_sku                 TEXT,
    asin                       TEXT,
    quantity_refunded          INTEGER,
    product_description        TEXT,
    breakdowns                 JSONB,   -- raw item.breakdowns tree

    charges_total               NUMERIC(12, 2),   -- breakdown 'ProductCharges' (Amazon's sign: negative on a refund)
    fees_total                   NUMERIC(12, 2),   -- breakdown 'AmazonFees'/'DigitalServicesFee'
    tax_withheld_total            NUMERIC(12, 2),   -- breakdown 'Tax'
    promotions_total               NUMERIC(12, 2),   -- breakdown 'PromoRebates'
    other_total                     NUMERIC(12, 2),   -- any other/unrecognized breakdown type
    refunded_amount                  NUMERIC(12, 2),   -- money given back to buyer: -charges_total (positive)
    net_refund                       NUMERIC(12, 2),   -- item's own totalAmount, Amazon-computed
    currency                          TEXT,

    fetched_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (transaction_id, item_index)
);

CREATE INDEX IF NOT EXISTS idx_amazonuk_refund_items_sku   ON amazonuk.refund_items (seller_sku);
CREATE INDEX IF NOT EXISTS idx_amazonuk_refund_items_order ON amazonuk.refund_items (amazon_order_id);

-- SAP B1 AR Credit Memo postings — same tracking pattern as sap_postings,
-- but keyed on the refund's transaction_id (not amazon_order_id): one
-- order can get more than one separate RELEASED refund over time, and
-- each one becomes its own credit memo, not one combined per order.
CREATE TABLE IF NOT EXISTS amazonuk.sap_credit_notes (
    transaction_id    TEXT PRIMARY KEY,
    amazon_order_id   TEXT NOT NULL,
    sap_doc_entry     TEXT,
    sap_doc_num       TEXT,
    status            TEXT NOT NULL,  -- 'posted' or 'failed'
    error             TEXT,
    sap_response      JSONB,
    posted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts          INTEGER NOT NULL DEFAULT 0,
    dead              BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_amazonuk_sap_credit_notes_order ON amazonuk.sap_credit_notes (amazon_order_id);
