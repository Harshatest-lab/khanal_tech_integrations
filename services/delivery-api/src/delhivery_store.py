"""PostgreSQL persistence for Delhivery shipment status + POD webhooks."""

from __future__ import annotations

import json
from typing import Any

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


INSERT_POD_EVENT = """
INSERT INTO delhivery_lr_pod (lrnum, waybill, order_id, epod_b64, raw_payload)
VALUES (%(lrnum)s, %(waybill)s, %(order_id)s, %(epod_b64)s, %(raw_payload)s::jsonb);
"""


def insert_pod_event(conn: Any, fields: dict[str, Any]) -> None:
    """Append a POD webhook row.  Multiple rows per lrnum are intentional."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                INSERT_POD_EVENT,
                {
                    "lrnum": fields["lrnum"],
                    "waybill": fields.get("waybill"),
                    "order_id": fields.get("order_id"),
                    "epod_b64": fields.get("epod_b64"),
                    "raw_payload": json.dumps(fields["payload"]),
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
