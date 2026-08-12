"""Tests for post_invoices.py's idempotency guard: a failed duplicate
check must never fall through to a blind post (SAP/store calls
monkeypatched -- this exercises only the guard branch, not the full
posting pipeline)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import post_invoices, sap_client  # noqa: E402


def test_post_order_defers_to_verify_when_duplicate_check_fails(monkeypatch):
    calls = {}

    def fake_find(uniware_id):
        raise sap_client.SapLookupError("SAP unreachable")

    def fake_unclaim(conn, uniware_id, to_status="ready"):
        calls["unclaim"] = (uniware_id, to_status)

    def fail_if_called(*a, **k):
        raise AssertionError("must not proceed to post when the duplicate check failed")

    monkeypatch.setattr(post_invoices.sap_client, "find_invoice_by_uc_order_id", fake_find)
    monkeypatch.setattr(post_invoices.store, "unclaim_order", fake_unclaim)
    monkeypatch.setattr(post_invoices.store, "mark_posted", fail_if_called)
    monkeypatch.setattr(post_invoices.sap_client, "post_invoice_with_series_fallback", fail_if_called)

    row = {"uniware_id": "UC-1"}
    outcome, shortages = post_invoices._post_order(
        conn=None, row=row, settings=object(),
        batch_managed_cache={}, fefo_cache={}, series_cache={}, reservation_ctx={},
    )

    assert outcome == "verify"
    assert shortages == []
    assert calls["unclaim"] == ("UC-1", "verify")
