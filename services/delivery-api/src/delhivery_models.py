"""Parse Delhivery track API + shipment webhook payloads (no I/O)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any


def lrnum_from_track_response(body: dict[str, Any]) -> str | None:
    """Read lrnum from Delhivery GET /lrn/track JSON body."""
    data = body.get("data")
    if isinstance(data, dict):
        lr = data.get("lrnum")
        if isinstance(lr, str) and lr.strip():
            return lr.strip()
    return None


def _normalize_lr_key(raw: Any) -> str | None:
    """Strip whitespace and invisible Unicode so ``lrnum`` matches ``delhivery_lr.lrnum`` (Postgres ``btrim`` won't remove ZWSP/BOM)."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        s = str(raw)
    elif isinstance(raw, float) and raw == int(raw):
        s = str(int(raw))
    elif isinstance(raw, str):
        s = raw
    else:
        return None
    for ch in ("\ufeff", "\u200b", "\u200c", "\u200d"):
        s = s.replace(ch, "")
    s = s.strip()
    return s if s else None


def _first_non_empty_str(payload: dict[str, Any], *keys: str) -> str | None:
    for k in keys:
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _parse_pod_date(payload: dict[str, Any]) -> date | None:
    """Best-effort POD / delivery date from Delhivery-style keys (maps to SAP ``U_POD_DATE``)."""
    for k in (
        "U_POD_DATE",
        "PODDate",
        "podDate",
        "pod_date",
        "POD_DATE",
        "deliveryDate",
        "DeliveredDate",
        "delivered_date",
    ):
        v = payload.get(k)
        if v is None:
            continue
        if isinstance(v, datetime):
            return v.date()
        if type(v) is date:
            return v
        if isinstance(v, str) and v.strip():
            s = v.strip()
            try:
                if "T" in s or s.endswith("Z") or "+" in s[-6:]:
                    return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
                return date.fromisoformat(s[:10])
            except ValueError:
                continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                return datetime.fromtimestamp(float(v), tz=timezone.utc).date()
            except (OSError, ValueError, OverflowError):
                continue
    ts = payload.get("timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).date()
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(ts, str) and ts.strip().isdigit():
        try:
            return datetime.fromtimestamp(int(ts.strip()), tz=timezone.utc).date()
        except (OSError, ValueError, OverflowError):
            return None
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
    """Return flat fields for DB write; None if identifiers and POD content are unusable.

    Delhivery may send:
      - Base64 EPOD, and/or
      - A time-limited S3 (or other) URL, and/or
      - Other fields (treated as ``raw`` / form-like JSON).

    ``lrnum`` is only taken from the payload (not copied from waybill). If ``lrnum`` is
    absent, the worker resolves the parent row via ``delhivery_lr.waybill`` before insert.

    Multiple POD pushes per LR (re-triggers / revised PODs) are normal.
    """
    lr_raw = payload.get("lrnum")
    if lr_raw is None:
        lr_raw = payload.get("lr_num")
    lrnum_s = _normalize_lr_key(lr_raw)

    waybill_s = _normalize_lr_key(payload.get("waybill"))

    if not lrnum_s and not waybill_s:
        return None

    epod = payload.get("EPOD") or payload.get("epod") or payload.get("Epod")
    epod_s = epod.strip() if isinstance(epod, str) and epod.strip() else None

    pod_url_s = _first_non_empty_str(
        payload,
        "pod_url",
        "PODUrl",
        "podURL",
        "s3_url",
        "download_url",
        "epod_url",
        "EPODUrl",
        "pod_link",
    )

    order_id = payload.get("orderID") or payload.get("order_id")
    order_id_s = order_id.strip() if isinstance(order_id, str) and order_id.strip() else None

    if epod_s and pod_url_s:
        pod_format = "mixed"
    elif epod_s:
        pod_format = "base64"
    elif pod_url_s:
        pod_format = "url"
    else:
        pod_format = "raw"

    pod_date = _parse_pod_date(payload)

    return {
        "lrnum": lrnum_s,
        "waybill": waybill_s,
        "order_id": order_id_s,
        "epod_b64": epod_s,
        "pod_url": pod_url_s,
        "pod_format": pod_format,
        "pod_date": pod_date,
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
