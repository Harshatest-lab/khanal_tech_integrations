"""Tests for recon_summary.py's three corrections, found and validated
live against real August 2026 data before this module existed:
1. Shipping charge is tax-inclusive on our side, pre-tax on SAP/Finance's
   -- must be stripped before comparing.
2. Shipping's own declared tax% can be an explicit 0 (not missing) on
   some channels -- falls back to the item's own tax rate.
3. Finance's Revenue Recognition query represents a fully-discounted
   BOGO/free line's Net Taxable Value as -1 instead of 0.
4. Credit memos are negative in Finance's convention; api_total is
   always a positive magnitude -- compare against the magnitude."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import recon_summary  # noqa: E402


def test_invoice_api_total_strips_shippings_own_declared_tax():
    # DSC-26089421 / DocEntry 81543: uc_shipping=50, shipping's own tax=18% ->
    # taxable shipping = 50/1.18 = 42.3729
    row = {"uc_taxable_value": "295.76", "uc_shipping": "50.00", "shipping_tax_percent": "18.0", "item_tax_percent": "18.0"}
    total = recon_summary._invoice_api_total(row)
    assert abs(total - (295.76 + 50 / 1.18)) < 0.001


def test_invoice_api_total_falls_back_to_item_tax_when_shipping_tax_is_explicit_zero():
    # AMAZON_IN_API: shippingchargetaxpercentage stored as an explicit '0.0'
    # (not null) while the item's own rate is 18% -- must fall back, not
    # treat the explicit 0 as "no shipping tax at all".
    row = {"uc_taxable_value": "1055.10", "uc_shipping": "50.00", "shipping_tax_percent": "0.0", "item_tax_percent": "18.0"}
    total = recon_summary._invoice_api_total(row)
    assert abs(total - (1055.10 + 50 / 1.18)) < 0.001


def test_invoice_api_total_uses_raw_shipping_when_truly_untaxed():
    row = {"uc_taxable_value": "100.00", "uc_shipping": "0", "shipping_tax_percent": None, "item_tax_percent": "18.0"}
    assert recon_summary._invoice_api_total(row) == 100.0


def test_sap_totals_corrects_bogo_placeholder_to_zero():
    finance_rows = [
        {"Doc Entry": 81543, "Document": "SALES INVOICE", "Net Taxable Value": 42.373, "Taxable Value": 42.373, "Discount Value": 0},
        {"Doc Entry": 81543, "Document": "SALES INVOICE", "Net Taxable Value": -1, "Taxable Value": 349, "Discount Value": 349},
        {"Doc Entry": 81543, "Document": "SALES INVOICE", "Net Taxable Value": 295.76, "Taxable Value": 295.76, "Discount Value": 0},
    ]
    out = recon_summary._sap_totals_by_doc_entry(finance_rows)
    # Without the correction this would sum to 42.373 - 1 + 295.76 = 337.133
    assert abs(out[81543]["ntv_sum"] - (42.373 + 0 + 295.76)) < 0.01


def test_sap_totals_leaves_a_real_negative_ntv_alone():
    # A real partial-discount line (Taxable != Discount) must not be zeroed.
    finance_rows = [{"Doc Entry": 1, "Document": "SALES INVOICE", "Net Taxable Value": -1, "Taxable Value": 100, "Discount Value": 50}]
    out = recon_summary._sap_totals_by_doc_entry(finance_rows)
    assert out[1]["ntv_sum"] == -1


def test_status_for_matches_credit_memo_by_magnitude_not_raw_sign():
    # Finance records a credit memo as negative revenue; our api_total is
    # always a positive magnitude -- same real amount, opposite sign.
    sap_by_entry = {12813: {"document": "CREDIT MEMO", "ntv_sum": -10289.23}}
    status, sap_total = recon_summary._status_for(10289.23, 12813, sap_by_entry)
    assert status == "matched"
    assert sap_total == -10289.23  # display value stays in Finance's real convention


def test_status_for_still_flags_a_genuine_mismatch():
    sap_by_entry = {1: {"document": "SALES INVOICE", "ntv_sum": 100.0}}
    status, _ = recon_summary._status_for(150.0, 1, sap_by_entry)
    assert status == "amount_mismatch"


def test_status_for_not_posted_without_doc_entry():
    status, sap_total = recon_summary._status_for(100.0, None, {})
    assert status == "not_posted"
    assert sap_total is None
