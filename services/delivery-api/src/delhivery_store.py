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
  INSERT INTO delhivery_lr_pod (lrnum, waybill, order_id, epod_b64, pod_url, pod_format, pod_date, raw_payload)
  SELECT
    e.lrnum,
    b.waybill_in,
    %(order_id)s,
    %(epod_b64)s,
    %(pod_url)s,
    %(pod_format)s,
    %(pod_date)s::date,
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
  RETURNING i.id, i.lrnum
)
SELECT id, lrnum FROM upd;
"""


FETCH_DOC_ENTRY_FOR_LR = """
SELECT NULLIF(btrim(doc_entry), '') AS doc_entry
FROM delhivery_lr
WHERE btrim(lrnum) = btrim(%(lrnum)s)
LIMIT 1
"""

FETCH_DOC_ENTRY_AND_STATUS_FOR_LR = """
SELECT NULLIF(btrim(doc_entry), '') AS doc_entry,
       NULLIF(btrim(delhivery_status), '') AS delhivery_status
FROM delhivery_lr
WHERE btrim(lrnum) = btrim(%(lrnum)s)
LIMIT 1
"""


UPDATE_POD_SAP_AUDIT = """
UPDATE delhivery_lr_pod SET sap_pod_response = (%(doc)s)::jsonb WHERE id = %(pod_id)s
"""


def save_pod_sap_sl_audit(conn: Any, pod_id: int, audit: dict[str, Any]) -> None:
    """Persist SAP Service Layer POD sync outcome on the POD row (does not touch ``delhivery_lr.sap_response``)."""
    _execute_one_statement(conn, UPDATE_POD_SAP_AUDIT, {"pod_id": pod_id, "doc": json.dumps(audit)})


def fetch_doc_entry_for_lr(conn: Any, lrnum: str) -> str | None:
    """Return ``delhivery_lr.doc_entry`` when set (SAP Service Layer ``DocEntry``)."""
    prev = conn.autocommit
    conn.autocommit = True
    try:
        cur = conn.execute(FETCH_DOC_ENTRY_FOR_LR, {"lrnum": lrnum})
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return str(row[0]).strip()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = prev


def fetch_doc_entry_and_delhivery_status_for_lr(
    conn: Any, lrnum: str
) -> tuple[str | None, str | None]:
    """Return ``(doc_entry, delhivery_status)`` from ``delhivery_lr`` for SAP POD PATCH context."""
    prev = conn.autocommit
    conn.autocommit = True
    try:
        cur = conn.execute(FETCH_DOC_ENTRY_AND_STATUS_FOR_LR, {"lrnum": lrnum})
        row = cur.fetchone()
        if row is None:
            return (None, None)
        de = None if row[0] is None else str(row[0]).strip()
        st = None if row[1] is None else str(row[1]).strip()
        return (de, st)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = prev


def apply_pod_webhook(conn: Any, fields: dict[str, Any]) -> tuple[int, str] | None:
    """Append a POD row when ``delhivery_lr`` exists; update ``latest_pod_id`` + ``pod_received``.

    Returns ``(new delhivery_lr_pod.id, resolved lrnum)``, or ``None`` if no matching LR.
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
                "pod_date": fields.get("pod_date"),
                "raw_payload": json.dumps(fields["payload"]),
            },
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return (int(row[0]), str(row[1]).strip())
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
