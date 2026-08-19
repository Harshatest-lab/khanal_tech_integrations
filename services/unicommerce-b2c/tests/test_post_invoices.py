"""Tests for post_invoices.py's idempotency guard and posting-cutoff
wiring. Guard: a failed duplicate check must never fall through to a
blind post. Cutoff: post_ready_orders must always pass
constants.POSTING_CUTOFF_DATE into store.get_ready_orders (SAP/store
calls monkeypatched -- these exercise only the branches under test, not
the full posting pipeline)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import constants, post_invoices, sap_client  # noqa: E402
from unicommerce.settings import UnicommerceSettings  # noqa: E402


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


def test_post_ready_orders_passes_posting_cutoff_to_get_ready_orders(monkeypatch):
    import types
    calls = {}

    fake_settings = types.SimpleNamespace(posting_timeout_mins=30, max_retry_attempts=3)
    monkeypatch.setattr(post_invoices, "get_settings", lambda **kw: fake_settings)
    monkeypatch.setattr(post_invoices.sap_client, "validate_active_series", lambda settings: (True, "ok"))
    monkeypatch.setattr(post_invoices.store, "start_sync_log", lambda *a, **k: "run-1")
    monkeypatch.setattr(post_invoices.store, "finish_sync_log", lambda *a, **k: None)
    monkeypatch.setattr(post_invoices.store, "recover_stuck_posting", lambda *a, **k: None)
    monkeypatch.setattr(post_invoices.store, "recover_failed_orders", lambda *a, **k: 0)
    monkeypatch.setattr(post_invoices.store, "recover_batch_hold_orders", lambda *a, **k: {})
    monkeypatch.setattr(post_invoices.store, "get_verify_orders", lambda *a, **k: [])
    monkeypatch.setattr(post_invoices.hana_access, "bulk_manage_batch_numbers", lambda item_codes: {})

    import unicommerce.fetch_sync as fetch_sync_module
    monkeypatch.setattr(fetch_sync_module, "promote_fetched_to_ready", lambda *a, **k: {"moved_to_ready": 0, "deferred": 0})

    def fake_get_ready_orders(conn, limit, cutoff_date, **kw):
        calls["cutoff_date"] = cutoff_date
        return []

    monkeypatch.setattr(post_invoices.store, "get_ready_orders", fake_get_ready_orders)

    post_invoices.post_ready_orders(conn=None, limit=10)

    assert calls["cutoff_date"] == constants.POSTING_CUTOFF_DATE


def test_check_batches_ignores_ucs_batch_number_uses_fefo_instead():
    """UC's own recorded batchNumber on a line item is informational, not
    a hard requirement -- confirmed real incident 2026-08-19: re-posting
    week-old orders hit "have 0.0" on UC's exact recorded batch while a
    different batch of the same item was genuinely available (consumed
    by later orders in the meantime). The validator must find and use
    whatever's currently available via FEFO, not hard-fail on UC's
    specific (now possibly gone) batch."""
    uc_data = {
        "items": [
            {
                "itemSku": "SKU-A", "quantity": 5, "warehouseCode": "EC-FG",
                "batchNumber": "OLD-BATCH-NOW-DEPLETED",
            }
        ]
    }
    result = post_invoices._check_batches(
        uniware_id="SO-1",
        channel_name="FLIPKART",
        uc_data=uc_data,
        settings=UnicommerceSettings(),
        batch_managed_cache={"SKU-A": True},
        # No "OLD-BATCH-NOW-DEPLETED" row at all -- only a different,
        # currently-available batch.
        fefo_cache={("SKU-A", "EC-FG"): [{"batch_number": "CURRENTLY-AVAILABLE", "quantity": 10}]},
        reservation_ctx=post_invoices.new_reservation_context(),
    )
    assert result["ok"] is True
    assert result["missing"] == []
