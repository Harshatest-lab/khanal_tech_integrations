"""PostgreSQL persistence for Delhivery shipment status + POD webhooks."""

from __future__ import annotations

import json
from typing import Any


def _execute_one_statement(conn: Any, query: str, params: dict[str, Any]) -> None:
    """Run a single atomic SQL statement without explicit BEGIN/COMMIT (fewer client↔server round-trips)."""
    prev = conn.autocommit
    conn.autocommit = True
    try:
        conn.execute(query, params)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = prev


# One statement = one client/server round-trip (lower latency than MERGE + INSERT separately).
UPSERT_SHIPMENT_STATUS_AND_AUDIT = """
WITH upserted AS (
  INSERT INTO delhivery_lr (
    lrnum, delhivery_status, status_type, shipment_remark, location, waybill,
    last_status_webhook, raw_tracking, updated_at, created_at
  ) VALUES (
    %(lrnum)s, %(delhivery_status)s, %(status_type)s, %(shipment_remark)s, %(location)s, %(waybill)s,
    %(payload)s::jsonb, '{}'::jsonb, now(), now()
  )
  ON CONFLICT (lrnum) DO UPDATE SET
    delhivery_status = COALESCE(EXCLUDED.delhivery_status, delhivery_lr.delhivery_status),
    status_type = COALESCE(EXCLUDED.status_type, delhivery_lr.status_type),
    shipment_remark = COALESCE(EXCLUDED.shipment_remark, delhivery_lr.shipment_remark),
    location = COALESCE(EXCLUDED.location, delhivery_lr.location),
    waybill = COALESCE(EXCLUDED.waybill, delhivery_lr.waybill),
    last_status_webhook = EXCLUDED.last_status_webhook,
    raw_tracking = delhivery_lr.raw_tracking,
    updated_at = now()
  RETURNING lrnum
)
INSERT INTO delhivery_lr_status_events (lrnum, payload)
SELECT upserted.lrnum, %(payload)s::jsonb FROM upserted;
"""


INSERT_POD_EVENT = """
INSERT INTO delhivery_lr_pod (lrnum, waybill, order_id, epod_b64, raw_payload)
VALUES (%(lrnum)s, %(waybill)s, %(order_id)s, %(epod_b64)s, %(raw_payload)s::jsonb);
"""


def insert_pod_event(conn: Any, fields: dict[str, Any]) -> None:
    """Append a POD webhook row.  Multiple rows per lrnum are intentional."""
    _execute_one_statement(
        conn,
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
        "payload": payload_json,
    }
    _execute_one_statement(conn, UPSERT_SHIPMENT_STATUS_AND_AUDIT, params)
