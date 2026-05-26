"""Parse Delhivery track API + shipment webhook payloads (no I/O)."""

from __future__ import annotations

import json
from typing import Any


def lrnum_from_track_response(body: dict[str, Any]) -> str | None:
    """Read lrnum from Delhivery GET /lrn/track JSON body."""
    data = body.get("data")
    if isinstance(data, dict):
        lr = data.get("lrnum")
        if isinstance(lr, str) and lr.strip():
            return lr.strip()
    return None


def normalize_shipment_webhook(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return flat fields for DB write; None if lrnum missing."""
    lrnum = payload.get("lrnum")
    if not isinstance(lrnum, str) or not lrnum.strip():
        return None
    lrnum = lrnum.strip()
    status = payload.get("status")
    status_s = status.strip() if isinstance(status, str) else None
    stype = payload.get("status_type")
    stype_s = stype.strip() if isinstance(stype, str) else None
    remark = payload.get("shipment_remark")
    remark_s = remark if isinstance(remark, str) else None
    loc = payload.get("location")
    loc_s = loc if isinstance(loc, str) else None
    waybill = payload.get("mwn") or payload.get("wbn")
    waybill_s = waybill.strip() if isinstance(waybill, str) else None
    return {
        "lrnum": lrnum,
        "delhivery_status": status_s,
        "status_type": stype_s,
        "shipment_remark": remark_s,
        "location": loc_s,
        "waybill": waybill_s,
        "payload": payload,
    }


def normalize_pod_webhook(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return flat fields for DB write; None if both lrnum and waybill are missing.

    Delhivery sends:
        { "waybill": "…", "EPOD": "<base64>", "orderID": "…", "lrnum": "…" }

    Notes:
      - lrnum is the canonical key; waybill is a fallback identifier.
      - EPOD key name may also appear as 'EPOD' with different casing; we try both.
      - Multiple POD pushes per LR are normal (revised PODs from audit team).
    """
    lrnum = payload.get("lrnum") or payload.get("lr_num")
    lrnum_s = lrnum.strip() if isinstance(lrnum, str) else None

    waybill = payload.get("waybill")
    waybill_s = waybill.strip() if isinstance(waybill, str) else None

    if not lrnum_s and not waybill_s:
        return None

    epod = payload.get("EPOD") or payload.get("epod") or payload.get("Epod")
    epod_s = epod.strip() if isinstance(epod, str) else None

    order_id = payload.get("orderID") or payload.get("order_id")
    order_id_s = order_id.strip() if isinstance(order_id, str) else None

    return {
        "lrnum": lrnum_s or waybill_s,   # use waybill as fallback key
        "waybill": waybill_s,
        "order_id": order_id_s,
        "epod_b64": epod_s,
        "payload": payload,
    }


def body_json(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        return {}
