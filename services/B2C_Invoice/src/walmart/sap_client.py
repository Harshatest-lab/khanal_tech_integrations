"""SAP B1 Service Layer access for Walmart US documents.

Built on ``khanal_shared.sap_sl`` (SSM-cached B1 session, profile ``inc``).
Duplicate lookups filter CancelStatus eq 'csNo' and fail loud.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

from khanal_shared.sap_sl import (
    build_opener_and_base_from_env,
    build_opener_and_base_via_ssm,
    sap_sl_enabled,
    sap_sl_request_json,
    sap_sl_use_ssm,
)
from khanal_shared.sap_sl.ssm_session import param_b1_session

logger = logging.getLogger(__name__)


class SapPostError(Exception):
    """SAP B1 rejected a document."""


class SapLookupError(Exception):
    """A duplicate-guard or document lookup failed. Never treat as 'not found'."""


def _invalidate_cached_session() -> None:
    import boto3

    try:
        ssm = boto3.client("ssm")
        name = param_b1_session()
        raw = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        data = json.loads(raw)
        data["expires_on"] = "1970-01-01T00:00:00+00:00"
        data["is_locked"] = False
        ssm.put_parameter(Name=name, Value=json.dumps(data), Type="SecureString", Overwrite=True)
    except Exception:
        logger.warning("Could not invalidate cached SAP session", exc_info=True)


def _opener_and_base():
    if not sap_sl_enabled():
        raise SapLookupError("SAP SL is not configured")
    if sap_sl_use_ssm():
        return build_opener_and_base_via_ssm()
    return build_opener_and_base_from_env()


def _request(method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
    opener, base = _opener_and_base()
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    status, resp_body = sap_sl_request_json(opener, method, url, body=body)
    if status == 401:
        logger.info("SAP session stale (401); re-logging in")
        _invalidate_cached_session()
        opener, base = _opener_and_base()
        return sap_sl_request_json(
            opener, method, f"{base.rstrip('/')}/{path.lstrip('/')}", body=body
        )
    return status, resp_body


def post_credit_note(payload: dict[str, Any]) -> dict[str, Any]:
    """POST /CreditNotes. Returns doc_entry, doc_num, response."""
    try:
        status, body = _request("POST", "CreditNotes", body=payload)
    except RuntimeError as e:
        raise SapPostError(f"SAP AR Credit Note failed: {_sap_error_text(str(e))}") from e

    if status not in (200, 201) or not isinstance(body, dict) or not body.get("DocEntry"):
        raise SapPostError(f"SAP AR Credit Note failed: HTTP {status}: {str(body)[:500]}")
    return {
        "doc_entry": int(body["DocEntry"]),
        "doc_num": int(body.get("DocNum") or 0),
        "response": body,
    }


def cancel_credit_note(doc_entry: int) -> dict[str, Any]:
    """POST /CreditNotes({DocEntry})/Cancel — reverses a posted CN in SAP."""
    path = f"CreditNotes({int(doc_entry)})/Cancel"
    try:
        status, body = _request("POST", path, body={})
    except RuntimeError as e:
        raise SapPostError(
            f"SAP Credit Note cancel failed (DocEntry={doc_entry}): {_sap_error_text(str(e))}"
        ) from e

    # SL may return 204 No Content or 200/201 with a body.
    if status not in (200, 201, 204):
        raise SapPostError(
            f"SAP Credit Note cancel failed (DocEntry={doc_entry}): HTTP {status}: {str(body)[:500]}"
        )
    return {
        "doc_entry": int(doc_entry),
        "response": body if isinstance(body, dict) else {"http_status": status},
    }


def _sap_error_text(raw: str) -> str:
    try:
        json_part = raw[raw.index("{") :]
        msg = json.loads(json_part).get("error", {}).get("message", {})
        if isinstance(msg, dict):
            return msg.get("value") or raw[:500]
        return str(msg) or raw[:500]
    except Exception:
        return raw[:500]


def find_invoice_by_customer_order_id(customer_order_id: str) -> dict[str, int] | None:
    """Live (non-cancelled) AR Invoice with NumAtCard == customerOrderId."""
    return _find_by_num_at_card("Invoices", customer_order_id)


def find_credit_note_by_ref(ref: str) -> dict[str, int] | None:
    """Duplicate guard: CreditNotes NumAtCard == return_order_id."""
    return _find_by_num_at_card("CreditNotes", ref)


def _find_by_num_at_card(endpoint: str, ref: str) -> dict[str, int] | None:
    escaped = (ref or "").replace("'", "''")
    path = quote(
        f"{endpoint}?$filter=NumAtCard eq '{escaped}' and CancelStatus eq 'csNo'"
        "&$select=DocEntry,DocNum&$top=1",
        safe="/?$&=',",
    )
    try:
        status, body = _request("GET", path)
    except Exception as e:
        raise SapLookupError(f"Duplicate check failed for {ref}: {e}") from e
    if status != 200 or not isinstance(body, dict):
        raise SapLookupError(f"Duplicate check failed for {ref}: HTTP {status}")
    rows = body.get("value", [])
    if not rows:
        return None
    return {"doc_entry": int(rows[0]["DocEntry"]), "doc_num": int(rows[0].get("DocNum") or 0)}


def get_invoice_doc(doc_entry: int) -> dict[str, Any]:
    """Full invoice header + DocumentLines for base-linking a credit note."""
    path = (
        f"Invoices({int(doc_entry)})"
        "?$select=DocEntry,DocNum,NumAtCard,CancelStatus,DocDate,PayToCode,DocumentLines"
    )
    try:
        status, body = _request("GET", path)
    except Exception as e:
        raise SapLookupError(f"Invoice fetch failed for DocEntry={doc_entry}: {e}") from e
    if status != 200 or not isinstance(body, dict):
        raise SapLookupError(f"Invoice fetch failed for DocEntry={doc_entry}: HTTP {status}")
    return body
