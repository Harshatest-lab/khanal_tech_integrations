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


# Resolve lrnum from payload or waybill lookup; insert POD only when parent LR exists;
# set delhivery_lr.latest_pod_id + pod_received (independent of shipment status / Delivered).
APPLY_POD_WEBHOOK = """
WITH bound AS (
  SELECT
    CAST(%(lrnum)s AS text) AS lrnum_in,
    CAST(%(waybill)s AS text) AS waybill_in
),
resolved AS (
  SELECT
    CASE
      WHEN btrim(b.lrnum_in) <> '' THEN btrim(b.lrnum_in)
      WHEN btrim(b.waybill_in) <> '' THEN (
        SELECT d.lrnum FROM delhivery_lr d WHERE btrim(d.waybill) = btrim(b.waybill_in) LIMIT 1
      )
      ELSE NULL
    END AS lrnum
  FROM bound b
),
exists_ok AS (
  SELECT r.lrnum FROM resolved r
  INNER JOIN delhivery_lr d ON btrim(d.lrnum) = r.lrnum
),
ins AS (
  INSERT INTO delhivery_lr_pod (lrnum, waybill, order_id, epod_b64, pod_url, pod_format, raw_payload)
  SELECT
    e.lrnum,
    b.waybill_in,
    %(order_id)s,
    %(epod_b64)s,
    %(pod_url)s,
    %(pod_format)s,
    %(raw_payload)s::jsonb
  FROM exists_ok e
  CROSS JOIN bound b
  RETURNING id, lrnum
),
upd AS (
  UPDATE delhivery_lr l
  SET
    latest_pod_id = i.id,
    pod_received = true,
    updated_at = now()
  FROM ins i
  WHERE l.lrnum = i.lrnum
  RETURNING i.id
)
SELECT id FROM upd;
"""


def apply_pod_webhook(conn: Any, fields: dict[str, Any]) -> int | None:
    """Append a POD row when ``delhivery_lr`` exists; update ``latest_pod_id`` + ``pod_received``.

    Returns new ``delhivery_lr_pod.id``, or ``None`` if no matching LR (unknown lrnum / waybill).
    """
    # Never pass None for lrnum/waybill placeholders: ``btrim(NULL)`` makes Postgres unable to
    # infer parameter types (psycopg ``AmbiguousParameter`` on ``WHEN %(lrnum)s IS NOT NULL ...``).
    lr_raw = fields.get("lrnum")
    lr_val = "" if lr_raw is None else str(lr_raw).strip()
    wb_raw = fields.get("waybill")
    wb_val = "" if wb_raw is None else str(wb_raw).strip()

    prev = conn.autocommit
    conn.autocommit = True
    try:
        cur = conn.execute(
            APPLY_POD_WEBHOOK,
            {
                "lrnum": lr_val,
                "waybill": wb_val,
                "order_id": fields.get("order_id"),
                "epod_b64": fields.get("epod_b64"),
                "pod_url": fields.get("pod_url"),
                "pod_format": fields.get("pod_format") or "raw",
                "raw_payload": json.dumps(fields["payload"]),
            },
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = prev


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
