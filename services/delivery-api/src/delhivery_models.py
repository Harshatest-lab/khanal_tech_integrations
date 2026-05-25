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
