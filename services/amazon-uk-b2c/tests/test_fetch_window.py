"""sync_incremental window cap + batch save (no network, no DB)."""
from datetime import datetime, timedelta, timezone

import pytest

from amazon_uk import fetch_orders, store, sync_amazonuk

NOW = datetime(2026, 9, 21, 5, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture()
def run(monkeypatch):
    """Run sync_incremental with a given last watermark; return (result, calls)."""
    calls = {}

    def _run(last_watermark, initial_lookback_days=None, orders=None):
        monkeypatch.setattr(sync_amazonuk, "datetime", _FrozenDatetime)
        monkeypatch.setattr(store, "get_last_watermark", lambda source: last_watermark)
        monkeypatch.setattr(
            fetch_orders, "fetch_orders_by_update",
            lambda after, before=None: calls.update(after=after, before=before) or (orders or []),
        )
        monkeypatch.setattr(store, "upsert_orders", lambda o: calls.update(upserted=len(o)) or len(o))
        monkeypatch.setattr(store, "record_fetch_log", lambda *a: calls.update(logged=a))
        monkeypatch.setattr(sync_amazonuk, "fetch_and_store_shipments",
                            lambda s, e: calls.update(ship=(s, e)) or {"shipments_stored": 0})
        return sync_amazonuk.sync_incremental(initial_lookback_days), calls

    return _run


def test_normal_daily_run_is_uncapped_and_watermark_is_now(run):
    result, c = run(NOW - timedelta(hours=24))          # ran yesterday
    assert c["before"] is None                           # open-ended, like before
    assert c["after"] == "2026-09-19T23:00:00Z"          # watermark (24h ago) minus the 6h lookback
    assert result["window_capped"] is False
    assert result["watermark"] == NOW.isoformat()


def test_long_gap_is_capped_and_watermark_stops_at_window_end(run):
    result, c = run(NOW - timedelta(days=6))             # 6 days of downtime
    start = NOW - timedelta(days=6, hours=6)
    end = start + timedelta(hours=sync_amazonuk.MAX_WINDOW_HOURS)
    assert c["after"] == start.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert c["before"] == end.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert result["window_capped"] is True
    assert result["watermark"] == end.isoformat()        # NOT now — next run continues here
    assert c["logged"][1] == end


def test_gap_drains_and_terminates(run):
    """Repeated capped runs always move the watermark forward until uncapped."""
    wm, runs = NOW - timedelta(days=7), 0
    while True:
        result, _ = run(wm)
        new = datetime.fromisoformat(result["watermark"])
        assert new > wm                                   # strictly progresses
        wm, runs = new, runs + 1
        if not result["window_capped"]:
            break
        assert runs < 20
    assert runs <= 5                                       # 7 days drains in a handful of runs


def test_first_run_without_watermark_is_capped_too(run):
    result, c = run(None)                                  # 7-day default catch-up
    assert result["window_capped"] is True
    assert c["before"] is not None


def test_saves_all_orders_in_one_batch_call(run):
    orders = [{"orderId": str(i), "orderItems": []} for i in range(213)]
    result, c = run(NOW - timedelta(hours=24), orders=orders)
    assert c["upserted"] == 213 and result["orders"] == 213


def test_fetch_orders_by_update_sends_before_only_when_given(monkeypatch):
    seen = []
    monkeypatch.setattr(fetch_orders, "_paginate", lambda params: seen.append(params) or [])
    fetch_orders.fetch_orders_by_update("2026-09-01T00:00:00Z")
    fetch_orders.fetch_orders_by_update("2026-09-01T00:00:00Z", "2026-09-03T00:00:00Z")
    assert "lastUpdatedBefore" not in seen[0]
    assert seen[1]["lastUpdatedBefore"] == "2026-09-03T00:00:00Z"
    assert seen[1]["lastUpdatedAfter"] == "2026-09-01T00:00:00Z"


def test_upsert_orders_single_connection_and_flattens_items(monkeypatch):
    captured = {}
    monkeypatch.setattr(store, "flatten_order_item", lambda o, i: {"o": o["orderId"], "i": i["orderItemId"]})
    monkeypatch.setattr(store, "_upsert", lambda sql, rows: captured.update(sql=sql, rows=rows) or len(rows))
    orders = [
        {"orderId": "A", "orderItems": [{"orderItemId": "1"}, {"orderItemId": "2"}]},
        {"orderId": "B", "orderItems": [{"orderItemId": "3"}]},
        {"orderId": "C", "orderItems": []},
    ]
    assert store.upsert_orders(orders) == 3
    assert captured["rows"] == [{"o": "A", "i": "1"}, {"o": "A", "i": "2"}, {"o": "B", "i": "3"}]
    assert store.upsert_orders([]) == 0                # empty list is a no-op
