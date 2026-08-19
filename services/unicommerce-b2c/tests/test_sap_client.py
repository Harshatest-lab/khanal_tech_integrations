"""Tests for validate_active_series: it must check the SAP series IDs
actually referenced in Unicommerce Settings (what postings really use),
not SAP's unrelated company-wide default series. Also covers the
duplicate-invoice/credit-note guards: primary check is HANA (the header
UDFs SAP SL was originally queried against don't exist on this SAP
install), falling back to SL-based checks only if HANA itself fails."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import hana_access, sap_client  # noqa: E402
from unicommerce.settings import UnicommerceSettings  # noqa: E402


def _current_period() -> str:
    today = datetime.today()
    fy_start = today.year if today.month >= 4 else today.year - 1
    return f"FY{str(fy_start)[-2:]}{str(fy_start + 1)[-2:]}"


def _stale_period() -> str:
    today = datetime.today()
    fy_start = today.year if today.month >= 4 else today.year - 1
    prev_start = fy_start - 1
    return f"FY{str(prev_start)[-2:]}{str(prev_start + 1)[-2:]}"


def _settings(**overrides) -> UnicommerceSettings:
    base = dict(ar_invoice_series_map={"CHANNEL_A": 100, "CHANNEL_B": 200}, ar_invoice_series=100, credit_note_series=300)
    base.update(overrides)
    return UnicommerceSettings(**base)


def test_all_configured_series_current_fy_passes(monkeypatch):
    period = _current_period()
    series_by_id = {100: "KAB2C", 200: "MHB2C", 300: "KAB2CN"}

    def fake_request(method, path, body=None):
        sid = body["SeriesParams"]["Series"]
        return 200, {"Name": series_by_id[sid], "PeriodIndicator": period}

    monkeypatch.setattr(sap_client, "_request", fake_request)
    ok, msg = sap_client.validate_active_series(_settings())
    assert ok is True
    assert "3 configured series" in msg


def test_one_stale_series_blocks_the_run(monkeypatch):
    current = _current_period()
    stale = _stale_period()

    def fake_request(method, path, body=None):
        sid = body["SeriesParams"]["Series"]
        period = stale if sid == 200 else current
        return 200, {"Name": f"SERIES{sid}", "PeriodIndicator": period}

    monkeypatch.setattr(sap_client, "_request", fake_request)
    ok, msg = sap_client.validate_active_series(_settings())
    assert ok is False
    assert "POSTING BLOCKED" in msg
    assert "200" in msg


def test_unresolvable_series_warns_but_does_not_block(monkeypatch):
    def fake_request(method, path, body=None):
        raise RuntimeError("SAP unreachable")

    monkeypatch.setattr(sap_client, "_request", fake_request)
    ok, msg = sap_client.validate_active_series(_settings())
    assert ok is True
    assert "could not be resolved" in msg


def test_no_series_configured_passes_trivially(monkeypatch):
    def fake_request(method, path, body=None):
        raise AssertionError("should not be called when nothing is configured")

    monkeypatch.setattr(sap_client, "_request", fake_request)
    ok, msg = sap_client.validate_active_series(_settings(ar_invoice_series_map={}, ar_invoice_series=None, credit_note_series=None))
    assert ok is True
    assert "nothing to validate" in msg


def test_current_fy_period_indicator_matches_fy_start_suffix():
    # Confirmed against live NNM1 rows in india_test: PeriodIndicator
    # "FY2627" pairs with series named "...26" (the FY-start year), not
    # "...27" -- this is the inverted assumption the old guard had.
    assert sap_client._current_fy_period_indicator() == _current_period()


# ── find_invoice_by_uc_order_id ────────────────────────────────────────

def test_find_invoice_uses_hana_result_without_touching_sl(monkeypatch):
    monkeypatch.setattr(hana_access, "find_invoice_by_order_id", lambda key: {"doc_entry": 1, "doc_num": 2, "doc_tax_amount": 3.0})
    monkeypatch.setattr(sap_client, "_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("SL must not be called")))
    result = sap_client.find_invoice_by_uc_order_id("ORDER1")
    assert result == {"doc_entry": 1, "doc_num": 2, "doc_tax_amount": 3.0}


def test_find_invoice_hana_none_is_trusted_as_not_found(monkeypatch):
    monkeypatch.setattr(hana_access, "find_invoice_by_order_id", lambda key: None)
    monkeypatch.setattr(sap_client, "_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("SL must not be called")))
    assert sap_client.find_invoice_by_uc_order_id("ORDER1") is None


def test_find_invoice_falls_back_to_sl_when_hana_fails(monkeypatch):
    def broken_hana(key):
        raise RuntimeError("HANA down")

    monkeypatch.setattr(hana_access, "find_invoice_by_order_id", broken_hana)

    def fake_request(method, path, body=None):
        assert "U_UCOrderID" in path
        return 200, {"value": [{"DocEntry": 9, "DocNum": 8, "DocTaxAmount": 1.5, "Comments": "Unicommerce Order: ORDER1 | Channel: TEST"}]}

    monkeypatch.setattr(sap_client, "_request", fake_request)
    result = sap_client.find_invoice_by_uc_order_id("ORDER1")
    assert result == {"doc_entry": 9, "doc_num": 8, "doc_tax_amount": 1.5, "comments": "Unicommerce Order: ORDER1 | Channel: TEST"}


def test_find_invoice_raises_when_every_path_fails(monkeypatch):
    monkeypatch.setattr(hana_access, "find_invoice_by_order_id", lambda key: (_ for _ in ()).throw(RuntimeError("HANA down")))
    monkeypatch.setattr(sap_client, "_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("SL down")))
    try:
        sap_client.find_invoice_by_uc_order_id("ORDER1")
        assert False, "expected SapLookupError"
    except sap_client.SapLookupError:
        pass


# ── find_credit_note_by_return_id ──────────────────────────────────────

def test_find_credit_note_uses_hana_via_base_doc_entry(monkeypatch):
    seen = {}

    def fake_hana(base_doc_entry):
        seen["base_doc_entry"] = base_doc_entry
        return {"doc_entry": 5, "doc_num": 6, "doc_tax_amount": 0.5}

    monkeypatch.setattr(hana_access, "find_credit_note_by_base_invoice", fake_hana)
    monkeypatch.setattr(sap_client, "_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("SL must not be called")))
    result = sap_client.find_credit_note_by_return_id("RET1", base_doc_entry=59967)
    assert result == {"doc_entry": 5, "doc_num": 6, "doc_tax_amount": 0.5}
    assert seen["base_doc_entry"] == 59967


def test_find_credit_note_skips_hana_without_base_doc_entry(monkeypatch):
    monkeypatch.setattr(
        hana_access, "find_credit_note_by_base_invoice", lambda *a: (_ for _ in ()).throw(AssertionError("must not be called"))
    )
    calls = []

    def fake_request(method, path, body=None):
        calls.append(path)
        return 200, {"value": []}

    monkeypatch.setattr(sap_client, "_request", fake_request)
    assert sap_client.find_credit_note_by_return_id("RET1") is None
    assert any("U_UCReturnID" in p for p in calls)
