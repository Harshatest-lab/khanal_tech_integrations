"""Periodic reconciliation sweep: re-verify SAP vs expected for posted
invoices, backfill for posted credit notes.

Ported from khanal_tech_integrations/unicommerce/validators/reconcile.py's
refresh_recon (the real production entry point -- an older
_run_reconciliation_refresh_legacy exists there but is dead, reference
only), simplified per docs/superpowers/specs/2026-07-27-recon-design.md:

- Invoice: genuinely can mismatch (SAP can round, reject, or silently
  adjust something from what we submitted). Rather than re-deriving
  GST/discount/shipping-tax from raw UC order data like the Frappe
  source does, this rebuilds the *same* payload via
  sap_payload.ar_invoice_payload (the exact function post_invoices.py
  already calls) to get expected totals -- guaranteed consistent with
  what was actually posted, by construction.
- Credit note: expected always equals actual by construction here (SAP
  Credit Notes are "Copy From" the base invoice -- see
  store.update_cn_reconciliation's docstring). So this is backfill-only,
  not diagnosis: re-fetch the CN from SAP, write it if our row's
  reconciliation was never populated.

Read-only against SAP, writes only to our own Postgres -- no claim/state
machine needed at all, unlike PoGrn. A failed SAP fetch for one order is
logged and skipped; it stays selectable next run.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from unicommerce import post_invoices, sap_client, sap_payload, store
from unicommerce.settings import get_settings

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 200


def current_and_previous_month_window(today: date | None = None) -> tuple[str, str]:
    """Rolling 2-calendar-month window: previous month's 1st through
    today, recalculated fresh every run. Confirmed with the user: recon
    is a monthly-cadence process, older months are considered settled --
    no unbounded sweep, no historical backfill beyond this window."""
    today = today or date.today()
    if today.month == 1:
        prev_month_start = date(today.year - 1, 12, 1)
    else:
        prev_month_start = date(today.year, today.month - 1, 1)
    return prev_month_start.isoformat(), today.isoformat()


def _match_payload_lines(payload_lines: list[dict], actual_lines: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Match our submitted payload's DocumentLines to SAP's actual
    DocumentLines by ItemCode + Quantity (exact), falling back to
    ItemCode-only (handles partial-quantity edge cases) -- same two-pass
    idea as the Frappe source's _match_lines_by_sku_qty, simplified since
    both sides are already SAP-shaped (ItemCode/Quantity/UnitPrice), no
    raw UC field extraction needed. Returns (matched pairs, payload-only,
    actual-only)."""
    pool = list(actual_lines)
    matched: list[dict] = []
    payload_only: list[dict] = []
    for pl in payload_lines:
        sku = str(pl.get("ItemCode") or "")
        qty = float(pl.get("Quantity") or 0)
        found_idx = None
        for idx, al in enumerate(pool):
            if str(al.get("ItemCode") or "") == sku and abs(float(al.get("Quantity") or 0) - qty) <= 0.01:
                found_idx = idx
                break
        if found_idx is None:
            for idx, al in enumerate(pool):
                if str(al.get("ItemCode") or "") == sku:
                    found_idx = idx
                    break
        if found_idx is not None:
            matched.append({"payload": pl, "actual": pool.pop(found_idx)})
        else:
            payload_only.append(pl)
    return matched, payload_only, pool  # pool remainder = actual-only


def diagnose_invoice_mismatch(
    payload_lines: list[dict], actual_lines: list[dict],
    expected_unit: float, actual_unit: float, expected_tax: float, actual_tax: float,
) -> str:
    """Human-readable explanation for a reconciliation mismatch, stored in
    ar_invoice_mismatch_reason. Adapted from the Frappe source's
    _diagnose_invoice_mismatch."""
    unit_diff = round(abs(expected_unit - actual_unit), 2)
    tax_diff = round(abs(expected_tax - actual_tax), 2)
    parts: list[str] = []

    if unit_diff > 1.0:
        matched, payload_only, actual_only = _match_payload_lines(payload_lines, actual_lines)
        line_issues: list[str] = []
        for pair in matched:
            p_price = float(pair["payload"].get("UnitPrice") or 0)
            a_price = float(pair["actual"].get("UnitPrice") or 0)
            diff = round(abs(p_price - a_price), 2)
            if diff > 0.05:
                sku = str(pair["payload"].get("ItemCode") or "")
                qty = float(pair["payload"].get("Quantity") or 0)
                line_issues.append(f"{sku}: submitted Rs{p_price:.2f} vs SAP Rs{a_price:.2f} (diff Rs{diff:.2f} x{qty:.0f})")
        for pl in payload_only:
            line_issues.append(f"{pl.get('ItemCode')}: missing in SAP")
        for al in actual_only:
            price = float(al.get("UnitPrice") or 0)
            if price > 0:
                line_issues.append(f"{al.get('ItemCode')}: extra SAP line Rs{price:.2f} x{float(al.get('Quantity') or 0):.0f}")
        if line_issues:
            parts.append(f"unit diff Rs{unit_diff}: {'; '.join(line_issues[:3])}")
        else:
            parts.append(f"unit diff Rs{unit_diff}: SAP Rs{actual_unit:.2f} vs submitted Rs{expected_unit:.2f}")

    if tax_diff > 1.0:
        if expected_tax > 0:
            ratio = actual_tax / expected_tax
            if 1.85 <= ratio <= 2.15:
                parts.append(f"tax diff Rs{tax_diff}: SAP Rs{actual_tax:.2f} is ~2x submitted Rs{expected_tax:.2f} (IGST+CGST/SGST both summed?)")
            elif actual_tax > expected_tax:
                parts.append(f"tax diff Rs{tax_diff}: SAP Rs{actual_tax:.2f} > submitted Rs{expected_tax:.2f}")
            else:
                parts.append(f"tax diff Rs{tax_diff}: SAP Rs{actual_tax:.2f} < submitted Rs{expected_tax:.2f}")
        else:
            parts.append(f"tax diff Rs{tax_diff}: submitted Rs0 but SAP has Rs{actual_tax:.2f}")

    return " | ".join(parts)[:2000] if parts else ""


def _recon_invoice(conn, uniware_id: str, settings) -> None:
    row = store.get_order_for_recon(conn, uniware_id)
    if not row or not row.get("sap_ar_invoice_docentry"):
        return
    doc_entry = row["sap_ar_invoice_docentry"]

    uc_data = json.loads(row["order_json"] or "{}") if isinstance(row["order_json"], str) else (row["order_json"] or {})
    order_date = row.get("order_date")
    order_date_str = order_date.isoformat() if hasattr(order_date, "isoformat") else str(order_date)
    order_json_str = row["order_json"] if isinstance(row["order_json"], str) else json.dumps(row["order_json"], default=str)

    # Same pre-warm helper post_invoices.py uses for the real post -- keeps
    # the FEFO allocation this rebuild uses in agreement with what an actual
    # post would compute (see post_invoices._post_order's identical wiring;
    # this rebuild only feeds price/tax totals into the diff below, but a
    # payload that doesn't match what SAP would actually accept is still
    # worth avoiding on principle).
    batch_managed_cache, fefo_cache = post_invoices._prewarm_caches([row], settings)

    payload, tax_expected = sap_payload.ar_invoice_payload(
        uniware_id=uniware_id, channel=row.get("channel_name") or "", order_json=order_json_str,
        uc_data=uc_data, order_date=order_date_str, settings=settings, batch_managed_cache=batch_managed_cache,
        fefo_rows_cache=fefo_cache,
        # Read-only rebuild of an ALREADY-POSTED invoice for a price/tax diff
        # (DocDate is not compared). Legacy invoices may predate the ship-date
        # rule or have no shipment_date at all, so fall back to the order date
        # here only -- the real posting path never falls back.
        doc_date=row.get("doc_date") or order_date_str,
    )
    payload_lines = payload.get("DocumentLines", [])
    expected_unit = sap_payload.sum_unit_price_total(payload_lines)

    actual_doc = sap_client.get_invoice_by_doc_entry(doc_entry)
    actual_lines = actual_doc.get("DocumentLines", [])
    actual_unit = sap_payload.sum_unit_price_total(actual_lines)
    actual_tax_from_lines = sap_payload.sum_tax_total(actual_lines)
    actual_tax = actual_tax_from_lines if actual_tax_from_lines else float(actual_doc.get("DocTaxAmount") or 0)

    unit_diff = abs(expected_unit - actual_unit)
    reason = ""
    if unit_diff > 1.0 or abs(tax_expected - actual_tax) > 1.0:
        reason = diagnose_invoice_mismatch(payload_lines, actual_lines, expected_unit, actual_unit, tax_expected, actual_tax)

    store.update_reconciliation_diffs(conn, uniware_id, tax_expected, actual_tax, unit_diff, expected_unit, actual_unit, mismatch_reason=reason)


def _recon_cn(conn, uniware_id: str) -> None:
    row = store.get_order_for_recon(conn, uniware_id)
    if not row or not row.get("sap_ar_creditnote_docentry"):
        return
    doc_entry = row["sap_ar_creditnote_docentry"]
    cn_doc = sap_client.get_credit_note_by_doc_entry(doc_entry)
    lines = cn_doc.get("DocumentLines", [])
    actual_unit = sap_payload.sum_unit_price_total(lines) if lines else 0.0
    actual_tax = float(cn_doc.get("DocTaxAmount") or 0) or sap_payload.sum_tax_total(lines)
    store.update_cn_reconciliation(conn, uniware_id, actual_unit, actual_tax)


def refresh_recon(conn, limit: int = DEFAULT_LIMIT, start_date: str | None = None, end_date: str | None = None) -> dict:
    settings = get_settings(force_reload=True)
    if not start_date or not end_date:
        start_date, end_date = current_and_previous_month_window()

    run_id = store.start_sync_log(conn, "recon", start_date, end_date)
    try:
        invoice_ids = store.get_recon_invoice_candidates(conn, start_date, end_date, limit)
        inv_refreshed = inv_failed = 0
        for uniware_id in invoice_ids:
            try:
                _recon_invoice(conn, uniware_id, settings)
                inv_refreshed += 1
            except Exception as e:
                inv_failed += 1
                logger.warning("recon: invoice refresh failed for %s: %s", uniware_id, e, exc_info=True)

        cn_ids = store.get_recon_cn_candidates(conn, start_date, end_date, limit)
        cn_refreshed = cn_failed = 0
        for uniware_id in cn_ids:
            try:
                _recon_cn(conn, uniware_id)
                cn_refreshed += 1
            except Exception as e:
                cn_failed += 1
                logger.warning("recon: CN refresh failed for %s: %s", uniware_id, e, exc_info=True)

        result = {
            "window": {"start_date": start_date, "end_date": end_date},
            "invoice_selected": len(invoice_ids), "invoice_refreshed": inv_refreshed, "invoice_failed": inv_failed,
            "cn_selected": len(cn_ids), "cn_refreshed": cn_refreshed, "cn_failed": cn_failed,
        }
        store.finish_sync_log(conn, run_id, "success", result)
        return result
    except Exception as e:
        store.finish_sync_log(conn, run_id, "error", error=str(e))
        raise


def recon_one_order(conn, uniware_id: str) -> dict:
    """Manual single-order retry, bypassing the date window and limit."""
    settings = get_settings(force_reload=True)
    row = store.get_order_for_recon(conn, uniware_id)
    if not row:
        return {"status": "not_found", "uniware_id": uniware_id}

    did_invoice = did_cn = False
    if row.get("sap_ar_invoice_docentry"):
        _recon_invoice(conn, uniware_id, settings)
        did_invoice = True
    if row.get("sap_ar_creditnote_docentry"):
        _recon_cn(conn, uniware_id)
        did_cn = True

    if not did_invoice and not did_cn:
        return {"status": "nothing_to_reconcile", "uniware_id": uniware_id}
    return {"status": "reconciled", "uniware_id": uniware_id, "invoice": did_invoice, "credit_note": did_cn}
