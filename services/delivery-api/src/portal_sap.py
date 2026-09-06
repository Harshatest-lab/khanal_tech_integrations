"""Portal SAP Service Layer PATCH — writes audit to delhivery_lr.sap_response."""

from __future__ import annotations

from typing import Any, Optional

from khanal_shared.sap_sl import (
    build_opener_and_base_from_env,
    build_opener_and_base_via_ssm,
    sap_sl_enabled,
    sap_sl_request_json,
    sap_sl_use_ssm,
)

from src.portal_invoice import PortalValidationError


def _opener_base() -> tuple[Any, str]:
    if sap_sl_use_ssm():
        return build_opener_and_base_via_ssm()
    return build_opener_and_base_from_env()


def _sap_error_message(sap_body: dict, status_code: int) -> str:
    if isinstance(sap_body.get("error"), dict):
        m = sap_body["error"].get("message") or {}
        if isinstance(m, dict):
            val = m.get("value")
            if val and str(val).strip().lower() != "value":
                return str(val)
        elif isinstance(m, str) and m.strip():
            return m
    return f"SAP update failed ({status_code}). See logs for full response."


def patch_audit_failure(doc_entry: int, lrnum: str, error: str, url: Optional[str] = None) -> dict:
    return {
        "ok": False,
        "sap_sync_status": "failed",
        "doc_entry": doc_entry,
        "patch": {"U_LRtrackno": lrnum},
        "url": url,
        "error": (error or "")[:2000],
    }


def patch_audit_success(sap_body: dict, doc_entry: int) -> dict:
    body = dict(sap_body) if isinstance(sap_body, dict) else {}
    body["ok"] = True
    body["sap_sync_status"] = "synced"
    body["doc_entry"] = doc_entry
    return body


def patch_invoice_lr_track(doc_entry: int, lrnum: str) -> tuple[dict, dict, str]:
    if not sap_sl_enabled():
        raise PortalValidationError("SAP Service Layer is not configured.")

    opener, base = _opener_base()
    url = f"{base}/Invoices({doc_entry})"
    sap_payload = {"U_LRtrackno": lrnum}

    status, sap_body = sap_sl_request_json(opener, "PATCH", url, body=sap_payload)

    if sap_body is None:
        sap_body = {}
    if not isinstance(sap_body, dict):
        sap_body = {"result": sap_body}

    if status not in (200, 201, 204):
        raise PortalValidationError(_sap_error_message(sap_body, status))

    if not sap_body or status == 204:
        sap_body = {
            "ok": True,
            "sap_sync_status": "synced",
            "http_status": status,
            "doc_entry": doc_entry,
            "patch": sap_payload,
            "url": url,
        }
    else:
        sap_body = patch_audit_success(sap_body, doc_entry)

    return sap_payload, sap_body, url


def patch_invoice_pod_fields(
    doc_entry: int,
    lrnum: str,
    pod_url: str,
    pod_date: Optional[str] = None,
    frappe_grn_key: Optional[int] = None,
) -> tuple[dict, dict, str]:
    if not sap_sl_enabled():
        raise PortalValidationError("SAP Service Layer is not configured.")

    opener, base = _opener_base()
    url = f"{base}/Invoices({doc_entry})"
    sap_payload: dict[str, Any] = {
        "U_LRtrackno": (lrnum or "").strip(),
        "U_Pod_Link": (pod_url or "").strip(),
    }
    pdate = (str(pod_date).strip() if pod_date not in (None, "") else "") or ""
    if pdate:
        sap_payload["U_POD_DATE"] = pdate
    if frappe_grn_key is not None:
        sap_payload["U_FrappeGRNKey"] = int(frappe_grn_key)

    status, sap_body = sap_sl_request_json(opener, "PATCH", url, body=sap_payload)

    if sap_body is None:
        sap_body = {}
    if not isinstance(sap_body, dict):
        sap_body = {"result": sap_body}

    if status not in (200, 201, 204):
        raise PortalValidationError(_sap_error_message(sap_body, status))

    if not sap_body or status == 204:
        sap_body = {
            "ok": True,
            "sap_sync_status": "synced",
            "http_status": status,
            "doc_entry": doc_entry,
            "patch": sap_payload,
            "url": url,
        }
    else:
        sap_body = patch_audit_success(sap_body, doc_entry)

    return sap_payload, sap_body, url
