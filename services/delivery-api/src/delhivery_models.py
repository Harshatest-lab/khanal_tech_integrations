"""Parse Delhivery track API + shipment/POD webhook payloads (no I/O)."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

# Site calendar for SAP ``U_POD_DATE`` (epoch / naive timestamps → local date).
SITE_TZ = ZoneInfo("Asia/Kolkata")

PodDateOrigin = Literal[
    "pod_raw_payload",
    "last_status_webhook",
    "status_events_payload",
    "status_events_received_at",
    "delhivery_lr_updated_at",
    "none",
]

# Forecast fields — never use for U_POD_DATE.
_FORECAST_DATE_KEYS = frozenset(
    {
        "expected_delivery_date",
        "promised_delivery_date",
        "ExpectedDeliveryDate",
        "PromisedDeliveryDate",
        "expectedDeliveryDate",
        "promisedDeliveryDate",
    }
)

# Priority-1 keys on the POD webhook for origin ``pod_raw_payload``.
_POD_WEBHOOK_DATE_KEYS = ("PODDate", "podDate", "POD_DATE", "pod_date")

# Status / delivery event date keys (plus ``timestamp`` fallback).
_STATUS_DATE_KEYS = (
    "U_POD_DATE",
    "PODDate",
    "podDate",
    "pod_date",
    "POD_DATE",
    "deliveryDate",
    "DeliveredDate",
    "delivered_date",
    "DeliveryDate",
    "status_date",
    "StatusDate",
)


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


def coerce_to_site_date(value: Any) -> date | None:
    """Normalize a Delhivery/SAP-ish date value to a calendar ``date`` in Asia/Kolkata.

    Supports ``date``, aware/naive ``datetime``, ISO strings, common ``DD/MM/YYYY`` /
    ``DD-MM-YYYY``, and unix epoch seconds or milliseconds.
    """
    if value is None or isinstance(value, bool):
        return None
    if type(value) is date:
        return value
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SITE_TZ)
        return dt.astimezone(SITE_TZ).date()
    if isinstance(value, (int, float)):
        return _epoch_to_site_date(float(value))
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            try:
                return _epoch_to_site_date(float(s))
            except (OSError, ValueError, OverflowError):
                return None
        # ISO / RFC3339
        try:
            if "T" in s or s.endswith("Z") or (len(s) > 10 and ("+" in s[10:] or s.count("-") > 2)):
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=SITE_TZ)
                return dt.astimezone(SITE_TZ).date()
        except ValueError:
            pass
        if len(s) >= 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s[:10]):
            try:
                return date.fromisoformat(s[:10])
            except ValueError:
                pass
        candidates = [s]
        # Allow ``DD/MM/YYYY …`` with a time suffix.
        for sep in (" ", "T"):
            if sep in s:
                candidates.append(s.split(sep, 1)[0])
                break
        for cand in candidates:
            for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%Y/%m/%d", "%m/%d/%Y"):
                try:
                    return datetime.strptime(cand, fmt).date()
                except ValueError:
                    continue
        return None
    return None


def _epoch_to_site_date(ts: float) -> date | None:
    """Unix seconds or milliseconds → Asia/Kolkata calendar date."""
    try:
        # Heuristic: ≥ 1e12 ≈ milliseconds since epoch.
        seconds = ts / 1000.0 if abs(ts) >= 1_000_000_000_000 else ts
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(SITE_TZ).date()
    except (OSError, ValueError, OverflowError):
        return None


def _date_from_payload_keys(payload: dict[str, Any], keys: tuple[str, ...]) -> date | None:
    for k in keys:
        if k in _FORECAST_DATE_KEYS:
            continue
        if k not in payload:
            continue
        d = coerce_to_site_date(payload.get(k))
        if d is not None:
            return d
    return None


def extract_pod_webhook_date(payload: dict[str, Any] | None) -> date | None:
    """Priority-1 POD webhook date keys only (``PODDate`` / ``podDate`` / ``POD_DATE`` / ``pod_date``)."""
    if not isinstance(payload, dict):
        return None
    return _date_from_payload_keys(payload, _POD_WEBHOOK_DATE_KEYS)


def extract_status_payload_date(payload: dict[str, Any] | None) -> date | None:
    """Delivery / status-event date from a shipment-status JSON body (never forecast dates)."""
    if not isinstance(payload, dict):
        return None
    d = _date_from_payload_keys(payload, _STATUS_DATE_KEYS)
    if d is not None:
        return d
    # ``timestamp`` is the usual Delhivery status-event clock.
    if "timestamp" in payload:
        return coerce_to_site_date(payload.get("timestamp"))
    return None


def _parse_pod_date(payload: dict[str, Any]) -> date | None:
    """Best-effort POD / delivery date for RDS ``pod_date`` (broader than SAP priority-1 keys)."""
    d = extract_pod_webhook_date(payload)
    if d is not None:
        return d
    return extract_status_payload_date(payload)


def frappe_grn_key_from_delhivery_status(status: str | None) -> int | None:
    """Map Delhivery-style status text to SAP ``U_FrappeGRNKey`` (integer).

    Case-insensitive substring rules (``rto`` checked first):

    - contains ``rto`` → ``2``
    - contains ``deliver`` → ``1``
    - else → ``None`` (caller omits the UDF on PATCH)
    """
    if not isinstance(status, str):
        return None
    s = status.strip().casefold()
    if not s:
        return None
    if "rto" in s:
        return 2
    if "deliver" in s:
        return 1
    return None


def is_delivered_like_status(status: str | None) -> bool:
    """True when status maps to GRN ``1`` (contains ``deliver``, not RTO-first)."""
    return frappe_grn_key_from_delhivery_status(status) == 1


def format_sap_pod_date(d: date | None) -> str | None:
    """SAP ``U_POD_DATE`` as ``YYYY-MM-DD``."""
    if d is None:
        return None
    return d.isoformat()


def resolve_u_pod_date(
    *,
    pod_payload: dict[str, Any] | None = None,
    delhivery_status: str | None = None,
    last_status_webhook: dict[str, Any] | None = None,
    delivered_event_payload: dict[str, Any] | None = None,
    delivered_event_received_at: Any = None,
    lr_updated_at: Any = None,
) -> tuple[str | None, PodDateOrigin]:
    """Resolve SAP ``U_POD_DATE`` + origin label.

    Priority:
      1. POD webhook ``PODDate`` / ``podDate`` / ``POD_DATE`` / ``pod_date``
      2. If status is delivered-like:
         last_status_webhook date → newest delivered status-event payload date →
         that event ``received_at`` → ``delhivery_lr.updated_at``
    """
    pod_d = extract_pod_webhook_date(pod_payload)
    if pod_d is not None:
        return format_sap_pod_date(pod_d), "pod_raw_payload"

    if not is_delivered_like_status(delhivery_status):
        # Still allow last_status_webhook if *its* status is delivered-like (POD path
        # may run while LR status is already Delivered even when this function is
        # called with an older status string — callers should pass LR status).
        return None, "none"

    d = extract_status_payload_date(last_status_webhook)
    if d is not None:
        return format_sap_pod_date(d), "last_status_webhook"

    d = extract_status_payload_date(delivered_event_payload)
    if d is not None:
        return format_sap_pod_date(d), "status_events_payload"

    d = coerce_to_site_date(delivered_event_received_at)
    if d is not None:
        return format_sap_pod_date(d), "status_events_received_at"

    d = coerce_to_site_date(lr_updated_at)
    if d is not None:
        return format_sap_pod_date(d), "delhivery_lr_updated_at"

    return None, "none"


def normalize_shipment_webhook(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return flat fields for DB write; None if lrnum missing.

    ``status_date`` is the event date from this status push (often ``timestamp``).
    When status is delivered-like, SAP sync may use RDS resolution for ``U_POD_DATE``.
    """
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
    waybill = (
        payload.get("mwn")
        or payload.get("wbn")
        or payload.get("waybill")
        or payload.get("Waybill")
        or payload.get("WBN")
        or payload.get("MWN")
    )
    if isinstance(waybill, (int, float)) and not isinstance(waybill, bool):
        waybill_s = str(int(waybill)) if waybill == int(waybill) else str(waybill)
    else:
        waybill_s = waybill.strip() if isinstance(waybill, str) else None
        if waybill_s == "":
            waybill_s = None
    return {
        "lrnum": lrnum,
        "delhivery_status": status_s,
        "status_type": stype_s,
        "shipment_remark": remark_s,
        "location": loc_s,
        "waybill": waybill_s,
        "status_date": extract_status_payload_date(payload),
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
