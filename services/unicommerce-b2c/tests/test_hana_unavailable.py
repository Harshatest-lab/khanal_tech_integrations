"""A HANA outage must not look like a stock shortage.

bulk_batch_inventory_by_warehouse used to return [] when HANA was down. That
became "no_sap_batch_available" for every order, then batch_hold, and 48h
later 'dead' -- with no alert. It now raises HanaUnavailableError: the run
aborts before claiming anything, so orders stay 'ready' and retry once HANA
is back."""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import hana_access, post_invoices  # noqa: E402


def _hana_down(monkeypatch):
    @contextmanager
    def broken():
        raise RuntimeError("HANA connect failed")
        yield  # pragma: no cover

    monkeypatch.setattr(hana_access, "hana_connection", broken)


def test_batch_inventory_raises_when_hana_is_unreachable(monkeypatch):
    _hana_down(monkeypatch)
    with pytest.raises(hana_access.HanaUnavailableError):
        hana_access.bulk_batch_inventory_by_warehouse("EC-FG", ["SKU-A"])


def test_batch_inventory_with_nothing_to_look_up_is_empty_not_an_error(monkeypatch):
    _hana_down(monkeypatch)
    assert hana_access.bulk_batch_inventory_by_warehouse("EC-FG", []) == []
    assert hana_access.bulk_batch_inventory_by_warehouse("", ["SKU-A"]) == []


def _one_item_order(monkeypatch):
    monkeypatch.setattr(
        post_invoices.sap_payload, "resolve_and_expand_items",
        lambda uc_data, settings: [{"itemSku": "SKU-A", "warehouseCode": "W"}],
    )
    monkeypatch.setattr(post_invoices.sap_payload, "_resolve_sap_warehouse", lambda settings, channel, wh: "EC-FG")
    return [{"uniware_id": "UC-1", "channel_name": "FLIPKART", "order_json": "{}"}]


def test_prewarm_propagates_a_batch_inventory_outage(monkeypatch):
    orders = _one_item_order(monkeypatch)

    def down(wh, item_codes):
        raise hana_access.HanaUnavailableError("down")

    monkeypatch.setattr(post_invoices.hana_access, "bulk_batch_inventory_by_warehouse", down)
    with pytest.raises(hana_access.HanaUnavailableError):
        post_invoices._prewarm_caches(orders, settings=object())


def test_prewarm_treats_an_unknown_batch_flag_as_an_outage(monkeypatch):
    """bulk_manage_batch_numbers answers None per item when its lookup fails.
    Posting on that skips batch allocation, SAP rejects the invoice, and the
    order burns a retry attempt for something that is not its fault."""
    orders = _one_item_order(monkeypatch)
    monkeypatch.setattr(post_invoices.hana_access, "bulk_batch_inventory_by_warehouse", lambda wh, items: [])
    monkeypatch.setattr(post_invoices.hana_access, "bulk_manage_batch_numbers", lambda items: {"SKU-A": None})

    with pytest.raises(hana_access.HanaUnavailableError):
        post_invoices._prewarm_caches(orders, settings=object())


def test_prewarm_still_returns_both_caches_when_hana_answers(monkeypatch):
    orders = _one_item_order(monkeypatch)
    rows = [{"item_code": "SKU-A", "warehouse_code": "EC-FG", "batch_number": "B1", "quantity": 4, "expiry": None}]
    monkeypatch.setattr(post_invoices.hana_access, "bulk_batch_inventory_by_warehouse", lambda wh, items: rows)
    monkeypatch.setattr(post_invoices.hana_access, "bulk_manage_batch_numbers", lambda items: {"SKU-A": True})

    managed, fefo = post_invoices._prewarm_caches(orders, settings=object())

    assert managed == {"SKU-A": True}
    assert fefo[("SKU-A", "EC-FG")] == [{"batch_number": "B1", "quantity": 4, "expiry": None}]


def test_post_ready_orders_aborts_before_claiming_anything_when_hana_is_down(monkeypatch):
    logged = {}
    settings = types.SimpleNamespace(posting_timeout_mins=30, max_retry_attempts=3)
    monkeypatch.setattr(post_invoices, "get_settings", lambda **kw: settings)
    monkeypatch.setattr(post_invoices.sap_client, "validate_active_series", lambda s: (True, "ok"))
    monkeypatch.setattr(post_invoices.store, "start_sync_log", lambda *a, **k: "run-1")
    monkeypatch.setattr(
        post_invoices.store, "finish_sync_log", lambda conn, run_id, status, *a, **k: logged.update(status=status)
    )
    monkeypatch.setattr(post_invoices.store, "recover_stuck_posting", lambda *a, **k: None)
    monkeypatch.setattr(post_invoices.store, "recover_failed_orders", lambda *a, **k: 0)
    monkeypatch.setattr(post_invoices.store, "recover_batch_hold_orders", lambda *a, **k: {})
    monkeypatch.setattr(post_invoices.store, "get_verify_orders", lambda *a, **k: [])
    monkeypatch.setattr(post_invoices.store, "get_ready_orders", lambda *a, **k: [{"uniware_id": "UC-1"}])

    import unicommerce.fetch_sync as fetch_sync_module

    monkeypatch.setattr(fetch_sync_module, "promote_fetched_to_ready", lambda *a, **k: {"moved_to_ready": 0, "deferred": 0})

    def down(orders, settings):
        raise hana_access.HanaUnavailableError("down")

    def fail_if_called(*a, **k):
        raise AssertionError("must not claim or post while HANA is down")

    monkeypatch.setattr(post_invoices, "_prewarm_caches", down)
    monkeypatch.setattr(post_invoices.store, "claim_ready_order", fail_if_called)
    monkeypatch.setattr(post_invoices.store, "mark_batch_hold", fail_if_called)

    with pytest.raises(hana_access.HanaUnavailableError):
        post_invoices.post_ready_orders(conn=None, limit=10)

    assert logged["status"] == "error"


class _Cursor:
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
        return [
            types.SimpleNamespace(name=n)
            for n in ("uniware_id", "channel_name", "order_date", "doc_date", "order_json", "attempt_count")
        ]

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cursor(self._row)


def test_post_one_order_hands_the_order_back_to_ready_when_hana_is_down(monkeypatch):
    seen = {}

    def down(orders, settings):
        raise hana_access.HanaUnavailableError("down")

    def fail_if_called(*a, **k):
        raise AssertionError("an outage is not the order's fault: must not burn a retry attempt")

    monkeypatch.setattr(post_invoices.store, "is_period_locked", lambda conn, d: False)
    monkeypatch.setattr(post_invoices, "get_settings", lambda **kw: object())
    monkeypatch.setattr(post_invoices.sap_client, "validate_active_series", lambda s: (True, "ok"))
    monkeypatch.setattr(post_invoices.store, "claim_ready_order", lambda conn, uid: True)
    monkeypatch.setattr(post_invoices.store, "unclaim_order", lambda conn, uid, to="ready": seen.update(last=(uid, to)))
    monkeypatch.setattr(post_invoices, "_prewarm_caches", down)
    monkeypatch.setattr(post_invoices.store, "mark_failed", fail_if_called)

    row = ("UC-9", "FLIPKART", "2026-09-10", "2026-09-12", "{}", 0)
    result = post_invoices.post_one_order(_Conn(row), "UC-9")

    assert result["status"] == "hana_unavailable"
    assert seen["last"] == ("UC-9", "ready")
