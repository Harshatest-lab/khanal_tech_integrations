"""SAP B1 Service Layer access for Unicommerce AR Invoices.

Built on khanal_shared.sap_sl (SSM-cached B1 session with lock, default
/sap/* profile -- India entity, same as delivery-api and, once posting
lands here, this service). Ported from
khanal_tech_integrations/unicommerce/client/sap.py's SAP-facing methods
and workers/invoice.py's series/FY resolution.

Duplicate guard uses U_UCOrderID (a dedicated UDF), NOT NumAtCard like
amazon-us-b2c/B2C_Invoice's SAP client -- Unicommerce's Frappe source
posts U_UCOrderID = staging_doc.name specifically so this lookup works
even when Comments (the human-readable "Unicommerce Order: X" text) gets
edited or truncated.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from khanal_shared.sap_sl import build_opener_and_base_via_ssm, sap_sl_request_json
from khanal_shared.sap_sl.ssm_session import param_b1_session

logger = logging.getLogger(__name__)


class SapPostError(Exception):
    """SAP B1 rejected a document. Message carries SAP's error text."""


class SapLookupError(Exception):
    """A duplicate-guard or document lookup failed. NEVER treat as 'not found'."""


class SeriesGuardError(Exception):
    """Active SAP invoice series belongs to a stale financial year -- posting aborted."""


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


def _request(method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
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


def _sap_error_text(raw: str) -> str:
    try:
        json_part = raw[raw.index("{") :]
        msg = json.loads(json_part).get("error", {}).get("message", {})
        if isinstance(msg, dict):
            return msg.get("value") or raw[:500]
        return str(msg) or raw[:500]
    except Exception:
        return raw[:500]


def post_invoice(payload: dict) -> dict:
    """Returns {"doc_entry": int, "doc_num": int, "response": dict}."""
    try:
        status, body = _request("POST", "Invoices", body=payload)
    except RuntimeError as e:
        raise SapPostError(f"SAP AR Invoice failed: {_sap_error_text(str(e))}") from e
    if status not in (200, 201) or not isinstance(body, dict) or not body.get("DocEntry"):
        raise SapPostError(f"SAP AR Invoice failed: HTTP {status}: {str(body)[:500]}")
    return {"doc_entry": int(body["DocEntry"]), "doc_num": int(body.get("DocNum") or 0), "response": body}


def find_invoice_by_uc_order_id(uc_order_id: str) -> dict | None:
    """Duplicate guard: live (non-cancelled) AR Invoice with U_UCOrderID ==
    uc_order_id, falling back to a Comments-contains search for
    manual/legacy posts that predate the UDF. Raises SapLookupError on any
    failure -- callers must NOT post then (a swallowed exception -> None
    -> "not posted yet" is how duplicate invoices happen)."""
    key = str(uc_order_id or "").strip()
    if not key:
        return None
    escaped = key.replace("'", "''")

    try:
        status, body = _request(
            "GET",
            quote(
                f"Invoices?$filter=U_UCOrderID eq '{escaped}'"
                "&$select=DocEntry,DocNum,DocTaxAmount,DocTotal&$orderby=DocEntry desc&$top=5",
                safe="/?$&=',",
            ),
        )
    except Exception as e:
        raise SapLookupError(f"Duplicate check (U_UCOrderID) failed for {key}: {e}") from e
    if status != 200 or not isinstance(body, dict):
        raise SapLookupError(f"Duplicate check (U_UCOrderID) failed for {key}: HTTP {status}")
    rows = body.get("value", [])
    if rows:
        return _row_to_result(rows[0])

    try:
        status, body = _request(
            "GET",
            quote(
                f"Invoices?$filter=contains(Comments,'{escaped}')"
                "&$select=DocEntry,DocNum,DocTaxAmount,DocTotal&$orderby=DocEntry desc&$top=20",
                safe="/?$&=',",
            ),
        )
    except Exception as e:
        raise SapLookupError(f"Duplicate check (Comments fallback) failed for {key}: {e}") from e
    if status != 200 or not isinstance(body, dict):
        raise SapLookupError(f"Duplicate check (Comments fallback) failed for {key}: HTTP {status}")
    rows = body.get("value", [])
    return _row_to_result(rows[0]) if rows else None


def _row_to_result(row: dict) -> dict:
    return {
        "doc_entry": int(row["DocEntry"]),
        "doc_num": int(row.get("DocNum") or 0),
        "doc_tax_amount": float(row.get("DocTaxAmount") or 0),
    }


def get_invoice_by_doc_entry(doc_entry: int) -> dict:
    try:
        status, body = _request("GET", f"Invoices({int(doc_entry)})")
    except Exception as e:
        raise SapLookupError(f"Invoice fetch failed for DocEntry={doc_entry}: {e}") from e
    if status != 200 or not isinstance(body, dict):
        raise SapLookupError(f"Invoice fetch failed for DocEntry={doc_entry}: HTTP {status}")
    return body


# ── Series / financial-year resolution ──────────────────────────────────
# Ported from workers/invoice.py's _pick_active_invoice_series /
# _validate_active_series / _pick_series_for_doc_date /
# _post_invoice_with_series_fallback. The FY guard exists because of a
# real production incident (comment in the Frappe source: re-posting into
# a stale FY series nearly double-booked 148 cancelled March orders).

_KNOWN_SERIES_PREFIXES = {"KAB2C", "TNB2C", "MHB2C"}


def _current_fy_end_suffix() -> str:
    today = datetime.today()
    fy_start = today.year if today.month >= 4 else today.year - 1
    return str(fy_start + 1)[-2:]


def _pick_active_invoice_series() -> int | None:
    filters = [
        "ObjectCode eq 13 and Locked eq 'tNO'",
        "ObjectCode eq '13' and Locked eq 'tNO'",
        "ObjectCode eq 13",
        "ObjectCode eq '13'",
    ]
    for expr in filters:
        try:
            status, body = _request(
                "GET",
                quote(
                    f"Series?$filter={expr}&$select=Series,ObjectCode,Locked,DefaultSeries,NextNumber&$top=50",
                    safe="/?$&=',",
                ),
            )
            if status != 200 or not isinstance(body, dict):
                continue
            rows = body.get("value") or []
            if not rows:
                continue
            rows.sort(
                key=lambda r: (
                    0 if str(r.get("DefaultSeries") or "").strip().upper() in ("TYES", "Y", "TRUE", "1") else 1,
                    -(int(r.get("NextNumber") or 0)),
                )
            )
            return int(rows[0]["Series"])
        except Exception:
            continue
    return None


def validate_active_series() -> tuple[bool, str]:
    """Abort-the-run guard: confirms the active default AR Invoice series
    belongs to the current financial year before any posting starts."""
    curr_suffix = _current_fy_end_suffix()
    prev_suffix = str(int(curr_suffix) - 1).zfill(2)
    good = {f"{p}{curr_suffix}" for p in _KNOWN_SERIES_PREFIXES}
    old = {f"{p}{prev_suffix}" for p in _KNOWN_SERIES_PREFIXES}

    active_series_id = _pick_active_invoice_series()
    if not active_series_id:
        logger.warning("validate_active_series: could not resolve active series ID -- proceeding")
        return True, "Series ID could not be resolved -- proceeding without validation"

    try:
        status, body = _request("POST", "SeriesService_GetSeries", {"SeriesParams": {"Series": int(active_series_id)}})
        series_name = str(body.get("Name") or "").strip() if isinstance(body, dict) else ""
    except Exception as exc:
        logger.warning("validate_active_series: could not fetch series name: %s -- proceeding", exc)
        return True, f"Series name lookup failed ({exc}) -- proceeding without validation"

    if series_name in good:
        return True, f"Active series '{series_name}' is valid for the current financial year"
    if series_name in old:
        return False, (
            f"POSTING BLOCKED -- active SAP AR Invoice series is '{series_name}' (previous financial year). "
            f"Update the default series to one of {sorted(good)} in SAP B1 and retry."
        )
    logger.warning("validate_active_series: unrecognised series '%s' -- proceeding", series_name)
    return True, f"Series '{series_name}' is not in the known list -- proceeding"


def _fy_period_indicator_for_doc_date(doc_date: str | None) -> str | None:
    raw = str(doc_date or "").strip()[:10]
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d")
    except Exception:
        return None
    fy_start_year = dt.year if dt.month >= 4 else dt.year - 1
    return f"FY{str(fy_start_year)[-2:]}{str(fy_start_year + 1)[-2:]}"


def _match_series_prefix(name: str) -> str:
    raw = str(name or "").strip().upper()
    return re.sub(r"\d{2}$", "", raw) if raw else ""


def pick_series_for_doc_date(configured_series: int | None, doc_date: str | None, series_cache: dict) -> int | None:
    """Resolve a period-correct SAP series for the invoice's DocDate.
    series_cache (shared across a posting run) avoids repeated
    SeriesService calls when every order in the run shares a fiscal
    period, the common case."""
    if not configured_series:
        return None
    target_period = _fy_period_indicator_for_doc_date(doc_date)
    if not target_period:
        return int(configured_series)

    cache_key = (int(configured_series), target_period)
    if cache_key in series_cache:
        return series_cache[cache_key]

    resolved = int(configured_series)
    try:
        _st, current = _request("POST", "SeriesService_GetSeries", {"SeriesParams": {"Series": int(configured_series)}})
        current = current if isinstance(current, dict) else {}
        current_period = str(current.get("PeriodIndicator") or "").strip()
        if current_period == target_period:
            resolved = int(configured_series)
        else:
            doc_subtype = str(current.get("DocumentSubType") or "--").strip() or "--"
            current_prefix = _match_series_prefix(str(current.get("Name") or ""))
            desired_suffix = target_period[-2:]
            _st2, all_series = _request(
                "POST", "SeriesService_GetDocumentSeries", {"DocumentSeriesParams": {"Document": "13", "DocumentSubType": doc_subtype}}
            )
            all_series = all_series if isinstance(all_series, dict) else {}
            candidates = [
                row
                for row in (all_series.get("value") or [])
                if str(row.get("Locked") or "").strip().upper() != "TYES"
                and str(row.get("PeriodIndicator") or "").strip() == target_period
            ]
            if candidates:
                if current_prefix:
                    family = [row for row in candidates if _match_series_prefix(str(row.get("Name") or "")) == current_prefix]
                    if family:
                        exact = [row for row in family if str(row.get("Name") or "").strip().upper().endswith(desired_suffix)]
                        resolved = int((exact or family)[0]["Series"])
                    else:
                        candidates.sort(key=lambda r: int(r.get("NextNumber") or 0), reverse=True)
                        resolved = int(candidates[0]["Series"])
                else:
                    candidates.sort(key=lambda r: int(r.get("NextNumber") or 0), reverse=True)
                    resolved = int(candidates[0]["Series"])
    except Exception:
        resolved = int(configured_series)

    series_cache[cache_key] = resolved
    return resolved


def post_invoice_with_series_fallback(payload: dict, series_cache: dict) -> tuple[dict, dict]:
    """Post with resilient numbering-series fallback: configured payload
    series -> period-correct series -> active SAP series -> no series.
    Returns (sap_result, payload_actually_posted)."""
    payload_to_post = dict(payload)
    resolved_series = pick_series_for_doc_date(payload_to_post.get("Series"), payload_to_post.get("DocDate"), series_cache)
    if resolved_series:
        payload_to_post["Series"] = int(resolved_series)
    try:
        result = post_invoice(payload_to_post)
        return result, payload_to_post
    except SapPostError as e:
        if "numbering series" not in str(e).lower():
            raise

    fallback_series = _pick_active_invoice_series()
    if fallback_series:
        payload_with_fallback = dict(payload)
        payload_with_fallback["Series"] = int(fallback_series)
        try:
            result = post_invoice(payload_with_fallback)
            return result, payload_with_fallback
        except SapPostError as e:
            if "numbering series" not in str(e).lower():
                raise

    payload_without_series = dict(payload)
    payload_without_series.pop("Series", None)
    result = post_invoice(payload_without_series)
    return result, payload_without_series
