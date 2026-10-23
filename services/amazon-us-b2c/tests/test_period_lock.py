"""Posting-period lock: a locked month can never be claimed for posting, and credit notes in it are held."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from amazon_us import post_credit_notes, store  # noqa: E402
from amazon_us.finances_api import RefundEvent  # noqa: E402


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.conn.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return (1,) if self.conn.locked else None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, locked=False):
        self.locked = locked
        self.calls: list = []

    def cursor(self):
        return _Cur(self)

    def commit(self):
        pass


def test_is_period_locked_checks_the_month_of_the_document_date():
    conn = _Conn(locked=True)
    assert store.is_period_locked(conn, "2026-09-25") is True
    assert conn.calls[0][1] == ("2026-09",)
    assert store.is_period_locked(_Conn(locked=False), "2026-10-01") is False


def test_every_route_to_posting_is_gated_by_the_lock():
    conn = _Conn()
    store.claim(conn, "111-1111111-1111111", "ready", "posting")
    sql = conn.calls[0][0]
    assert "amazon_us_period_locks" in sql and "l.locked" in sql and "actual_ship_date" in sql


def test_the_batch_listing_does_not_offer_locked_orders():
    conn = _Conn()
    store.list_ready_orders(conn, "2026-06-01", 10)
    assert "amazon_us_period_locks" in conn.calls[0][0]


def test_lock_holds_ready_orders_and_failed_credit_notes_and_unlock_releases_them():
    conn = _Conn()
    out = store.lock_period(conn, "2026-09", "ops@x.com", "closed")
    assert out["locked"] is True and out["orders_held"] == 1 and out["credit_notes_held"] == 1
    sqls = [c[0] for c in conn.calls]
    assert any("SET stage = 'period_locked'" in s and "stage = 'ready'" in s for s in sqls)
    assert any("SET status = 'period_locked'" in s and "status = 'failed'" in s for s in sqls)

    conn = _Conn()
    store.unlock_period(conn, "2026-09", "ops@x.com")
    sqls = [c[0] for c in conn.calls]
    assert any("SET stage = 'ready'" in s and "stage = 'period_locked'" in s for s in sqls)
    assert any("SET status = 'failed'" in s and "status = 'period_locked'" in s for s in sqls)


def _event(posted="2026-09-25"):
    return RefundEvent(refund_id="r1", amazon_order_id="111-1111111-1111111", posted_date=posted, item_adjustments=[])


def test_credit_note_in_a_locked_month_is_held_not_posted(monkeypatch):
    saved = []
    monkeypatch.setattr(store, "is_period_locked", lambda conn, d: True)
    monkeypatch.setattr(store, "get_cn_log", lambda conn, rid: None)
    monkeypatch.setattr(store, "upsert_cn_log", lambda *a, **k: saved.append((a, k)))
    monkeypatch.setattr(store, "claim_cn", lambda *a: (_ for _ in ()).throw(AssertionError("must not claim")))
    assert post_credit_notes._process_event(object(), _event(), {}, dry_run=False) == "skipped"
    assert saved[0][0][4] == "period_locked"


def test_a_posted_credit_note_keeps_its_status_when_its_month_is_locked(monkeypatch):
    saved = []
    monkeypatch.setattr(store, "is_period_locked", lambda conn, d: True)
    monkeypatch.setattr(store, "get_cn_log", lambda conn, rid: {"status": "posted"})
    monkeypatch.setattr(store, "upsert_cn_log", lambda *a, **k: saved.append((a, k)))
    assert post_credit_notes._process_event(object(), _event(), {}, dry_run=False) == "skipped"
    assert saved == []  # an amendment is blocked too, but the posted row is not overwritten


def test_dry_run_writes_nothing_for_a_locked_month(monkeypatch):
    saved = []
    monkeypatch.setattr(store, "is_period_locked", lambda conn, d: True)
    monkeypatch.setattr(store, "get_cn_log", lambda conn, rid: None)
    monkeypatch.setattr(store, "upsert_cn_log", lambda *a, **k: saved.append(1))
    assert post_credit_notes._process_event(object(), _event(), {}, dry_run=True) == "skipped"
    assert saved == []
