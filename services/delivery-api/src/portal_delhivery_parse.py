"""Pure Delhivery LTL track payload helpers (no HTTP / SSM deps — safe for unit tests)."""

from __future__ import annotations

from typing import Any


def unwrap_delhivery_payload(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("data"), dict):
        return raw["data"]
    return raw


def first_wbn(data: dict) -> dict:
    wbns = data.get("wbns")
    if isinstance(wbns, list) and wbns:
        first = wbns[0]
        return first if isinstance(first, dict) else {}
    return {}


def tracking_summary(tracking_raw: dict) -> dict[str, Any]:
    data = unwrap_delhivery_payload(tracking_raw)
    wbn = first_wbn(data)
    top_status = data.get("status")
    wbn_status = wbn.get("status")
    # LTL track often returns master waybill as top-level ``mwbn`` (no ``wbns`` array yet).
    waybill = (
        wbn.get("wbn")
        or data.get("mwbn")
        or data.get("wbn")
        or data.get("mwn")
        or data.get("waybill")
    )
    return {
        "delhivery_status": top_status or wbn_status,
        "status_type": wbn_status or top_status,
        "shipment_remark": wbn.get("scan_remark") or data.get("scan_remark"),
        "location": wbn.get("location") or data.get("location"),
        "waybill": str(waybill).strip() if waybill not in (None, "") else None,
    }


def normalize_delhivery_track_envelope(tracking_raw: dict, lrnum: str) -> dict:
    envelope = tracking_raw if isinstance(tracking_raw, dict) else {}
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else None
    if data is None:
        data = unwrap_delhivery_payload(envelope)
        envelope = {
            "success": envelope.get("success", True),
            "request_id": envelope.get("request_id"),
            "data": data,
        }
    if lrnum:
        data["lrnum"] = str(lrnum).strip()
        envelope["data"] = data
    return envelope


def envelope_has_lr(tracking_raw: dict, lr: str) -> bool:
    envelope = normalize_delhivery_track_envelope(tracking_raw, lr)
    tracked = (envelope.get("data") or {}).get("lrnum") if isinstance(envelope.get("data"), dict) else None
    return bool(tracked)
