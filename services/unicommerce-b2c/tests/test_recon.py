"""Tests for recon.py: month-window scoping, line-matching, mismatch
diagnosis, and the refresh/one-order orchestration (with SAP/HANA/store
calls monkeypatched -- this flow is read-only against SAP so there's no
claim/state machine to exercise, just the selection -> diagnose -> write
sequence)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import recon  # noqa: E402


# ── current_and_previous_month_window ──

def test_month_window_mid_year():
    start, end = recon.current_and_previous_month_window(date(2026, 7, 27))
    assert start == "2026-06-01"
    assert end == "2026-07-27"


def test_month_window_january_wraps_to_prior_december():
    start, end = recon.current_and_previous_month_window(date(2026, 1, 15))
    assert start == "2025-12-01"
    assert end == "2026-01-15"


# ── _match_payload_lines ──

def test_match_payload_lines_exact_sku_and_qty():
    payload = [{"ItemCode": "SKU-A", "Quantity": 2, "UnitPrice": 100.0}]
    actual = [{"ItemCode": "SKU-A", "Quantity": 2, "UnitPrice": 100.0}]
    matched, payload_only, actual_only = recon._match_payload_lines(payload, actual)
    assert len(matched) == 1
    assert payload_only == []
    assert actual_only == []


def test_match_payload_lines_falls_back_to_sku_only_on_qty_mismatch():
    payload = [{"ItemCode": "SKU-A", "Quantity": 2, "UnitPrice": 100.0}]
    actual = [{"ItemCode": "SKU-A", "Quantity": 3, "UnitPrice": 100.0}]
    matched, payload_only, actual_only = recon._match_payload_lines(payload, actual)
    assert len(matched) == 1
    assert matched[0]["actual"]["Quantity"] == 3


def test_match_payload_lines_unmatched_sku_goes_to_payload_only():
    payload = [{"ItemCode": "SKU-A", "Quantity": 2, "UnitPrice": 100.0}]
    actual = [{"ItemCode": "SKU-B", "Quantity": 2, "UnitPrice": 100.0}]
    matched, payload_only, actual_only = recon._match_payload_lines(payload, actual)
    assert matched == []
    assert payload_only == payload
    assert actual_only == actual


# ── diagnose_invoice_mismatch ──

def test_diagnose_unit_price_line_diff():
    payload_lines = [{"ItemCode": "SKU-A", "Quantity": 2, "UnitPrice": 100.0}]
    actual_lines = [{"ItemCode": "SKU-A", "Quantity": 2, "UnitPrice": 90.0}]
    reason = recon.diagnose_invoice_mismatch(payload_lines, actual_lines, 200.0, 180.0, 10.0, 10.0)
    assert "SKU-A" in reason
    assert "unit diff" in reason


def test_diagnose_missing_sap_line():
    payload_lines = [{"ItemCode": "SKU-A", "Quantity": 2, "UnitPrice": 100.0}]
    actual_lines = []
    reason = recon.diagnose_invoice_mismatch(payload_lines, actual_lines, 200.0, 0.0, 10.0, 10.0)
    assert "missing in SAP" in reason


def test_diagnose_tax_double_counted_ratio():
    reason = recon.diagnose_invoice_mismatch([], [], 100.0, 100.0, 50.0, 100.0)
    assert "~2x" in reason


def test_diagnose_no_mismatch_returns_empty():
    payload_lines = [{"ItemCode": "SKU-A", "Quantity": 2, "UnitPrice": 100.0}]
    actual_lines = [{"ItemCode": "SKU-A", "Quantity": 2, "UnitPrice": 100.0}]
    reason = recon.diagnose_invoice_mismatch(payload_lines, actual_lines, 200.0, 200.0, 10.0, 10.0)
    assert reason == ""


# ── _recon_invoice wiring ──

def test_recon_invoice_forwards_prewarmed_fefo_cache_to_payload_builder(monkeypatch):
    """Regression: _recon_invoice rebuilds the invoice payload via
    sap_payload.ar_invoice_payload to compute expected totals -- it must
    pass the same fefo_rows_cache post_invoices.py's real post does
    (same bug class fixed in _post_order, see PR #25), or the rebuild
    silently diverges from what an actual post would send."""
    row = {
        "sap_ar_invoice_docentry": "999",
        "channel_name": "FLIPKART",
        "order_date": date(2026, 7, 20),
        "order_json": "{}",
    }
    monkeypatch.setattr(recon.store, "get_order_for_recon", lambda conn, uid: row)

    sentinel_fefo_cache = {"marker": "prewarmed"}
    prewarm_calls = []

    def fake_prewarm(orders, settings):
        prewarm_calls.append(orders)
        return {}, sentinel_fefo_cache

    monkeypatch.setattr(recon.post_invoices, "_prewarm_caches", fake_prewarm)

    captured = {}

    def fake_ar_invoice_payload(**kwargs):
        captured.update(kwargs)
        return {"DocumentLines": []}, 0.0

    monkeypatch.setattr(recon.sap_payload, "ar_invoice_payload", fake_ar_invoice_payload)
    monkeypatch.setattr(recon.sap_client, "get_invoice_by_doc_entry", lambda doc_entry: {"DocumentLines": []})
    monkeypatch.setattr(recon.store, "update_reconciliation_diffs", lambda *a, **k: None)

    recon._recon_invoice(_FakeConn(), "SO-1", settings=object())

    assert prewarm_calls == [[row]]
    assert captured["fefo_rows_cache"] is sentinel_fefo_cache


# ── refresh_recon / recon_one_order orchestration (monkeypatched) ──

class _FakeConn:
    pass


def test_refresh_recon_uses_default_month_window_and_reports_counts(monkeypatch):
    monkeypatch.setattr(recon.store, "get_recon_invoice_candidates", lambda *a, **k: ["SO-1", "SO-2"])
    monkeypatch.setattr(recon.store, "get_recon_cn_candidates", lambda *a, **k: ["SO-3"])

    calls = {"invoice": [], "cn": []}

    def fake_recon_invoice(conn, uniware_id, settings):
        calls["invoice"].append(uniware_id)
        if uniware_id == "SO-2":
            raise RuntimeError("boom")

    def fake_recon_cn(conn, uniware_id):
        calls["cn"].append(uniware_id)

    monkeypatch.setattr(recon, "_recon_invoice", fake_recon_invoice)
    monkeypatch.setattr(recon, "_recon_cn", fake_recon_cn)
    monkeypatch.setattr(recon, "get_settings", lambda **kw: object())

    result = recon.refresh_recon(_FakeConn(), limit=50)

    assert calls["invoice"] == ["SO-1", "SO-2"]
    assert calls["cn"] == ["SO-3"]
    assert result["invoice_selected"] == 2
    assert result["invoice_refreshed"] == 1
    assert result["invoice_failed"] == 1
    assert result["cn_selected"] == 1
    assert result["cn_refreshed"] == 1
    assert result["cn_failed"] == 0
    assert "window" in result and "start_date" in result["window"]


def test_refresh_recon_respects_explicit_date_window(monkeypatch):
    seen = {}

    def fake_invoice_candidates(conn, start_date, end_date, limit, **kw):
        seen["invoice_window"] = (start_date, end_date)
        return []

    def fake_cn_candidates(conn, start_date, end_date, limit):
        seen["cn_window"] = (start_date, end_date)
        return []

    monkeypatch.setattr(recon.store, "get_recon_invoice_candidates", fake_invoice_candidates)
    monkeypatch.setattr(recon.store, "get_recon_cn_candidates", fake_cn_candidates)
    monkeypatch.setattr(recon, "get_settings", lambda **kw: object())

    result = recon.refresh_recon(_FakeConn(), limit=10, start_date="2026-05-01", end_date="2026-05-31")

    assert seen["invoice_window"] == ("2026-05-01", "2026-05-31")
    assert seen["cn_window"] == ("2026-05-01", "2026-05-31")
    assert result["window"] == {"start_date": "2026-05-01", "end_date": "2026-05-31"}


def test_recon_one_order_not_found(monkeypatch):
    monkeypatch.setattr(recon.store, "get_order_for_recon", lambda conn, uid: None)
    monkeypatch.setattr(recon, "get_settings", lambda **kw: object())
    result = recon.recon_one_order(_FakeConn(), "SO-404")
    assert result == {"status": "not_found", "uniware_id": "SO-404"}


def test_recon_one_order_nothing_to_reconcile(monkeypatch):
    monkeypatch.setattr(
        recon.store, "get_order_for_recon",
        lambda conn, uid: {"sap_ar_invoice_docentry": None, "sap_ar_creditnote_docentry": None},
    )
    monkeypatch.setattr(recon, "get_settings", lambda **kw: object())
    result = recon.recon_one_order(_FakeConn(), "SO-1")
    assert result["status"] == "nothing_to_reconcile"


def test_recon_one_order_reconciles_invoice_and_cn(monkeypatch):
    monkeypatch.setattr(
        recon.store, "get_order_for_recon",
        lambda conn, uid: {"sap_ar_invoice_docentry": 111, "sap_ar_creditnote_docentry": 222},
    )
    monkeypatch.setattr(recon, "get_settings", lambda **kw: object())
    calls = {"invoice": False, "cn": False}
    monkeypatch.setattr(recon, "_recon_invoice", lambda conn, uid, settings: calls.__setitem__("invoice", True))
    monkeypatch.setattr(recon, "_recon_cn", lambda conn, uid: calls.__setitem__("cn", True))

    result = recon.recon_one_order(_FakeConn(), "SO-1")

    assert result == {"status": "reconciled", "uniware_id": "SO-1", "invoice": True, "credit_note": True}
    assert calls == {"invoice": True, "cn": True}
