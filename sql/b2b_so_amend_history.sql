-- B2B Blinkit amend: archive old PO snapshots before re-ingest.
-- Run once on existing khanaltestdb:
--   psql "$DATABASE_URL" -f sql/b2b_so_amend_history.sql
-- Safe to re-run (IF NOT EXISTS).

SET search_path TO b2b, public;

ALTER TABLE b2b.purchase_orders
  ADD COLUMN IF NOT EXISTS duplicate_records_status text NOT NULL DEFAULT 'False/0';

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

UPDATE b2b.purchase_orders
SET duplicate_records_status = 'False/0'
WHERE duplicate_records_status IS NULL OR duplicate_records_status = '';
