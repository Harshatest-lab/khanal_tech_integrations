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
