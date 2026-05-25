"""PostgreSQL persistence for Delhivery LRN tracking + status webhooks."""

from __future__ import annotations

import json
from typing import Any

UPSERT_TRACKING_SNAPSHOT = """
INSERT INTO delhivery_lr (
  lrnum, delhivery_status, raw_tracking, updated_at, created_at
) VALUES (
  %(lrnum)s, %(delhivery_status)s, %(raw_tracking)s::jsonb, now(), now()
)
ON CONFLICT (lrnum) DO UPDATE SET
  delhivery_status = EXCLUDED.delhivery_status,
  raw_tracking = EXCLUDED.raw_tracking,
  updated_at = now();
"""

MERGE_WEBHOOK = """
INSERT INTO delhivery_lr (
  lrnum, delhivery_status, status_type, shipment_remark, location, waybill,
  last_status_webhook, raw_tracking, updated_at, created_at
) VALUES (
  %(lrnum)s, %(delhivery_status)s, %(status_type)s, %(shipment_remark)s, %(location)s, %(waybill)s,
  %(last_status_webhook)s::jsonb, '{}'::jsonb, now(), now()
)
ON CONFLICT (lrnum) DO UPDATE SET
  delhivery_status = COALESCE(EXCLUDED.delhivery_status, delhivery_lr.delhivery_status),
  status_type = COALESCE(EXCLUDED.status_type, delhivery_lr.status_type),
  shipment_remark = COALESCE(EXCLUDED.shipment_remark, delhivery_lr.shipment_remark),
  location = COALESCE(EXCLUDED.location, delhivery_lr.location),
  waybill = COALESCE(EXCLUDED.waybill, delhivery_lr.waybill),
  last_status_webhook = EXCLUDED.last_status_webhook,
  raw_tracking = delhivery_lr.raw_tracking,
  updated_at = now();
"""

INSERT_STATUS_EVENT = """
INSERT INTO delhivery_lr_status_events (lrnum, payload) VALUES (%(lrnum)s, %(payload)s::jsonb);
"""


def upsert_tracking_snapshot(conn: Any, lrnum: str, track_response: dict[str, Any]) -> None:
    """Store / replace Delhivery GET /lrn/track JSON for an lrnum (Frappe calls after submit)."""
    status: str | None = None
    data = track_response.get("data")
    if isinstance(data, dict):
        st = data.get("status")
        if isinstance(st, str):
            status = st
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                UPSERT_TRACKING_SNAPSHOT,
                {
                    "lrnum": lrnum,
                    "delhivery_status": status,
                    "raw_tracking": json.dumps(track_response),
                },
            )


def apply_shipment_status_webhook(conn: Any, fields: dict[str, Any]) -> None:
    """Apply Delhivery shipment status webhook + append audit row."""
    lrnum = fields["lrnum"]
    payload_json = json.dumps(fields["payload"])
    params = {
        "lrnum": lrnum,
        "delhivery_status": fields.get("delhivery_status"),
        "status_type": fields.get("status_type"),
        "shipment_remark": fields.get("shipment_remark"),
        "location": fields.get("location"),
        "waybill": fields.get("waybill"),
        "last_status_webhook": payload_json,
    }
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(MERGE_WEBHOOK, params)
            cur.execute(INSERT_STATUS_EVENT, {"lrnum": lrnum, "payload": payload_json})
