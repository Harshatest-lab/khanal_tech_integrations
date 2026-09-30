"""Re-queueing failed_permanent credit notes: exactly the named refund ids,
only from 'failed_permanent', then post only those events."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import handlers  # noqa: E402
from amazon_us import post_credit_notes, store  # noqa: E402
from amazon_us.finances_api import RefundEvent  # noqa: E402


class _Cur:
    def __init__(self, returning):
        self.returning = returning
        self.calls: list = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return [(r,) for r in self.returning]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, returning=()):
        self.cur = _Cur(list(returning))
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True


def test_requeue_only_moves_failed_permanent_rows_by_explicit_id():
    conn = _Conn(["r1", "r2"])
    moved = store.requeue_failed_permanent_cns(conn, ["r1", "r2", "r3"])
    assert moved == ["r1", "r2"]
    sql, params = conn.cur.calls[0]
    assert "status = 'failed_permanent'" in sql  # never any other status
    assert "refund_id = ANY(%s)" in sql  # never a sweep
    assert params == (["r1", "r2", "r3"],)
    assert conn.committed is True


def test_requeue_with_no_ids_does_nothing():
    conn = _Conn()
    assert store.requeue_failed_permanent_cns(conn, []) == []
    assert conn.cur.calls == []


def _event(rid):
    return RefundEvent(refund_id=rid, amazon_order_id=rid.split("-2026")[0], posted_date="2026-09-09")


def test_post_refunds_only_processes_the_requested_refund_ids(monkeypatch):
    events = [_event("111-1-1-2026-09-09T01:00:00Z"), _event("222-2-2-2026-09-09T02:00:00Z")]
    seen = []
    monkeypatch.setattr(post_credit_notes.store, "start_sync_log", lambda *a, **k: 1)
    monkeypatch.setattr(post_credit_notes.store, "finish_sync_log", lambda *a, **k: None)
    monkeypatch.setattr(post_credit_notes.store, "get_sku_mapping", lambda conn: {})
    monkeypatch.setattr(post_credit_notes.finances_api, "list_refund_events", lambda a, b=None: events)
    monkeypatch.setattr(
        post_credit_notes, "_process_event", lambda conn, e, m, d: seen.append(e.refund_id) or "posted"
    )
    out = post_credit_notes.post_refunds("c", "2026-09-01T00:00:00Z", only_refund_ids={events[1].refund_id})
    assert seen == [events[1].refund_id]
    assert out["total"] == 1 and out["posted"] == 1

    # Default (no filter) is unchanged: every event in the window.
    seen.clear()
    post_credit_notes.post_refunds("c", "2026-09-01T00:00:00Z")
    assert len(seen) == 2


class _Ctx:
    def __enter__(self):
        return "conn"

    def __exit__(self, *a):
        return False


def test_handler_requeues_then_posts_only_the_requeued(monkeypatch):
    calls = {}
    monkeypatch.setattr(handlers.store, "connect", lambda: _Ctx())
    monkeypatch.setattr(handlers.store, "requeue_failed_permanent_cns", lambda conn, ids: ["a"])

    def fake_post(conn, after, before=None, dry_run=False, only_refund_ids=None):
        calls.update(after=after, dry_run=dry_run, only=only_refund_ids)
        return {"status": "done", "posted": 1}

    monkeypatch.setattr(handlers.post_credit_notes, "post_refunds", fake_post)
    out = handlers.post_credit_notes_handler(
        {"mode": "requeue_failed_permanent_and_post", "refund_ids": ["a", "b"], "posted_after": "2026-09-01T00:00:00Z"},
        None,
    )
    assert out["requested"] == 2 and out["requeued"] == ["a"]
    assert calls["only"] == {"a"} and calls["dry_run"] is False  # 'b' wasn't requeued -> never posted


def test_handler_dry_run_never_requeues(monkeypatch):
    monkeypatch.setattr(handlers.store, "connect", lambda: _Ctx())
    monkeypatch.setattr(
        handlers.store, "requeue_failed_permanent_cns", lambda *a: (_ for _ in ()).throw(AssertionError("wrote"))
    )
    seen = {}
    monkeypatch.setattr(
        handlers.post_credit_notes, "post_refunds",
        lambda conn, after, before=None, dry_run=False, only_refund_ids=None: seen.update(d=dry_run, o=only_refund_ids) or {},
    )
    handlers.post_credit_notes_handler(
        {"mode": "requeue_failed_permanent_and_post", "refund_ids": ["a"], "posted_after": "x", "dry_run": True}, None
    )
    assert seen == {"d": True, "o": {"a"}}


def test_handler_requires_ids_and_window():
    out = handlers.post_credit_notes_handler({"mode": "requeue_failed_permanent_and_post", "refund_ids": []}, None)
    assert out["status"] == "error"
