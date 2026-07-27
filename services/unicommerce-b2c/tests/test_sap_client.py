"""Tests for validate_active_series: it must check the SAP series IDs
actually referenced in Unicommerce Settings (what postings really use),
not SAP's unrelated company-wide default series."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import sap_client  # noqa: E402
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
