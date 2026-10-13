"""One-off: cancel + repost AR Invoices that were posted with Karnataka (KA)
tax codes although their channel ships from another state (HR / MH / TN).

Background: before PR #188 (deployed 2026-10-08) tax.get_tax_code only read
the flat KA tax_code_map, so FBA / Flipkart-FA channels with a non-KA origin
carried KACS*/KAIG* codes. Finance asked for the October (open period)
invoices to be cancelled and reposted with the correct state codes; earlier
months are reconciled and must not be touched.

Each correction is a real, live SAP action (Invoices/Cancel), so this is
deliberately conservative:
  * dry-run by default, and `limit` defaults to 1 (a pilot) even when applying;
  * a full set of live-SAP checks runs BEFORE anything is cancelled -- the
    invoice must still be open, unpaid, uncancelled, unlinked to any credit
    note, dated in the allowed month, and carry only KA codes;
  * the replacement invoice is rebuilt offline first and must come out with
    the origin state's codes and the same tax total as the live invoice --
    otherwise nothing is cancelled;
  * the cancelling document must land in the required series (KAB2C26 =
    Series 500; SAP does this by default for API cancellations) -- verified
    after every cancel, and the batch stops if it ever differs;
  * cancel -> local reset -> repost run back-to-back for one order at a time
    (never left to the cron), so freed stock cannot be taken by another order
    in between, and the batch stops at the first anomaly.
"""

from __future__ import annotations

import json
import logging

from unicommerce import geo, hana_access, post_invoices, sap_client, sap_payload, store
from unicommerce.settings import get_settings

logger = logging.getLogger(__name__)

DEFAULT_CANCEL_SERIES = 500  # KAB2C26 (A/R Invoice series, FY2026-27)
TOTAL_TOLERANCE = 0.06  # SAP rounds each line to 2dp; matches the +-6 paise seen in the offline rehearsal

# Statuses that mean "this order was handled as intended".
_OK_APPLY = {"corrected"}
_OK_DRY = {"dry_run_would_correct"}


def _codes(doc: dict) -> set[str]:
    return {str(line.get("TaxCode") or "").strip() for line in (doc.get("DocumentLines") or []) if str(line.get("TaxCode") or "").strip()}


def _f(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _get_row(conn, uniware_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT uniware_id, channel_name, pipeline_status, sap_ar_invoice_docentry, order_date::text, order_json "
            "FROM unicommerce_orders WHERE uniware_id = %s",
            (uniware_id,),
        )
        r = cur.fetchone()
    if not r:
        return None
    return {
        "uniware_id": r[0], "channel_name": r[1], "pipeline_status": r[2],
        "doc_entry": r[3], "order_date": r[4], "order_json": r[5],
    }


def _rebuild(row: dict, live: dict, settings) -> tuple[set[str], float]:
    """Offline rebuild of the replacement invoice: (tax codes, total tax).
    batch_managed_cache is pre-filled with False so the rebuild makes no
    SAP/HANA batch lookups -- batches don't affect tax codes or tax."""
    raw = row["order_json"]
    order_json = raw if isinstance(raw, str) else json.dumps(raw or {})
    uc_data = json.loads(order_json or "{}")
    cache = {str(i.get("itemSku") or "").strip(): False for i in uc_data.get("saleOrderItems") or []}
    payload, tax = sap_payload.ar_invoice_payload(
        row["uniware_id"], row["channel_name"], order_json, uc_data, row["order_date"], settings,
        cache, {}, doc_date=str(live.get("DocDate") or "")[:10],
    )
    return {str(line.get("TaxCode")) for line in payload["DocumentLines"] if line.get("TaxCode")}, float(tax)


def correct_one(
    conn, uniware_id: str, doc_entry: int | str, doc_month: str, cancel_series: int = DEFAULT_CANCEL_SERIES, apply: bool = False,
) -> dict:
    base = {"uniware_id": uniware_id, "doc_entry": int(doc_entry)}

    def out(status: str, **extra) -> dict:
        return {**base, "status": status, **extra}

    row = _get_row(conn, uniware_id)
    if row is None:
        return out("skipped_not_found")
    if row["pipeline_status"] != "posted" or str(row["doc_entry"] or "") != str(int(doc_entry)):
        return out("skipped_local_state_mismatch", pipeline_status=row["pipeline_status"], local_doc_entry=row["doc_entry"])

    settings = get_settings(force_reload=True)
    origin = geo.resolve_origin_state_code(row["channel_name"], settings)
    if origin == "KA":
        return out("skipped_ka_origin_channel", channel=row["channel_name"])

    live = sap_client.get_invoice_by_doc_entry(int(doc_entry))
    if str(live.get("Cancelled")) != "tNO":
        return out("skipped_already_cancelled")
    if str(live.get("DocumentStatus")) != "bost_Open":
        return out("skipped_not_open", document_status=live.get("DocumentStatus"))
    if abs(_f(live.get("PaidToDate"))) > 0.005:
        return out("skipped_paid", paid_to_date=live.get("PaidToDate"))
    doc_date = str(live.get("DocDate") or "")[:10]
    if not doc_date.startswith(doc_month):
        return out("skipped_wrong_month", doc_date=doc_date, allowed=doc_month)
    if str(live.get("NumAtCard") or "") != uniware_id:
        return out("skipped_numatcard_mismatch", num_at_card=live.get("NumAtCard"))

    old_codes = _codes(live)
    if not old_codes:
        return out("skipped_no_tax_codes")
    if not all(c.startswith("KA") for c in old_codes):
        return out("skipped_not_all_ka", tax_codes=sorted(old_codes))

    if hana_access.find_credit_note_by_base_invoice(int(doc_entry)):
        return out("skipped_has_credit_note")

    try:
        new_codes, new_tax = _rebuild(row, live, settings)
    except Exception as e:  # nothing has been touched yet -- safe to bail
        return out("skipped_rebuild_failed", error=str(e)[:300])
    if not new_codes or not all(c.startswith(origin) for c in new_codes):
        return out("skipped_rebuild_not_state_coded", expected_prefix=origin, new_codes=sorted(new_codes))
    old_tax = _f(live.get("VatSum"))
    if abs(new_tax - old_tax) > TOTAL_TOLERANCE:
        return out("skipped_rebuild_tax_mismatch", old_tax=old_tax, new_tax=round(new_tax, 2))

    summary = {
        "channel": row["channel_name"], "origin": origin, "doc_date": doc_date, "old_doc_num": live.get("DocNum"),
        "old_series": live.get("Series"), "old_codes": sorted(old_codes), "new_codes": sorted(new_codes),
        "doc_total": _f(live.get("DocTotal")), "vat": old_tax,
    }
    if not apply:
        return out("dry_run_would_correct", **summary)

    # ---- point of no return: every guard above passed --------------------
    sap_client.cancel_invoice(int(doc_entry))
    cancel_doc = hana_access.find_cancellation_document(uniware_id, int(doc_entry))
    cancel_info = dict(cancel_doc) if cancel_doc else {}
    series_ok = bool(cancel_doc) and int(cancel_doc["series"]) == int(cancel_series)
    if not series_ok:
        logger.error(
            "tax_code_correction: invoice %s cancelled but cancelling doc series is %s (required %s) -- stopping batch",
            doc_entry, cancel_info.get("series"), cancel_series,
        )

    if not store.reset_posted_order_for_repost(conn, uniware_id, doc_entry):
        logger.error("tax_code_correction: %s cancelled but local reset for %s did not match -- manual follow-up", doc_entry, uniware_id)
        return out("cancelled_but_reset_failed", cancel_doc=cancel_info, **summary)

    result = post_invoices.post_one_order(conn, uniware_id)
    if result.get("status") != "posted":
        return out("cancelled_repost_not_posted", repost=result, cancel_doc=cancel_info, cancel_series_ok=series_ok, **summary)

    new_row = _get_row(conn, uniware_id)
    new_entry = int((new_row or {}).get("doc_entry") or 0)
    if not new_entry:
        return out("reposted_but_no_docentry", cancel_doc=cancel_info, cancel_series_ok=series_ok, **summary)
    new_live = sap_client.get_invoice_by_doc_entry(new_entry)
    verified = (
        all(c.startswith(origin) for c in _codes(new_live)) and bool(_codes(new_live))
        and abs(_f(new_live.get("DocTotal")) - summary["doc_total"]) <= TOTAL_TOLERANCE
        and abs(_f(new_live.get("VatSum")) - old_tax) <= TOTAL_TOLERANCE
        and str(new_live.get("DocDate"))[:10] == doc_date
    )
    details = {
        "new_doc_entry": new_entry, "new_doc_num": new_live.get("DocNum"), "new_series": new_live.get("Series"), "new_codes": sorted(_codes(new_live)),
        "new_doc_total": _f(new_live.get("DocTotal")), "new_vat": _f(new_live.get("VatSum")),
        "cancel_doc": cancel_info, "cancel_series_ok": series_ok,
    }
    if not verified:
        return out("reposted_verify_failed", **{**summary, **details})
    if not series_ok:
        return out("corrected_but_cancel_series_differs", **{**summary, **details})
    return out("corrected", **{**summary, **details})


def run(
    conn, corrections: list[dict], doc_month: str, cancel_series: int = DEFAULT_CANCEL_SERIES, apply: bool = False, limit: int = 1,
) -> dict:
    """Process at most `limit` corrections, sequentially; stop at the first
    anomaly (anything that is neither a clean dry-run/correction nor a
    harmless pre-check skip)."""
    ok = _OK_APPLY if apply else _OK_DRY
    results: list[dict] = []
    processed = 0
    stopped_on = None
    for item in corrections:
        if processed >= limit:
            break
        res = correct_one(conn, item["uniware_id"], item["doc_entry"], doc_month, cancel_series, apply)
        results.append(res)
        status = res["status"]
        if status.startswith("skipped_"):
            continue  # nothing was touched -- does not count against the limit
        processed += 1
        if status not in ok:
            stopped_on = res
            break
    return {
        "apply": apply, "doc_month": doc_month, "cancel_series": cancel_series, "limit": limit,
        "processed": processed, "stopped_on_anomaly": stopped_on, "results": results,
    }
