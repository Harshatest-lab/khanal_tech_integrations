"""Tests for post_invoices.py's idempotency guard and posting-cutoff
wiring. Guard: a failed duplicate check must never fall through to a
blind post. Cutoff: post_ready_orders must always pass
constants.POSTING_CUTOFF_DATE into store.get_ready_orders (SAP/store
calls monkeypatched -- these exercise only the branches under test, not
the full posting pipeline)."""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import constants, post_invoices, sap_client, sap_payload  # noqa: E402
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


def test_check_batches_applies_sku_remap_for_retired_items():
    """A UC line still reporting a SAP-retired itemSku (e.g. a dead plain
    item replaced by a like-for-like item, non-CFG) must resolve against
    the replacement code's own batch/managed caches, not the dead one --
    otherwise HANA never has any batches under the old code and it looks
    like a permanent stock-out instead of a wrong item code."""
    uc_data = {"items": [{"itemSku": "FGDC0999-DEAD", "quantity": 2, "warehouseCode": "EC-FG"}]}
    result = post_invoices._check_batches(
        uniware_id="SO-2",
        channel_name="FLIPKART",
        uc_data=uc_data,
        settings=UnicommerceSettings(sku_remap={"FGDC0999-DEAD": "FGDC0999-NEW"}),
        batch_managed_cache={"FGDC0999-NEW": True},
        fefo_cache={("FGDC0999-NEW", "EC-FG"): [{"batch_number": "B1", "quantity": 10}]},
        reservation_ctx=post_invoices.new_reservation_context(),
    )
    assert result["ok"] is True
    assert result["missing"] == []


def test_check_batches_expands_sku_remap_to_cfg_bundle(monkeypatch):
    """When the remap target is itself a SAP Sales BOM parent (CFG-
    prefixed, e.g. real dead item FGHN0056 -> CFGHN0009), the check must
    run against the expanded child items' own batch availability, not
    the non-inventory bundle parent -- confirmed live 2026-08-25: posting
    the bundle parent's ItemCode directly gets SAP's "Selecting multiple
    locations is not permitted" (it only accepts pre-expanded children
    under a synthetic iSalesTree line)."""
    monkeypatch.setattr(
        sap_payload, "_cfg_children",
        lambda item_code: [{"item_code": "CHILD-A", "quantity": 1}, {"item_code": "CHILD-B", "quantity": 2}],
    )
    uc_data = {"items": [{"itemSku": "FGHN0056", "quantity": 1, "warehouseCode": "AMZ-BLR8", "unitPrice": 300.0}]}
    result = post_invoices._check_batches(
        uniware_id="SO-3",
        channel_name="AMAZON_FBA_IN_BLR8",
        uc_data=uc_data,
        settings=UnicommerceSettings(sku_remap={"FGHN0056": "CFGHN0009"}),
        batch_managed_cache={"CHILD-A": True, "CHILD-B": True},
        fefo_cache={
            ("CHILD-A", "AMZ-BLR8"): [{"batch_number": "B1", "quantity": 5}],
            ("CHILD-B", "AMZ-BLR8"): [{"batch_number": "B2", "quantity": 5}],
        },
        reservation_ctx=post_invoices.new_reservation_context(),
    )
    assert result["ok"] is True
    assert result["missing"] == []


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    @property
    def description(self):
        return [types.SimpleNamespace(name=n) for n in ("uniware_id", "channel_name", "order_date", "order_json", "attempt_count")]

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _FakeCursor(self._row)


def test_post_one_order_blocks_pre_cutoff_orders_by_default(monkeypatch):
    """post_one_order was the one gap POSTING_CUTOFF_DATE didn't cover --
    confirmed real 2026-08-25: a manual retry loop with no date filter on
    its list of uniware_ids posted 3 real orders from April/June/July
    into already-closed accounting periods. Must refuse by default, and
    never touch the series-guard/claim state while refusing -- no side
    effects from a blocked call."""

    def fail_if_called(*a, **k):
        raise AssertionError("must not proceed past the cutoff check")

    monkeypatch.setattr(post_invoices.sap_client, "validate_active_series", fail_if_called)
    monkeypatch.setattr(post_invoices.store, "unclaim_order", fail_if_called)
    monkeypatch.setattr(post_invoices.store, "claim_ready_order", fail_if_called)

    row = ("UC-OLD", "FLIPKART", "2026-06-04", "{}", 0)
    result = post_invoices.post_one_order(_FakeConn(row), "UC-OLD")

    assert result["status"] == "pre_cutoff_blocked"
    assert result["order_date"] == "2026-06-04"
    assert result["cutoff_date"] == constants.POSTING_CUTOFF_DATE


def test_post_one_order_force_bypasses_cutoff(monkeypatch):
    """force=True is the explicit override for a future remediation that
    genuinely needs to post a pre-cutoff order -- confirms the check is
    actually skipped (reaches the series guard) rather than just always
    blocking."""
    calls = {}

    def fake_validate(settings):
        calls["reached_series_guard"] = True
        return False, "forced-early-exit-for-test"

    monkeypatch.setattr(post_invoices.sap_client, "validate_active_series", fake_validate)
    monkeypatch.setattr(post_invoices, "get_settings", lambda **kw: object())

    row = ("UC-OLD", "FLIPKART", "2026-06-04", "{}", 0)
    result = post_invoices.post_one_order(_FakeConn(row), "UC-OLD", force=True)

    assert calls.get("reached_series_guard") is True
    assert result["status"] == "series_guard_failed"
