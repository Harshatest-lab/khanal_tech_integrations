-- Delhivery LRN tracking + shipment status webhooks (run once per environment).
-- Adjust schema name if you use a dedicated schema.

CREATE TABLE IF NOT EXISTS delhivery_lr (
  lrnum text PRIMARY KEY,
  delhivery_status text,
  status_type text,
  shipment_remark text,
  location text,
  waybill text,
  raw_tracking jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_status_webhook jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS delhivery_lr_status_events (
  id bigserial PRIMARY KEY,
  lrnum text NOT NULL REFERENCES delhivery_lr (lrnum) ON DELETE CASCADE,
  payload jsonb NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_delhivery_lr_status_events_lrnum_received
  ON delhivery_lr_status_events (lrnum, received_at DESC);

-- POD (Proof of Delivery) events: append-only, one row per push from Delhivery.
-- Delhivery may re-push a revised POD, so multiple rows per lrnum are expected.
CREATE TABLE IF NOT EXISTS delhivery_lr_pod (
  id          bigserial PRIMARY KEY,
  lrnum       text        NOT NULL,
  waybill     text,
  order_id    text,
  epod_b64    text,                          -- raw base64 string from Delhivery
  pod_url     text,                          -- time-limited S3 (or other) URL when Delhivery sends URL mode
  pod_format  text,                          -- base64 | url | mixed | raw (hint for clients)
  pod_date    date,                          -- POD / delivery date from webhook (SAP U_POD_DATE)
  sap_pod_response jsonb,                     -- Lambda-only: last SAP Service Layer PATCH audit for this POD row (Frappe uses delhivery_lr.sap_response separately)
  raw_payload jsonb       NOT NULL DEFAULT '{}'::jsonb,
  received_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_delhivery_lr_pod_lrnum_received
  ON delhivery_lr_pod (lrnum, received_at DESC);

-- Column additions (safe to re-run on every deploy via ALTER TABLE IF NOT EXISTS)
ALTER TABLE delhivery_lr ADD COLUMN IF NOT EXISTS test text;
ALTER TABLE delhivery_lr ADD COLUMN IF NOT EXISTS doc_entry text;
ALTER TABLE delhivery_lr ADD COLUMN IF NOT EXISTS doc_num text;
ALTER TABLE delhivery_lr ADD COLUMN IF NOT EXISTS series text;
ALTER TABLE delhivery_lr ADD COLUMN IF NOT EXISTS sap_response jsonb;

-- POD: latest pointer on parent LR (history stays in delhivery_lr_pod).
ALTER TABLE delhivery_lr ADD COLUMN IF NOT EXISTS latest_pod_id bigint;
ALTER TABLE delhivery_lr ADD COLUMN IF NOT EXISTS pod_received boolean NOT NULL DEFAULT false;

ALTER TABLE delhivery_lr_pod ADD COLUMN IF NOT EXISTS pod_url text;
ALTER TABLE delhivery_lr_pod ADD COLUMN IF NOT EXISTS pod_format text;
-- POD delivery / document date from webhook (mirrors SAP U_POD_DATE when synced).
ALTER TABLE delhivery_lr_pod ADD COLUMN IF NOT EXISTS pod_date date;
-- SAP B1 POD→Invoice PATCH audit (Lambda); keep ``delhivery_lr.sap_response`` for Frappe LR payloads.
ALTER TABLE delhivery_lr_pod ADD COLUMN IF NOT EXISTS sap_pod_response jsonb;

-- FK from parent → latest pod row (idempotent add).
DO $$
BEGIN
  ALTER TABLE delhivery_lr
    ADD CONSTRAINT fk_delhivery_lr_latest_pod_id
    FOREIGN KEY (latest_pod_id) REFERENCES delhivery_lr_pod (id) ON DELETE SET NULL;
EXCEPTION
  WHEN duplicate_object THEN NULL;
END
$$;
