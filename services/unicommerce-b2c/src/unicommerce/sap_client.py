"""SAP B1 Service Layer access for Unicommerce AR Invoices + Credit Notes.

Built on khanal_shared.sap_sl (SSM-cached B1 session with lock, profile
"india_test" -- India entity, same physical SAP host as delivery-api,
but a separate SSM param family/session cache while this service is
unverified -- see serverless.yml's environment block). Ported from
khanal_tech_integrations/unicommerce/client/sap.py's SAP-facing methods
and workers/invoice.py's series/FY resolution.

Duplicate guard: primary lookup is a direct HANA query (hana_access.
find_invoice_by_order_id / find_credit_note_by_base_invoice) against the
line-level U_Order UDF / standard BaseEntry link -- confirmed live that
the header UDF U_UCOrderID this was originally built around doesn't
exist on this SAP install's Invoices entity type at all (SAP SL rejects
it outright), so it can never work via Service Layer here. The SL-based
$filter lookups below are kept only as a fallback for when HANA itself
is unreachable, matching the graceful-degrade behavior of the Frappe
source this was ported from (its primary lookup also silently falls
through on failure, rather than aborting the whole duplicate check).
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

from unicommerce import hana_access

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
    status, resp_body = sap_sl_request_json(opener, method, url, body=body)
    if status == 401:
        logger.info("SAP session stale (401); re-logging in")
        _invalidate_cached_session()
        opener, base = build_opener_and_base_via_ssm()
        return sap_sl_request_json(opener, method, f"{base}/{path}", body=body)
    return status, resp_body


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
    """Duplicate guard: live (non-cancelled) AR Invoice already posted for
    this UC order. Primary check is hana_access.find_invoice_by_order_id
    (see its docstring for why -- SAP SL's U_UCOrderID filter doesn't work
    on this install). Only falls back to the SL-based checks below if
    HANA itself is unreachable; if every path fails, raises
    SapLookupError -- callers must NOT post then (a swallowed exception ->
    None -> "not posted yet" is how duplicate invoices happen)."""
    key = str(uc_order_id or "").strip()
    if not key:
        return None

    try:
        return hana_access.find_invoice_by_order_id(key)
    except Exception as exc:
        logger.warning("find_invoice_by_uc_order_id: HANA lookup failed for %s (%s) -- falling back to SL", key, exc)

    escaped = key.replace("'", "''")
    last_exc: Exception | None = None

    # Comments is selected on both fallback queries (not just filtered on)
    # so the match can be verified below -- the $filter clauses alone
    # aren't trustworthy proof of a real match: U_UCOrderID is documented
    # above as not working on this install (unclear what it actually
    # matches when "rejected outright" doesn't happen), and a permissive
    # OData server could in principle ignore/mishandle a filter it
    # doesn't like rather than erroring. Found via a real corruption
    # incident (2026-08-19): 62% of a sample of 'posted' orders had a
    # sap_ar_invoice_docentry belonging to a provably unrelated order.
    try:
        status, body = _request(
            "GET",
            quote(
                f"Invoices?$filter=U_UCOrderID eq '{escaped}'"
                "&$select=DocEntry,DocNum,DocTaxAmount,DocTotal,Comments&$orderby=DocEntry desc&$top=5",
                safe="/?$&=',",
            ),
        )
        if status == 200 and isinstance(body, dict):
            rows = body.get("value", [])
            if rows and key in str(rows[0].get("Comments") or ""):
                return _row_to_result(rows[0])
    except Exception as exc:
        last_exc = exc

    try:
        status, body = _request(
            "GET",
            quote(
                f"Invoices?$filter=contains(Comments,'{escaped}')"
                "&$select=DocEntry,DocNum,DocTaxAmount,DocTotal,Comments&$orderby=DocEntry desc&$top=20",
                safe="/?$&=',",
            ),
        )
        if status == 200 and isinstance(body, dict):
            rows = body.get("value", [])
            for row in rows:
                if key in str(row.get("Comments") or ""):
                    return _row_to_result(row)
            return None
        last_exc = last_exc or RuntimeError(f"HTTP {status}")
    except Exception as exc:
        last_exc = exc

    raise SapLookupError(f"Duplicate check failed for {key} on every path (HANA + SL): {last_exc}")


def find_invoice_by_num_at_card(num_at_card: str) -> dict | None:
    """AMAZON_FLEX-only duplicate guard: NumAtCard is a native SAP header
    field (unlike U_UCOrderID, which SAP SL rejects on this install -- see
    find_invoice_by_uc_order_id's docstring), so this queries it directly
    via Service Layer instead of going through HANA. Ported from
    amazon-us-b2c's sap_client._find_by_num_at_card, which already proves
    this pattern works against this same SAP install. Other channels keep
    using find_invoice_by_uc_order_id/HANA -- NumAtCard is unset on their
    invoices, so this lookup would just never match for them."""
    key = str(num_at_card or "").strip()
    if not key:
        return None
    escaped = key.replace("'", "''")
    # DocTaxAmount deliberately NOT in $select -- confirmed 2026-08-20 the
    # SAP TEST company rejects it outright ("Property 'DocTaxAmount' of
    # 'Document' is invalid", HTTP 400), unlike prod's schema. It's only
    # used for reconciliation bookkeeping downstream, not correctness --
    # _row_to_result already treats a missing value as 0.
    path = quote(
        f"Invoices?$filter=NumAtCard eq '{escaped}' and CancelStatus eq 'csNo'"
        "&$select=DocEntry,DocNum&$top=1",
        safe="/?$&=',",
    )
    try:
        status, body = _request("GET", path)
    except Exception as e:
        raise SapLookupError(f"NumAtCard duplicate check failed for {key}: {e}") from e
    if status != 200 or not isinstance(body, dict):
        raise SapLookupError(f"NumAtCard duplicate check failed for {key}: HTTP {status}: {str(body)[:500]}")
    rows = body.get("value", [])
    return _row_to_result(rows[0]) if rows else None


def _row_to_result(row: dict) -> dict:
    return {
        "doc_entry": int(row["DocEntry"]),
        "doc_num": int(row.get("DocNum") or 0),
        "doc_tax_amount": float(row.get("DocTaxAmount") or 0),
        "comments": str(row.get("Comments") or ""),
    }


def set_invoice_num_at_card(doc_entry: int, value: str) -> None:
    """One-off backfill: PATCH the native NumAtCard header field onto an
    already-posted invoice. Used to migrate historical AR Invoices (posted
    before NumAtCard-based dedup existed) so they become matchable by the
    same $filter=NumAtCard lookup pattern amazon-us-b2c uses -- see
    scripts/backfill_numatcard.py. Not part of the normal posting path
    (post_invoice already sets NumAtCard at creation time going forward)."""
    try:
        status, body = _request("PATCH", f"Invoices({int(doc_entry)})", body={"NumAtCard": str(value)})
    except Exception as e:
        raise SapPostError(f"NumAtCard patch failed for DocEntry={doc_entry}: {e}") from e
    if status not in (200, 204):
        raise SapPostError(f"NumAtCard patch failed for DocEntry={doc_entry}: HTTP {status}: {str(body)[:500]}")


_product_tree_cache: dict[str, list[dict]] = {}


def get_product_tree_children(parent_item_code: str) -> list[dict]:
    """[{"item_code": str, "quantity": float}, ...] for a CFG (Sales BOM)
    parent's child items. Ported from khanal_tech_integrations MTR
    script's _get_product_tree_children -- tries 3 ProductTrees endpoint
    shapes because SAP Service Layer versions differ in whether
    ProductTreeLines comes back inline, via $expand, or needs a separate
    sub-resource fetch. Cached per Lambda container (bundle composition
    doesn't change mid-run). Raises ValueError if every variant fails or
    returns no children -- never guesses at bundle contents."""
    parent = (parent_item_code or "").strip()
    if not parent:
        return []
    if parent in _product_tree_cache:
        return _product_tree_cache[parent]

    escaped = parent.replace("'", "''")
    last_error: str | None = None

    for endpoint in (
        f"ProductTrees('{escaped}')",
        f"ProductTrees('{escaped}')?$expand=ProductTreeLines",
        f"ProductTrees?$filter=TreeCode eq '{escaped}'&$expand=ProductTreeLines",
    ):
        try:
            status, body = _request("GET", quote(endpoint, safe="/?$&=',()"))
            if status != 200 or not isinstance(body, dict):
                last_error = f"HTTP {status}: {str(body)[:200]}"
                continue
            data = body
            if "value" in data:
                trees = data.get("value") or []
                data = trees[0] if trees else {}
            lines = data.get("ProductTreeLines") or []
            if isinstance(lines, dict):
                lines = lines.get("value") or []
            if not lines:
                lines_status, lines_body = _request("GET", f"ProductTrees('{quote(escaped, safe='')}')/ProductTreeLines")
                if lines_status == 200 and isinstance(lines_body, dict):
                    lines = lines_body.get("value") or (lines_body if isinstance(lines_body, list) else [])

            children = [
                {"item_code": str(line.get("ItemCode") or "").strip(), "quantity": float(line.get("Quantity") or 0)}
                for line in lines
                if str(line.get("ItemCode") or "").strip() and float(line.get("Quantity") or 0)
            ]
            if children:
                _product_tree_cache[parent] = children
                return children
            last_error = f"ProductTree {parent} has no child lines"
        except Exception as e:
            last_error = str(e)

    raise ValueError(f"Unable to resolve Sales BOM children for CFG parent {parent}. Last error: {last_error}")


def get_invoice_by_doc_entry(doc_entry: int) -> dict:
    try:
        status, body = _request("GET", f"Invoices({int(doc_entry)})")
    except Exception as e:
        raise SapLookupError(f"Invoice fetch failed for DocEntry={doc_entry}: {e}") from e
    if status != 200 or not isinstance(body, dict):
        raise SapLookupError(f"Invoice fetch failed for DocEntry={doc_entry}: HTTP {status}")
    return body


# ── Credit notes ──────────────────────────────────────────────────────
# Ported from khanal_tech_integrations/unicommerce/client/sap.py's
# CreditNotes-facing methods and workers/credit_note.py's series fallback.

def post_credit_note(payload: dict) -> dict:
    """Returns {"doc_entry": int, "doc_num": int, "response": dict}."""
    try:
        status, body = _request("POST", "CreditNotes", body=payload)
    except RuntimeError as e:
        raise SapPostError(f"SAP Credit Note failed: {_sap_error_text(str(e))}") from e
    if status not in (200, 201) or not isinstance(body, dict) or not body.get("DocEntry"):
        raise SapPostError(f"SAP Credit Note failed: HTTP {status}: {str(body)[:500]}")
    return {"doc_entry": int(body["DocEntry"]), "doc_num": int(body.get("DocNum") or 0), "response": body}


def get_credit_note_by_doc_entry(doc_entry: int) -> dict:
    try:
        status, body = _request("GET", f"CreditNotes({int(doc_entry)})")
    except Exception as e:
        raise SapLookupError(f"Credit note fetch failed for DocEntry={doc_entry}: {e}") from e
    if status != 200 or not isinstance(body, dict):
        raise SapLookupError(f"Credit note fetch failed for DocEntry={doc_entry}: HTTP {status}")
    return body


def find_credit_note_by_return_id(uc_return_id: str, base_doc_entry: int | str | None = None) -> dict | None:
    """Duplicate guard: SAP Credit Note already posted for this return.
    Primary check is hana_access.find_credit_note_by_base_invoice, keyed
    on base_doc_entry (the parent AR Invoice's DocEntry) via RIN1's
    standard BaseEntry/BaseType link -- no custom field needed, and it
    works where SAP SL's U_UCReturnID filter can't (ORIN has no such UDF
    on this install at all; see find_invoice_by_uc_order_id's docstring
    for the equivalent invoice-side history). Falls back to the SL-based
    checks below only if HANA is unreachable or base_doc_entry wasn't
    supplied; if every path fails, raises SapLookupError -- callers must
    NOT post then (a swallowed exception -> None -> "not posted yet" is
    how duplicate credit notes happen)."""
    key = str(uc_return_id or "").strip()
    if not key:
        return None

    if base_doc_entry:
        try:
            return hana_access.find_credit_note_by_base_invoice(base_doc_entry)
        except Exception as exc:
            logger.warning("find_credit_note_by_return_id: HANA lookup failed for %s (%s) -- falling back to SL", key, exc)

    escaped = key.replace("'", "''")
    last_exc: Exception | None = None

    # Comments selected (not just filtered on) so the match can be
    # verified -- same defense-in-depth as find_invoice_by_uc_order_id,
    # see its comment for why the $filter clause alone isn't trusted.
    try:
        status, body = _request(
            "GET",
            quote(
                f"CreditNotes?$filter=U_UCReturnID eq '{escaped}'"
                "&$select=DocEntry,DocNum,DocTaxAmount,DocTotal,Comments&$orderby=DocEntry desc&$top=5",
                safe="/?$&=',",
            ),
        )
        if status == 200 and isinstance(body, dict):
            rows = body.get("value", [])
            if rows and key in str(rows[0].get("Comments") or ""):
                return _row_to_result(rows[0])
    except Exception as exc:
        last_exc = exc

    try:
        status, body = _request(
            "GET",
            quote(
                f"CreditNotes?$filter=contains(Comments,'{escaped}')"
                "&$select=DocEntry,DocNum,DocTaxAmount,DocTotal,Comments&$orderby=DocEntry desc&$top=20",
                safe="/?$&=',",
            ),
        )
        if status == 200 and isinstance(body, dict):
            rows = body.get("value", [])
            for row in rows:
                if key in str(row.get("Comments") or ""):
                    return _row_to_result(row)
            return None
        last_exc = last_exc or RuntimeError(f"HTTP {status}")
    except Exception as exc:
        last_exc = exc

    raise SapLookupError(f"Duplicate check failed for {key} on every path (HANA + SL): {last_exc}")


def _pick_active_credit_note_series() -> int | None:
    filters = [
        "ObjectCode eq 14 and Locked eq 'tNO'",
        "ObjectCode eq '14' and Locked eq 'tNO'",
        "ObjectCode eq 14",
        "ObjectCode eq '14'",
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


def post_credit_note_with_series_fallback(payload: dict) -> dict:
    """Post with numbering-series fallback: configured payload series ->
    active SAP CN series -> no series."""
    try:
        return post_credit_note(payload)
    except SapPostError as e:
        if "numbering series" not in str(e).lower():
            raise

    active = _pick_active_credit_note_series()
    if active:
        payload_with_fallback = dict(payload)
        payload_with_fallback["Series"] = int(active)
        try:
            return post_credit_note(payload_with_fallback)
        except SapPostError as e:
            if "numbering series" not in str(e).lower():
                raise

    payload_without_series = dict(payload)
    payload_without_series.pop("Series", None)
    return post_credit_note(payload_without_series)


# ── Series / financial-year resolution ──────────────────────────────────
# Ported from workers/invoice.py's _pick_active_invoice_series /
# _validate_active_series / _pick_series_for_doc_date /
# _post_invoice_with_series_fallback. The FY guard exists because of a
# real production incident (comment in the Frappe source: re-posting into
# a stale FY series nearly double-booked 148 cancelled March orders).


def _current_fy_period_indicator() -> str:
    """SAP's PeriodIndicator format, e.g. "FY2627" for the year starting
    April 2026 -- confirmed against live NNM1 rows (india_test), which
    consistently pair PeriodIndicator "FYxxyy" with series named
    "...xx" (the FY-start year's suffix), not "...yy"."""
    today = datetime.today()
    fy_start = today.year if today.month >= 4 else today.year - 1
    return f"FY{str(fy_start)[-2:]}{str(fy_start + 1)[-2:]}"


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


def validate_active_series(settings) -> tuple[bool, str]:
    """Abort-the-run guard: confirms every SAP series actually referenced
    by Unicommerce Settings (ar_invoice_series_map, its ar_invoice_series
    fallback, and credit_note_series) belongs to the current financial
    year, before any posting starts.

    Previously checked SAP's company-wide "default" AR Invoice series
    instead -- that's a different, unrelated series (confirmed live: the
    default resolved to a generic "AR2026", not any of the per-channel
    series orders actually post under) and the lookup itself queried a
    "Series" OData entity that doesn't exist in this SAP B1 Service Layer
    version (always 400 "Unrecognized resource path", silently swallowed
    -- this guard has never actually validated anything, including in
    the Frappe source it was ported from). Checking the settings' own
    series IDs via SeriesService_GetSeries (POST, not a raw entity query)
    directly protects against the real failure mode: forgetting to bump
    ar_invoice_series_map/credit_note_series at FY rollover -- the exact
    incident (148 cancelled March orders) this guard exists to prevent.
    """
    current_period = _current_fy_period_indicator()

    series_ids: set[int] = set()
    for value in (settings.ar_invoice_series_map or {}).values():
        if value:
            series_ids.add(int(value))
    if settings.ar_invoice_series:
        series_ids.add(int(settings.ar_invoice_series))
    if settings.credit_note_series:
        series_ids.add(int(settings.credit_note_series))

    if not series_ids:
        return True, "No series configured in settings -- nothing to validate"

    stale: list[str] = []
    unresolved: list[str] = []
    for series_id in sorted(series_ids):
        try:
            status, body = _request("POST", "SeriesService_GetSeries", {"SeriesParams": {"Series": series_id}})
        except Exception as exc:
            unresolved.append(f"{series_id} (lookup failed: {exc})")
            continue
        if status != 200 or not isinstance(body, dict):
            unresolved.append(f"{series_id} (HTTP {status})")
            continue
        period = str(body.get("PeriodIndicator") or "").strip()
        name = str(body.get("Name") or "").strip() or "unnamed"
        if period and period != current_period:
            stale.append(f"{series_id} ({name}, {period})")

    if stale:
        return False, (
            f"POSTING BLOCKED -- these configured series are not in the current financial year "
            f"({current_period}): {'; '.join(stale)}. Update ar_invoice_series_map / ar_invoice_series / "
            f"credit_note_series in Unicommerce Settings and retry."
        )
    if unresolved:
        logger.warning("validate_active_series: could not resolve %s -- proceeding", "; ".join(unresolved))
        return True, f"{len(unresolved)} series could not be resolved ({'; '.join(unresolved)}) -- proceeding without full validation"
    return True, f"All {len(series_ids)} configured series are valid for {current_period}"


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
