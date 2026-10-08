"""SAP B1 Service Layer access for Amazon UK AR Invoices.

Built on ``khanal_shared.sap_sl`` — the same session/HTTP machinery the
delivery-api, Walmart and Amazon US services already run in production.
One fresh Login per run (safe for a once-daily cron): credentials come
from ``amazon_uk.credentials.get_sap_sl_credentials()`` (SSM when
``SAP_SL_USE_SSM`` is set, else plain env vars), but — unlike the Walmart
/ US pipelines — the *session* itself is never cached in the shared SSM
``/sap/inc*`` parameters, so this service can't churn or contend on their
session lock.

UK-specific behaviour kept from the standalone pipeline's sap_b1.py:
every ``U_*`` UDF in the payload is stripped from the create POST (SAP
silently drops UDFs sent inline) and re-applied via a follow-up PATCH,
then the invoice is re-fetched so the stored sap_response reflects the
patched UDF values.

Duplicate guard: lookups filter ``CancelStatus eq 'csNo'`` and FAIL LOUD
on error — a swallowed exception treated as "not posted" is how duplicate
invoices happen.
"""

from __future__ import annotations

import time
from urllib.parse import quote

from khanal_shared.sap_sl import build_sap_cookie_opener, sap_sl_request_json

from amazon_uk import credentials

_VERIFY_SSL = True
_HEADERS = {"Content-Type": "application/json"}

# In-process session cache — a cron run makes a handful of calls in one
# invocation, so one Login serves the whole run. Renewed on 401 or expiry.
_session = {"opener": None, "base": "", "expires_at": 0.0}
_SESSION_TTL_SECONDS = 25 * 60  # SAP default timeout is 30 min; renew early


class SapPostError(RuntimeError):
    """SAP rejected the document. Carries the raw response body so the
    caller can store exactly what SAP said (amazonuk.sap_postings)."""

    def __init__(self, message: str, response_body: dict | None = None):
        super().__init__(message)
        self.response_body = response_body


class SapLookupError(RuntimeError):
    """A duplicate-guard/document lookup failed. NEVER treat as 'not found'."""


def extract_sap_error(body: dict) -> str:
    """SAP B1 Service Layer nests the real message at error.message.value."""
    err = "Unknown SAP error"
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        m = body["error"].get("message")
        err = m.get("value", err) if isinstance(m, dict) else (m or err)
    return err


def _login() -> None:
    sap = credentials.get_sap_sl_credentials()
    base = sap.base_url.strip().rstrip("/")
    opener = build_sap_cookie_opener()
    status, body = sap_sl_request_json(
        opener,
        "POST",
        f"{base}/Login",
        {"CompanyDB": sap.companydb, "UserName": sap.username, "Password": sap.password},
    )
    if status != 200:
        raise SapPostError(f"SAP Login failed (HTTP {status}): {extract_sap_error(body or {})}")
    _session["opener"] = opener
    _session["base"] = base
    _session["expires_at"] = time.time() + _SESSION_TTL_SECONDS


def _get_session():
    if _session["opener"] is None or time.time() >= _session["expires_at"]:
        _login()
    # base already ends in "/b1s/v1" (no trailing slash) — join with "/".
    return _session["opener"], _session["base"].rstrip("/") + "/"


def _request(method: str, path: str, body: dict | None = None,
             timeout: int = 60) -> tuple[int, dict]:
    """One Service Layer request with a single re-login retry on 401."""
    opener, base = _get_session()
    status, resp = sap_sl_request_json(opener, method, base + path, body=body)
    if status == 401:
        _login()
        opener, base = _get_session()
        status, resp = sap_sl_request_json(opener, method, base + path, body=body)
    if resp is None:
        resp = {}
    return status, resp


def find_invoice_by_order_id(amazon_order_id: str) -> dict | None:
    """Live AR Invoice with NumAtCard == amazon_order_id, or None.
    Raises SapLookupError on failure — callers must NOT post then."""
    escaped = amazon_order_id.replace("'", "''")
    path = quote(
        f"Invoices?$filter=NumAtCard eq '{escaped}' and CancelStatus eq 'csNo'"
        "&$select=DocEntry,DocNum&$top=1",
        safe="/?$&=',",
    )
    status, body = _request("GET", path)
    if status != 200:
        raise SapLookupError(
            f"Duplicate check failed for {amazon_order_id}: HTTP {status}: {extract_sap_error(body)}"
        )
    rows = body.get("value") or []
    if not rows:
        return None
    return {"doc_entry": str(rows[0]["DocEntry"]), "doc_num": str(rows[0].get("DocNum") or "")}


def get_invoice(doc_entry: str) -> dict:
    """Full Invoice object — used to store the real SAP response for an
    order recovered via find_invoice_by_order_id."""
    status, body = _request("GET", f"Invoices({doc_entry})")
    if status != 200:
        raise SapLookupError(f"Invoice fetch failed for DocEntry={doc_entry}: HTTP {status}")
    return body


def post_invoice(payload: dict) -> dict:
    """POST an AR Invoice. Returns {"doc_entry": str, "doc_num": str, "response": dict}.

    U_* UDFs are stripped from the create payload and PATCHed afterwards
    (inline UDFs are silently dropped by SAP), then the invoice is
    re-fetched so ``response`` reflects the patched values.
    """
    udf_fields = {k: v for k, v in payload.items() if k.startswith("U_")}
    create_payload = {k: v for k, v in payload.items() if k not in udf_fields}

    status, body = _request("POST", "Invoices", body=create_payload)
    if status not in (200, 201) or not body.get("DocEntry"):
        raise SapPostError(
            f"SAP AR Invoice post failed (HTTP {status}): {extract_sap_error(body)}",
            response_body=body,
        )

    doc_entry = body["DocEntry"]
    if udf_fields:
        patch_status, patch_body = _request("PATCH", f"Invoices({doc_entry})", body=udf_fields)
        if patch_status not in (200, 204):
            raise SapPostError(
                f"SAP AR Invoice {doc_entry} created but UDF PATCH failed "
                f"({patch_status}): {extract_sap_error(patch_body)}",
                response_body=patch_body,
            )
        body = get_invoice(doc_entry)

    return {"doc_entry": str(doc_entry), "doc_num": str(body.get("DocNum") or ""), "response": body}


def find_credit_notes_by_order(amazon_order_id: str) -> list[dict]:
    """All non-cancelled Credit Memos with NumAtCard == amazon_order_id.

    Unlike Invoices (one per order, so NumAtCard alone is a unique key), an
    order can legitimately get more than one Credit Memo (one per RELEASED
    refund transaction) — NumAtCard alone can't disambiguate, so this
    returns every candidate; the caller disambiguates by
    comparing DocumentLines content against the specific refund it's
    trying to post (see post_credit_notes_to_sap._find_existing_credit_note)."""
    escaped = amazon_order_id.replace("'", "''")
    path = quote(
        f"CreditNotes?$filter=NumAtCard eq '{escaped}' and CancelStatus eq 'csNo'"
        "&$select=DocEntry,DocNum",
        safe="/?$&=',",
    )
    status, body = _request("GET", path)
    if status != 200:
        raise SapLookupError(
            f"Credit Memo lookup failed for {amazon_order_id}: HTTP {status}: {extract_sap_error(body)}"
        )
    rows = body.get("value") or []
    return [{"doc_entry": str(r["DocEntry"]), "doc_num": str(r.get("DocNum") or "")} for r in rows]


def get_credit_note(doc_entry: str) -> dict:
    """Full Credit Memo object — used to store the real SAP response for a
    refund recovered via find_credit_notes_by_order."""
    status, body = _request("GET", f"CreditNotes({doc_entry})")
    if status != 200:
        raise SapLookupError(f"Credit Memo fetch failed for DocEntry={doc_entry}: HTTP {status}")
    return body


def post_credit_note(payload: dict) -> dict:
    """POST an AR Credit Memo. Same create-then-PATCH-UDFs shape as
    post_invoice (see that docstring)."""
    udf_fields = {k: v for k, v in payload.items() if k.startswith("U_")}
    create_payload = {k: v for k, v in payload.items() if k not in udf_fields}

    status, body = _request("POST", "CreditNotes", body=create_payload)
    if status not in (200, 201) or not body.get("DocEntry"):
        raise SapPostError(
            f"SAP Credit Memo post failed (HTTP {status}): {extract_sap_error(body)}",
            response_body=body,
        )

    doc_entry = body["DocEntry"]
    if udf_fields:
        patch_status, patch_body = _request("PATCH", f"CreditNotes({doc_entry})", body=udf_fields)
        if patch_status not in (200, 204):
            raise SapPostError(
                f"SAP Credit Memo {doc_entry} created but UDF PATCH failed "
                f"({patch_status}): {extract_sap_error(patch_body)}",
                response_body=patch_body,
            )
        body = get_credit_note(doc_entry)

    return {"doc_entry": str(doc_entry), "doc_num": str(body.get("DocNum") or ""), "response": body}
