"""Tests for store.get_claimable_pogrn_doc_entries's to_warehouses filter --
the mechanism behind pogrn.ENABLED_TO_WAREHOUSES's temporary EC-FG-only gate
(2026-08-20: AMZ-* facilities blocked on a UC-side vendor-facility link)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import store  # noqa: E402


class _FakeCursor:
    def __init__(self):
        self.last_sql = None
        self.last_params = None

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self):
        self._cursor = _FakeCursor()

    def cursor(self):
        return self._cursor


def test_no_filter_omits_to_warehouse_clause():
    conn = _FakeConn()
    store.get_claimable_pogrn_doc_entries(conn, limit=10)
    cur = conn._cursor
    assert "to_warehouse" not in cur.last_sql
    assert cur.last_params == (10,)


def test_to_warehouses_filter_adds_any_clause():
    conn = _FakeConn()
    store.get_claimable_pogrn_doc_entries(conn, limit=10, to_warehouses={"EC-FG"})
    cur = conn._cursor
    assert "to_warehouse = ANY(%s)" in cur.last_sql
    assert cur.last_params == (["EC-FG"], 10)


def test_empty_set_behaves_like_no_filter():
    conn = _FakeConn()
    store.get_claimable_pogrn_doc_entries(conn, limit=10, to_warehouses=set())
    cur = conn._cursor
    assert "to_warehouse" not in cur.last_sql
