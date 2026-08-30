-- B2B sales-order intake (Blinkit first; other channels reuse the same tables).
-- Schema b2b — same database as Walmart / sap_fg_items (those stay in public).
--
-- New empty database: run this file.
-- Existing khanaltestdb (tables already in public): run sql/b2b_so_move_to_schema.sql instead.
--
-- pipeline_status:
--   parsed       — attachments read, header/lines stored
--   needs_review — address, GST, or SKU map failed
--   ready        — matched and complete; waiting for a reviewer
--   approved     — reviewer signed off; waiting for SAP post
--   posted       — SAP Orders DocEntry stored
--   failed       — last attempt errored (see last_error)

CREATE SCHEMA IF NOT EXISTS b2b;

CREATE TABLE IF NOT EXISTS b2b.purchase_orders (
  id                      bigserial PRIMARY KEY,
  channel                 text        NOT NULL DEFAULT 'blinkit',
  po_number               text        NOT NULL,
  pipeline_status         text        NOT NULL DEFAULT 'parsed',
  company_name            text        NOT NULL DEFAULT '',
  contact_name            text        NOT NULL DEFAULT '',
  po_expiry_date          date,
  delivered_to            text        NOT NULL DEFAULT '',
  gst_no                  text        NOT NULL DEFAULT '',
  customer_code           text        NOT NULL DEFAULT '',
  salesperson_code       text        NOT NULL DEFAULT '',
  matched_address_name    text        NOT NULL DEFAULT '',
  matched_state           text        NOT NULL DEFAULT '',
  billing_from            text        NOT NULL DEFAULT 'KT',
  bill_to                 text        NOT NULL DEFAULT '',
  email_uid               text        NOT NULL DEFAULT '',
  email_subject            text        NOT NULL DEFAULT '',
  pdf_s3_key              text        NOT NULL DEFAULT '',
  xlsx_s3_key             text        NOT NULL DEFAULT '',
  total_quantity          numeric     NOT NULL DEFAULT 0,
  total_amount            numeric     NOT NULL DEFAULT 0,
  total_items             integer     NOT NULL DEFAULT 0,
  sap_status              text        NOT NULL DEFAULT '',
  sap_docentry            integer,
  sap_docnum              integer,
  sap_document_status     text        NOT NULL DEFAULT '',
  duplicate_records_status text       NOT NULL DEFAULT 'False/0',
  last_error              text        NOT NULL DEFAULT '',
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  UNIQUE (channel, po_number)
);

CREATE INDEX IF NOT EXISTS purchase_orders_status_idx
  ON b2b.purchase_orders (channel, pipeline_status, created_at DESC);

CREATE TABLE IF NOT EXISTS b2b.po_lines (
  id                    bigserial PRIMARY KEY,
  po_id                 bigint      NOT NULL REFERENCES b2b.purchase_orders(id) ON DELETE CASCADE,
  line_no               integer     NOT NULL DEFAULT 0,
  item_code             text        NOT NULL DEFAULT '',
  sap_code              text        NOT NULL DEFAULT '',
  item_name             text        NOT NULL DEFAULT '',
  quantity              numeric     NOT NULL DEFAULT 0,
  discount              numeric     NOT NULL DEFAULT 0,
  product_description    text        NOT NULL DEFAULT '',
  basic_cost_price      numeric     NOT NULL DEFAULT 0,
  igst_percentage        numeric     NOT NULL DEFAULT 0,
  tax_amount            numeric     NOT NULL DEFAULT 0,
  landing_rate          numeric     NOT NULL DEFAULT 0,
  mrp                   numeric     NOT NULL DEFAULT 0,
  total_amount          numeric     NOT NULL DEFAULT 0,
  tax_rate              numeric     NOT NULL DEFAULT 0,
  gst_tax               text        NOT NULL DEFAULT '',
  igst_tax              text        NOT NULL DEFAULT '',
  taxcode               text        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS po_lines_po_idx ON b2b.po_lines (po_id);

CREATE TABLE IF NOT EXISTS b2b.po_amend_history (
  id                    bigserial PRIMARY KEY,
  channel               text        NOT NULL DEFAULT 'blinkit',
  po_number             text        NOT NULL,
  po_id                 bigint      NOT NULL REFERENCES b2b.purchase_orders(id) ON DELETE CASCADE,
  record_data           jsonb       NOT NULL,
  sap_docentry          integer,
  sap_docnum            integer,
  sap_status            text        NOT NULL DEFAULT '',
  sap_document_status   text        NOT NULL DEFAULT '',
  pipeline_status       text        NOT NULL DEFAULT '',
  email_subject         text        NOT NULL DEFAULT '',
  email_uid             text        NOT NULL DEFAULT '',
  archived_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS po_amend_history_po_idx
  ON b2b.po_amend_history (channel, po_number, archived_at DESC);

CREATE TABLE IF NOT EXISTS b2b.sku_map (
  id          bigserial PRIMARY KEY,
  channel     text NOT NULL DEFAULT 'blinkit',
  item_code   text NOT NULL,
  sap_code    text NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (channel, item_code)
);

CREATE TABLE IF NOT EXISTS b2b.ship_to_aliases (
  id             bigserial PRIMARY KEY,
  channel        text NOT NULL DEFAULT 'blinkit',
  address_id     text NOT NULL,
  address_entry  text NOT NULL,
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ship_to_aliases_channel_idx
  ON b2b.ship_to_aliases (channel);

CREATE UNIQUE INDEX IF NOT EXISTS ship_to_aliases_uidx
  ON b2b.ship_to_aliases (channel, address_id);

CREATE TABLE IF NOT EXISTS b2b.processed_emails (
  id           bigserial PRIMARY KEY,
  channel      text NOT NULL,
  email_uid    text NOT NULL,
  po_number    text NOT NULL DEFAULT '',
  processed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (channel, email_uid)
);

CREATE TABLE IF NOT EXISTS b2b.notifications (
  id           bigserial PRIMARY KEY,
  channel      text        NOT NULL DEFAULT 'blinkit',
  kind         text        NOT NULL DEFAULT 'info',  -- success | warning | error | info
  title        text        NOT NULL,
  body         text        NOT NULL DEFAULT '',
  po_number    text        NOT NULL DEFAULT '',
  is_read      boolean     NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS notifications_unread_idx
  ON b2b.notifications (is_read, created_at DESC);

CREATE TABLE IF NOT EXISTS b2b.email_log (
  id           bigserial PRIMARY KEY,
  channel      text        NOT NULL DEFAULT 'blinkit',
  recipients   text        NOT NULL,
  subject      text        NOT NULL,
  po_number    text        NOT NULL DEFAULT '',
  sent_ok      boolean     NOT NULL DEFAULT false,
  error        text        NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS b2b.audit_log (
  id           bigserial PRIMARY KEY,
  channel      text        NOT NULL DEFAULT 'blinkit',
  po_number    text        NOT NULL DEFAULT '',
  actor        text        NOT NULL DEFAULT '',
  action       text        NOT NULL,
  detail       text        NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS b2b.roles (
  role              text PRIMARY KEY,
  can_approve       boolean NOT NULL DEFAULT false,
  can_post_sap      boolean NOT NULL DEFAULT false,
  can_manage_users  boolean NOT NULL DEFAULT false
);

INSERT INTO b2b.roles (role, can_approve, can_post_sap, can_manage_users) VALUES
  ('admin',    true,  true,  true),
  ('reviewer', true,  true,  false),
  ('viewer',   false, false, false)
ON CONFLICT (role) DO NOTHING;

CREATE TABLE IF NOT EXISTS b2b.users (
  id                    bigserial PRIMARY KEY,
  email                 text        NOT NULL UNIQUE,
  full_name             text        NOT NULL DEFAULT '',
  role                  text        NOT NULL REFERENCES b2b.roles(role),
  sap_salesperson_code  text        NOT NULL DEFAULT '',
  channels              text[]      NOT NULL DEFAULT ARRAY['blinkit']::text[],
  is_active             boolean     NOT NULL DEFAULT true,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
