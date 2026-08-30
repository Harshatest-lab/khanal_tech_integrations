-- Unicommerce B2C order sync + SAP posting (run once per environment).
-- Parent/child model mirrors amazon_us_orders.sql: headers + lines + logs.
--
-- Schema covers the full pipeline (fetch, post invoices, post credit
-- notes, PO/GRN creation, inventory sync, reconciliation) even though only
-- the fetch worker is implemented so far -- adding columns later is more
-- disruptive than sizing the table right once, per the Frappe source this
-- was ported from (khanal_tech_integrations/unicommerce/).
--
-- Stage machine on unicommerce_orders (matches the Frappe pipeline_status
-- values exactly, so behavior during the dual-run cutover is comparable):
--   fetched -> ready -> posting -> posted
--                 \-> batch_hold -> ready (auto-recovery) or dead (cap hit)
--                 \-> verify -> posted (recovered) or ready/dead (retry/cap)
--                 \-> failed -> ready (retry) or dead (cap hit)

CREATE TABLE IF NOT EXISTS unicommerce_orders (
  uniware_id                    text        PRIMARY KEY,   -- UC order code (saleOrderCode)
  channel_name                  text        NOT NULL DEFAULT '',
  pipeline_status                text        NOT NULL DEFAULT 'fetched',
  order_date                     date,
  order_json                     jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- full UC saleOrderDTO, as fetched
  channel_id                     text        NOT NULL DEFAULT '',   -- displayOrderCode / channelOrderId
  status                         text        NOT NULL DEFAULT '',   -- UC order status
  created                        timestamptz,
  updated                        timestamptz,
  displayorderdatetime           timestamptz,
  cod                            text        NOT NULL DEFAULT '',
  customer_name                  text        NOT NULL DEFAULT '',
  city                           text        NOT NULL DEFAULT '',
  state                          text        NOT NULL DEFAULT '',
  district                       text        NOT NULL DEFAULT '',
  pin_code                       integer,
  shippingpackages                jsonb,
  shipment_status                 text        NOT NULL DEFAULT '',
  shipment_date                   timestamptz,
  return_code                     text        NOT NULL DEFAULT '',
  statuscode                      text        NOT NULL DEFAULT '',
  shippingprovider                 text        NOT NULL DEFAULT '',
  trackingnumber                   text        NOT NULL DEFAULT '',
  trackingstatus                   text        NOT NULL DEFAULT '',
  providerstatus                   text        NOT NULL DEFAULT '',
  putawaycode                      text        NOT NULL DEFAULT '',
  returninvoicecode                 text        NOT NULL DEFAULT '',
  returninvoicedisplaycode           text        NOT NULL DEFAULT '',
  actioncode                       text        NOT NULL DEFAULT '',
  type                            text        NOT NULL DEFAULT '',
  returncompleteddate               text        NOT NULL DEFAULT '',
  inventoryreceiveddate             text        NOT NULL DEFAULT '',
  returnfacilitycode                text        NOT NULL DEFAULT '',
  returnitems                      jsonb,
  uc_taxable_value                 numeric(14,2) NOT NULL DEFAULT 0,
  uc_cgst                          numeric(14,2) NOT NULL DEFAULT 0,
  uc_sgst                          numeric(14,2) NOT NULL DEFAULT 0,
  uc_igst                          numeric(14,2) NOT NULL DEFAULT 0,
  uc_shipping                      numeric(14,2) NOT NULL DEFAULT 0,
  uc_total                         numeric(14,2) NOT NULL DEFAULT 0,
  sap_ar_invoice_docentry           text,
  sap_ar_invoice_docnum             text,
  ar_invoice_tax_expected           numeric(14,2),
  ar_invoice_tax_actual             numeric(14,2),
  ar_invoice_tax_diff               numeric(14,2),
  ar_invoice_unit_price_expected     numeric(14,2),
  ar_invoice_unit_price_actual       numeric(14,2),
  ar_invoice_unit_price_diff         numeric(14,2),
  ar_invoice_reconciled             integer     NOT NULL DEFAULT 0,
  ar_invoice_reconciliation_status   text        NOT NULL DEFAULT '',
  ar_invoice_mismatch_reason        text        NOT NULL DEFAULT '',   -- line-level diagnosis, set by Recon when a mismatch is found
  sap_ar_creditnote_docentry        text,        -- last-posted CN; an order can have several returns/CNs over time
  ar_creditnote_tax_expected        numeric(14,2),
  ar_creditnote_tax_actual          numeric(14,2),
  ar_creditnote_tax_diff            numeric(14,2),
  ar_creditnote_unit_price_expected numeric(14,2),
  ar_creditnote_unit_price_actual   numeric(14,2),
  ar_creditnote_unit_price_diff     numeric(14,2),
  ar_creditnote_reconciled          integer     NOT NULL DEFAULT 0,
  ar_creditnote_reconciliation_status text      NOT NULL DEFAULT '',
  payload_json                     jsonb,        -- last SAP AR Invoice payload actually posted
  posting_started_at                timestamptz,
  batch_hold_since                 timestamptz,
  attempt_count                    integer     NOT NULL DEFAULT 0,
  last_error                       text,
  created_at                       timestamptz NOT NULL DEFAULT now(),
  updated_at                       timestamptz NOT NULL DEFAULT now(),
  -- Last time this row was actually re-verified against live Unicommerce
  -- (get_order_detail), NOT the last time it was merely examined against
  -- its own cached order_json. Deliberately separate from updated_at --
  -- see store.defer_fetched's docstring (2026-08-28 fix) for why
  -- conflating the two let a 'fetched' row's cached status go stale
  -- forever despite being re-checked every cron cycle.
  last_checked_live_at             timestamptz
);

CREATE INDEX IF NOT EXISTS idx_unicommerce_orders_pipeline_status
  ON unicommerce_orders (pipeline_status);
CREATE INDEX IF NOT EXISTS idx_unicommerce_orders_order_date
  ON unicommerce_orders (order_date);
CREATE INDEX IF NOT EXISTS idx_unicommerce_orders_channel
  ON unicommerce_orders (channel_name);
CREATE INDEX IF NOT EXISTS idx_unicommerce_orders_updated_at
  ON unicommerce_orders (updated_at DESC);

-- One row per UC saleOrderItem line.
CREATE TABLE IF NOT EXISTS unicommerce_order_lines (
  id                          bigserial   PRIMARY KEY,
  uniware_id                  text        NOT NULL
    REFERENCES unicommerce_orders (uniware_id) ON DELETE CASCADE,
  item_id                     text        NOT NULL DEFAULT '',   -- UC item.id
  itemsku                     text        NOT NULL DEFAULT '',
  sellerskucode               text        NOT NULL DEFAULT '',
  channelproductid            text        NOT NULL DEFAULT '',
  quantity                    text        NOT NULL DEFAULT '1',
  total_price                 text        NOT NULL DEFAULT '',
  selling_price                text        NOT NULL DEFAULT '',
  created                     timestamptz,
  updated                     timestamptz,
  totalintegratedgst           text        NOT NULL DEFAULT '',
  integratedgstpercentage      text        NOT NULL DEFAULT '',
  sellingpricewithouttaxesanddiscount text NOT NULL DEFAULT '',
  shippingcharges              text        NOT NULL DEFAULT '',
  shippingmethodcharges        text        NOT NULL DEFAULT '',
  cashondeliverycharges        text        NOT NULL DEFAULT '',
  cancellationreason           text        NOT NULL DEFAULT '',
  state_gst_tax_percentage      text        NOT NULL DEFAULT '',
  central_gst_tax_percentage    text        NOT NULL DEFAULT '',
  statuscode                   text        NOT NULL DEFAULT '',
  discount                     text        NOT NULL DEFAULT '',
  shippingchargetaxpercentage   text        NOT NULL DEFAULT '',
  bundleskucode                text        NOT NULL DEFAULT '',
  code                        text        NOT NULL DEFAULT '',   -- saleOrderItemCode
  batchcode                    text        NOT NULL DEFAULT '',
  vendorbatchnumber             text        NOT NULL DEFAULT '',
  sap_invoice_no                text        NOT NULL DEFAULT '',
  sap_filled_status             text        NOT NULL DEFAULT '',
  delivery_linenum              text        NOT NULL DEFAULT '',
  uniware_return_status         text        NOT NULL DEFAULT '',
  uniware_return_items          jsonb,
  created_at                   timestamptz NOT NULL DEFAULT now(),
  updated_at                   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (uniware_id, code)
);

CREATE INDEX IF NOT EXISTS idx_unicommerce_order_lines_order
  ON unicommerce_order_lines (uniware_id);
CREATE INDEX IF NOT EXISTS idx_unicommerce_order_lines_itemsku
  ON unicommerce_order_lines (itemsku);

-- One row per return (CIR/RTO), staged for credit-note posting.
CREATE TABLE IF NOT EXISTS unicommerce_cn_staging (
  uc_return_id            text        PRIMARY KEY,
  uc_order_id              text        NOT NULL,   -- FK-like ref to unicommerce_orders.uniware_id (not enforced: return can arrive before its parent order)
  parent_sap_doc_entry      text        NOT NULL DEFAULT '',
  return_date              date,
  return_type              text        NOT NULL DEFAULT '',   -- courier | customer
  items_json               jsonb       NOT NULL DEFAULT '[]'::jsonb,
  status                   text        NOT NULL DEFAULT 'pending',   -- pending | posted | failed | dead
  sap_cn_docentry           text,
  sap_cn_docnum             text,
  attempt_count             integer     NOT NULL DEFAULT 0,
  last_error                text,
  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_unicommerce_cn_staging_status
  ON unicommerce_cn_staging (status);
CREATE INDEX IF NOT EXISTS idx_unicommerce_cn_staging_order
  ON unicommerce_cn_staging (uc_order_id);

-- SAP inbound Stock Transfer -> UC Purchase Order + GRN. One row per SAP
-- StockTransfers DocEntry (polled via SAP Service Layer, not a Frappe
-- doctype -- see docs/superpowers/specs/2026-07-27-pogrn-inventorysync-design.md).
-- State persisted after each UC write (po_created before GRN creation even
-- starts) so a crash mid-run never re-issues a UC write that already
-- succeeded -- there is no UC sandbox to safely absorb a duplicate PO.
--
-- There is no outward equivalent of this table. An earlier build had one
-- (unicommerce_inventory_sync_staging, watching EC-FG->EC-B2C / AMZ-*
-- outward transfers and calling UC's inventory/adjust/bulk REMOVE) --
-- removed 2026-07-27: EC-FG/EC-B2C/AMZ-* are same-company internal
-- warehouses, UC already decrements a batch itself when an order ships,
-- and PostInvoices (posting the SAP AR Invoice) is what corresponds to
-- that same event. A manual outward REMOVE would have double-decremented
-- real UC inventory for stock UC had already correctly reduced on its
-- own. See docs/superpowers/specs/2026-07-27-pogrn-inventorysync-design.md.
CREATE TABLE IF NOT EXISTS unicommerce_pogrn_staging (
  sap_doc_entry      bigint      PRIMARY KEY,   -- SAP StockTransfers DocEntry
  from_warehouse      text        NOT NULL DEFAULT '',
  to_warehouse        text        NOT NULL DEFAULT '',
  line_items          jsonb       NOT NULL DEFAULT '[]'::jsonb,   -- snapshot: item/batch/qty at claim time
  status              text        NOT NULL DEFAULT 'pending',   -- pending | po_creating | po_created | grn_creating | grn_done | failed | dead
  uc_po_id            text        NOT NULL DEFAULT '',
  uc_grn_ids          jsonb       NOT NULL DEFAULT '[]'::jsonb,   -- appended to as each GRN round succeeds
  attempt_count       integer     NOT NULL DEFAULT 0,
  last_error          text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_unicommerce_pogrn_staging_status
  ON unicommerce_pogrn_staging (status);

-- Sync run history + watermarks (mirrors amazon_us_sync_log / amazon_us_watermarks).
CREATE TABLE IF NOT EXISTS unicommerce_sync_log (
  id               bigserial   PRIMARY KEY,
  mode             text        NOT NULL,   -- fetch_orders | fetch_returns | post_invoices | post_credit_notes | retry_invoices | retry_cn | po_grn | recon
  started_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz,
  status           text        NOT NULL DEFAULT 'running',   -- running | success | error | aborted
  window_from      text,
  window_to        text,
  counts           jsonb       NOT NULL DEFAULT '{}'::jsonb,
  error_message    text
);

CREATE INDEX IF NOT EXISTS idx_unicommerce_sync_log_mode
  ON unicommerce_sync_log (mode, started_at DESC);

CREATE TABLE IF NOT EXISTS unicommerce_watermarks (
  name       text        PRIMARY KEY,
  value      text        NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
