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
    """Run sync_incremental with a given last watermark; return (result, calls).

    calls["fetches"] = [(after, before), ...] one per slice;
    calls["logged"]  = [watermark, ...] one per saved slice.
    `orders` are returned for the first slice only; `raise_on` (1-based call
    number) raises FetchBudgetExceeded with `partial` on that slice."""
    calls = {"fetches": [], "logged": [], "upserted": []}

    def _run(last_watermark, initial_lookback_days=None, orders=None, raise_on=None, partial=None,
             time_budget_seconds=None):
        def fake_fetch(after, before=None, deadline=None):
            calls["fetches"].append((after, before))
            calls["deadline"] = deadline
            if raise_on == len(calls["fetches"]):
                raise fetch_orders.FetchBudgetExceeded(partial or [])
            return (orders or []) if len(calls["fetches"]) == 1 else []

        monkeypatch.setattr(sync_amazonuk, "datetime", _FrozenDatetime)
        monkeypatch.setattr(store, "get_last_watermark", lambda source: last_watermark)
        monkeypatch.setattr(fetch_orders, "fetch_orders_by_update", fake_fetch)
        monkeypatch.setattr(store, "upsert_orders", lambda o: calls["upserted"].append(len(o)) or len(o))
        monkeypatch.setattr(store, "record_fetch_log", lambda src, wm, n, i: calls["logged"].append(wm))
        monkeypatch.setattr(sync_amazonuk, "fetch_and_store_shipments",
                            lambda s, e, **kw: calls.update(ship=(s, e), ship_kw=kw) or {"shipments_stored": 0})
        return sync_amazonuk.sync_incremental(initial_lookback_days,
                                              time_budget_seconds=time_budget_seconds), calls

    return _run


def test_normal_daily_run_is_sliced_last_slice_open_ended_watermark_now(run):
    result, c = run(NOW - timedelta(hours=24))          # ran yesterday
    assert c["fetches"][0][0] == "2026-09-19T23:00:00Z"  # watermark (24h ago) minus the 6h lookback
    assert len(c["fetches"]) == 5                        # 30h window / 6h slices
    assert all(before is not None for _, before in c["fetches"][:-1])
    assert c["fetches"][-1][1] is None                   # final slice open-ended, like before
    assert result["window_capped"] is False and result["slices"] == 5
    assert result["watermark"] == NOW.isoformat() and c["logged"][-1] == NOW
    assert result["budget_exhausted"] is False


def test_slices_are_contiguous(run):
    _, c = run(NOW - timedelta(hours=24))
    for (_, prev_before), (nxt_after, _) in zip(c["fetches"], c["fetches"][1:]):
        assert prev_before == nxt_after                   # no gap, no overlap between slices


def test_long_gap_is_capped_and_watermark_stops_at_window_end(run):
    result, c = run(NOW - timedelta(days=6))             # 6 days of downtime
    start = NOW - timedelta(days=6, hours=6)
    end = start + timedelta(hours=sync_amazonuk.MAX_WINDOW_HOURS)
    assert c["fetches"][0][0] == start.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert c["fetches"][-1][1] == end.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert result["window_capped"] is True
    assert result["watermark"] == end.isoformat()        # NOT now — next run continues here
    assert c["logged"][-1] == end


def test_watermark_saved_after_every_slice(run):
    _, c = run(NOW - timedelta(days=6))
    assert len(c["logged"]) == len(c["fetches"]) == 8    # 48h / 6h
    assert c["logged"] == sorted(c["logged"])            # strictly forward


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
    assert c["fetches"][-1][1] is not None


def test_saves_each_slice_in_one_batch_call(run):
    orders = [{"orderId": str(i), "orderItems": []} for i in range(213)]
    result, c = run(NOW - timedelta(hours=24), orders=orders)
    assert c["upserted"][0] == 213 and result["orders"] == 213


def test_budget_exhausted_keeps_finished_slices_and_partial_pages(run):
    partial = [{"orderId": "p1", "orderItems": []}, {"orderId": "p2", "orderItems": []}]
    result, c = run(NOW - timedelta(days=6), raise_on=3, partial=partial)
    assert result["budget_exhausted"] is True and result["slices"] == 2
    assert len(c["logged"]) == 2                          # slices 1-2 saved
    assert result["watermark"] == c["logged"][-1].isoformat()
    assert c["upserted"][-1] == 2                         # partial page of slice 3 stored...
    assert len(c["fetches"]) == 3                         # ...and the run stopped there


def test_budget_exhausted_before_any_slice_leaves_watermark_unchanged(run):
    last = NOW - timedelta(hours=24)
    result, c = run(last, raise_on=1)
    assert result["slices"] == 0 and result["budget_exhausted"] is True
    assert result["watermark"] == last.isoformat()
    assert c["logged"] == [] and "ship" not in c          # nothing saved, no shipment report


def test_time_budget_reserves_time_for_shipments(run, monkeypatch):
    monkeypatch.setattr(sync_amazonuk.time, "monotonic", lambda: 1000.0)
    result, c = run(NOW - timedelta(hours=24), time_budget_seconds=800)
    assert c["deadline"] == 1000.0 + 800 - sync_amazonuk.SHIPMENT_RESERVE_SECONDS
    assert 2 <= c["ship_kw"]["max_polls"] <= 20            # polls capped to the time that is left


def test_shipments_skipped_when_no_time_left(run, monkeypatch):
    monkeypatch.setattr(sync_amazonuk.time, "monotonic", lambda: 1000.0)
    result, c = run(NOW - timedelta(hours=24), time_budget_seconds=30)
    assert "ship" not in c and result["shipments_stored"] == 0


def test_page_wait_raises_instead_of_overrunning_deadline(monkeypatch):
    now = 5000.0
    monkeypatch.setattr(fetch_orders.time, "monotonic", lambda: now)
    monkeypatch.setattr(fetch_orders.time, "sleep", lambda s: pytest.fail("must not sleep past the deadline"))
    monkeypatch.setattr(fetch_orders, "_last_request_at", now)           # just made a request
    with pytest.raises(fetch_orders.FetchBudgetExceeded) as exc:
        fetch_orders._wait_for_next_page(now + 100, [{"orderId": "x"}])  # 180 s pacing > 100 s left
    assert exc.value.partial == [{"orderId": "x"}]


def test_page_wait_paces_across_calls_and_allows_no_deadline(monkeypatch):
    slept = []
    now = 5000.0
    monkeypatch.setattr(fetch_orders.time, "monotonic", lambda: now)
    monkeypatch.setattr(fetch_orders.time, "sleep", slept.append)
    monkeypatch.setattr(fetch_orders, "_last_request_at", now - 30)
    fetch_orders._wait_for_next_page(None, [])
    assert slept == [fetch_orders.PAGE_PACING_SECONDS - 30]
    monkeypatch.setattr(fetch_orders, "_last_request_at", None)           # very first request: no wait
    slept.clear()
    fetch_orders._wait_for_next_page(now + 100, [])
    assert slept == []


def test_fetch_orders_by_update_sends_before_only_when_given(monkeypatch):
    seen = []
    monkeypatch.setattr(fetch_orders, "_paginate", lambda params, deadline=None: seen.append(params) or [])
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
