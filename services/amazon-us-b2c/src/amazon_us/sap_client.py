"""SAP B1 Service Layer access for Amazon US documents.

Built on ``khanal_shared.sap_sl`` (urllib + SSM-cached B1 session with
lock, profile ``inc`` -> ``/sap/inc/*``) — the same session machinery the
delivery-api and Walmart services already run in production, so all
services share one SAP session instead of each logging in separately.

Correctness rules this module enforces (each one is a paid-for lesson
from the Frappe pipeline's production run):

  - Duplicate lookups filter ``CancelStatus eq 'csNo'``. Cancellation
    documents share NumAtCard but do NOT carry Cancelled='tYES'; only
    CancelStatus separates live / cancelled / cancellation docs.
  - Duplicate lookups FAIL LOUD. A swallowed exception -> None -> "not
    posted yet" is how duplicate invoices happen; the caller must treat a
    failed lookup as "do not post", never as "safe to post".
  - Any use of a STORED DocEntry must verify NumAtCard ownership first
    (``get_invoice_doc`` returns NumAtCard for exactly that check). A
    stale pointer from a DB repoint once nearly cancelled an unrelated
    customer's document.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

from khanal_shared.sap_sl import build_opener_and_base_via_ssm, sap_sl_request_json
from khanal_shared.sap_sl.ssm_session import param_b1_session

logger = logging.getLogger(__name__)


class SapPostError(Exception):
    """SAP B1 rejected a document. Message carries SAP's error text."""


class SapLookupError(Exception):
    """A duplicate-guard or document lookup failed. NEVER treat as 'not found'."""


def _invalidate_cached_session() -> None:
    """Force the next build_opener_and_base_via_ssm() to re-login by
    expiring the shared SSM session parameter."""
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


def _request(method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
    """Service Layer request with one retry on a stale cached session (401)."""
    opener, base = build_opener_and_base_via_ssm()
    url = f"{base}/{path}"
    try:
        return sap_sl_request_json(opener, method, url, body=body)
    except RuntimeError as e:
        if "HTTP 401" in str(e):
            logger.info("SAP session stale (401); re-logging in")
            _invalidate_cached_session()
            opener, base = build_opener_and_base_via_ssm()
            return sap_sl_request_json(opener, method, f"{base}/{path}", body=body)
        raise


# ── Document posting ─────────────────────────────────────────────────────

def post_invoice(payload: dict) -> dict:
    return _post_document("Invoices", payload, "AR Invoice")


def post_credit_note(payload: dict) -> dict:
    return _post_document("CreditNotes", payload, "AR Credit Note")


def _post_document(endpoint: str, payload: dict, label: str) -> dict:
    """Returns {"doc_entry": int, "doc_num": int, "response": dict}."""
    try:
        status, body = _request("POST", endpoint, body=payload)
    except RuntimeError as e:
        raise SapPostError(f"SAP {label} failed: {_sap_error_text(str(e))}") from e

    if status not in (200, 201) or not isinstance(body, dict) or not body.get("DocEntry"):
        raise SapPostError(f"SAP {label} failed: HTTP {status}: {str(body)[:500]}")
    return {
        "doc_entry": int(body["DocEntry"]),
        "doc_num": int(body.get("DocNum") or 0),
        "response": body,
    }


def _sap_error_text(raw: str) -> str:
    """Pull SAP's error message value out of the raised HTTP body when possible."""
    try:
        json_part = raw[raw.index("{"):]
        msg = json.loads(json_part).get("error", {}).get("message", {})
        if isinstance(msg, dict):
            return msg.get("value") or raw[:500]
        return str(msg) or raw[:500]
    except Exception:
        return raw[:500]


# ── Lookups (loud) ───────────────────────────────────────────────────────

def find_invoice_by_order_id(amazon_order_id: str) -> dict | None:
    """Live (non-cancelled) AR Invoice with NumAtCard == order id, or None.
    Raises SapLookupError on any failure — callers must NOT post then."""
    return _find_by_num_at_card("Invoices", amazon_order_id)


def find_credit_note_by_ref(refund_id: str) -> dict | None:
    """Duplicate guard for credit notes (NumAtCard carries the refund id)."""
    return _find_by_num_at_card("CreditNotes", refund_id)


def _find_by_num_at_card(endpoint: str, ref: str) -> dict | None:
    escaped = ref.replace("'", "''")
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


def get_invoice_doc(doc_entry: int) -> dict:
    """Header fields + DocumentLines of a posted invoice. Callers verify
    NumAtCard against their order id before base-linking to doc_entry."""
    path = f"Invoices({int(doc_entry)})?$select=DocEntry,DocNum,NumAtCard,CancelStatus,DocDate,DocumentLines"
    try:
        status, body = _request("GET", path)
    except Exception as e:
        raise SapLookupError(f"Invoice fetch failed for DocEntry={doc_entry}: {e}") from e
    if status != 200 or not isinstance(body, dict):
        raise SapLookupError(f"Invoice fetch failed for DocEntry={doc_entry}: HTTP {status}")
    return body
