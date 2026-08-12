"""Tests for the hard posting-cutoff floor on store.get_ready_orders /
store.get_pending_credit_notes: cutoff_date is a required parameter, not
an optional filter, and must always appear in the WHERE clause with its
value as the first bound param -- so no caller can accidentally sweep in
a pre-cutoff order/CN by omitting a date range (see
unicommerce.constants.POSTING_CUTOFF_DATE)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import store  # noqa: E402


class _Desc:
    def __init__(self, name):
        self.name = name


class _FakeCursor:
    def __init__(self, cols):
        self.description = [_Desc(c) for c in cols]
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
    def __init__(self, cols):
        self._cursor = _FakeCursor(cols)

    def cursor(self):
        return self._cursor


def test_get_ready_orders_applies_cutoff_as_hard_floor():
    conn = _FakeConn(["uniware_id", "channel_name", "order_date", "order_json", "attempt_count"])
    store.get_ready_orders(conn, limit=10, cutoff_date="2026-08-01")
    cur = conn._cursor
    assert "order_date >= %s" in cur.last_sql
    assert cur.last_params[0] == "2026-08-01"


def test_get_ready_orders_cutoff_combines_with_optional_filters():
    conn = _FakeConn(["uniware_id", "channel_name", "order_date", "order_json", "attempt_count"])
    store.get_ready_orders(conn, limit=10, cutoff_date="2026-08-01", channel_name="MEESHO")
    cur = conn._cursor
    assert "order_date >= %s" in cur.last_sql
    assert "channel_name = %s" in cur.last_sql
    assert cur.last_params[0] == "2026-08-01"
    assert "MEESHO" in cur.last_params


def test_get_pending_credit_notes_applies_cutoff_as_hard_floor():
    conn = _FakeConn(["uc_return_id", "uc_order_id", "attempt_count", "sap_cn_docentry"])
    store.get_pending_credit_notes(conn, limit=10, cutoff_date="2026-08-01")
    cur = conn._cursor
    assert "o.order_date >= %s" in cur.last_sql
    assert cur.last_params[0] == "2026-08-01"


def test_get_pending_credit_notes_cutoff_survives_explicit_date_range():
    # Caller asking for an earlier start_date must not bypass the cutoff --
    # both clauses apply (AND), so the effective floor is max(start_date, cutoff).
    conn = _FakeConn(["uc_return_id", "uc_order_id", "attempt_count", "sap_cn_docentry"])
    store.get_pending_credit_notes(conn, limit=10, cutoff_date="2026-08-01", start_date="2026-05-01")
    cur = conn._cursor
    assert "o.order_date >= %s" in cur.last_sql
    assert "o.order_date >= %s" in cur.last_sql or "o.order_date BETWEEN %s AND %s" in cur.last_sql
    assert cur.last_params[0] == "2026-08-01"
    assert "2026-05-01" in cur.last_params
