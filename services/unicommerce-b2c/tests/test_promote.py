"""Tests for promote_fetched_to_ready -- the fetched->ready gate.

This was found missing entirely during live testing 2026-07-24: the
posting worker only processes 'ready' orders, but nothing promoted
'fetched' -> 'ready', so postInvoices would silently process zero orders
forever. Ported from khanal_tech_integrations/unicommerce/workers/fetch.py."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import fetch_sync  # noqa: E402


class _FakeStore:
    """Minimal in-memory stand-in for the store module's promote surface."""

    def __init__(self, fetched: list[dict]):
        self._fetched = fetched
        self.marked_ready: list[str] = []
        self.deferred: list[tuple[str, str]] = []

    def get_fetched_orders(self, conn, limit=None, start_date=None, end_date=None):
        return self._fetched

    def mark_ready(self, conn, uniware_id):
        self.marked_ready.append(uniware_id)
        return True

    def defer_fetched(self, conn, uniware_id, reason):
        self.deferred.append((uniware_id, reason))


def _order(status, items):
    return json.dumps({"status": status, "saleOrderItems": items})


_ITEM_OK = {"statusCode": "COMPLETE", "quantity": 1}


def test_promotes_only_complete_orders_with_eligible_items(monkeypatch):
    fetched = [
        {"uniware_id": "A", "order_json": _order("COMPLETE", [_ITEM_OK])},   # ready
        {"uniware_id": "B", "order_json": _order("PROCESSING", [_ITEM_OK])},  # not complete
        {"uniware_id": "C", "order_json": _order("COMPLETE", [])},            # no line items
    ]
    fake = _FakeStore(fetched)
    monkeypatch.setattr(fetch_sync, "store", fake)

    result = fetch_sync.promote_fetched_to_ready(conn=None)

    assert result == {"moved_to_ready": 1, "deferred": 2, "refetched": 0}
    assert fake.marked_ready == ["A"]
    deferred_ids = [d[0] for d in fake.deferred]
    assert deferred_ids == ["B", "C"]
    # Each deferral records a human-readable reason.
    assert all(reason for _, reason in fake.deferred)


def test_handles_dict_order_json_not_just_str(monkeypatch):
    # psycopg returns jsonb columns as dicts already, not JSON strings --
    # promote must handle both.
    fetched = [{"uniware_id": "A", "order_json": {"status": "COMPLETE", "saleOrderItems": [_ITEM_OK]}}]
    fake = _FakeStore(fetched)
    monkeypatch.setattr(fetch_sync, "store", fake)

    result = fetch_sync.promote_fetched_to_ready(conn=None)

    assert result == {"moved_to_ready": 1, "deferred": 0, "refetched": 0}


def test_mark_ready_returning_false_is_not_counted(monkeypatch):
    # A concurrent run already moved the row -> mark_ready returns False ->
    # this run must not double-count it as promoted.
    fetched = [{"uniware_id": "A", "order_json": _order("COMPLETE", [_ITEM_OK])}]
    fake = _FakeStore(fetched)
    fake.mark_ready = lambda conn, uniware_id: False
    monkeypatch.setattr(fetch_sync, "store", fake)

    result = fetch_sync.promote_fetched_to_ready(conn=None)

    assert result == {"moved_to_ready": 0, "deferred": 0, "refetched": 0}


def test_empty_fetched_queue(monkeypatch):
    fake = _FakeStore([])
    monkeypatch.setattr(fetch_sync, "store", fake)
    assert fetch_sync.promote_fetched_to_ready(conn=None) == {"moved_to_ready": 0, "deferred": 0, "refetched": 0}


class _FakeUcClient:
    """Records that a live re-fetch was attempted and returns a
    now-COMPLETE order, standing in for uc_client.UcClient."""

    def __init__(self, creds):
        pass

    def login(self):
        pass

    def get_order_detail(self, uniware_id):
        return {"status": "COMPLETE", "saleOrderItems": [_ITEM_OK]}


def test_stale_fetched_row_triggers_live_refetch_and_upsert(monkeypatch):
    # Root cause found live 2026-08-22: an order stuck 'fetched' with a
    # cached non-COMPLETE status never got re-checked, even a full day
    # after it actually completed upstream in UC -- defer_fetched only
    # ever re-reads the same stale order_json. Past the staleness cutoff,
    # promote must do one live re-fetch before deciding.
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=120)
    fetched = [{"uniware_id": "A", "order_json": _order("PROCESSING", [_ITEM_OK]), "updated_at": old_ts}]
    fake = _FakeStore(fetched)
    fake.upserted: list[tuple] = []
    fake.upsert_order = lambda conn, uniware_id, order: fake.upserted.append((uniware_id, order))
    monkeypatch.setattr(fetch_sync, "store", fake)
    monkeypatch.setattr(fetch_sync, "UcClient", _FakeUcClient)
    monkeypatch.setattr(fetch_sync, "get_uc_credentials", lambda: None)

    result = fetch_sync.promote_fetched_to_ready(conn=None)

    assert result == {"moved_to_ready": 1, "deferred": 0, "refetched": 1}
    assert fake.marked_ready == ["A"]
    assert fake.upserted[0][0] == "A"
    assert fake.upserted[0][1]["status"] == "COMPLETE"


def test_fresh_fetched_row_is_not_refetched(monkeypatch):
    # A row still well within the staleness window must not trigger a
    # live API call -- it's plausibly still genuinely in-flight upstream.
    recent_ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    fetched = [{"uniware_id": "B", "order_json": _order("PROCESSING", [_ITEM_OK]), "updated_at": recent_ts}]
    fake = _FakeStore(fetched)

    def _must_not_construct(*a, **k):
        raise AssertionError("UcClient must not be constructed for a fresh row")

    monkeypatch.setattr(fetch_sync, "store", fake)
    monkeypatch.setattr(fetch_sync, "UcClient", _must_not_construct)
    monkeypatch.setattr(fetch_sync, "get_uc_credentials", lambda: None)

    result = fetch_sync.promote_fetched_to_ready(conn=None)

    assert result == {"moved_to_ready": 0, "deferred": 1, "refetched": 0}


def test_stale_row_refetch_failure_falls_back_to_defer(monkeypatch):
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=120)
    fetched = [{"uniware_id": "A", "order_json": _order("PROCESSING", [_ITEM_OK]), "updated_at": old_ts}]
    fake = _FakeStore(fetched)

    class _BoomUcClient(_FakeUcClient):
        def get_order_detail(self, uniware_id):
            raise RuntimeError("UC API down")

    monkeypatch.setattr(fetch_sync, "store", fake)
    monkeypatch.setattr(fetch_sync, "UcClient", _BoomUcClient)
    monkeypatch.setattr(fetch_sync, "get_uc_credentials", lambda: None)

    result = fetch_sync.promote_fetched_to_ready(conn=None)

    assert result == {"moved_to_ready": 0, "deferred": 1, "refetched": 0}
    assert fake.deferred[0][0] == "A"
