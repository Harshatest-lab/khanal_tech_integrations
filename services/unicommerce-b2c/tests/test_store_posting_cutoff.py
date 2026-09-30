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


class _RecordingCursor:
    """Records every execute() call (SQL + params) in order, unlike
    _FakeCursor which only remembers the last one -- delete_pre_cutoff_
    ready_orders issues a SELECT then up to two DELETEs on the same
    cursor, and the test needs to see all three."""

    def __init__(self, cols, select_rows):
        self.description = [_Desc(c) for c in cols]
        self.calls: list[tuple[str, object]] = []
        self._select_rows = select_rows

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self._select_rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RecordingConn:
    def __init__(self, cols, select_rows):
        self._cursor = _RecordingCursor(cols, select_rows)
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


def test_get_ready_orders_applies_cutoff_as_hard_floor():
    conn = _FakeConn(["uniware_id", "channel_name", "order_date", "order_json", "attempt_count"])
    store.get_ready_orders(conn, limit=10, cutoff_date="2026-08-01")
    cur = conn._cursor
    # The floor is on the invoice date = IST ship date, not the order date.
    assert f"{store.DOC_DATE_SQL} >= %s" in cur.last_sql
    assert "order_date >= %s" not in cur.last_sql
    assert cur.last_params[0] == "2026-08-01"


def test_get_ready_orders_requires_ship_date_and_locks_on_ship_month():
    conn = _FakeConn(["uniware_id", "channel_name", "order_date", "doc_date", "order_json", "attempt_count"])
    store.get_ready_orders(conn, limit=10, cutoff_date="2026-08-01")
    sql = conn._cursor.last_sql
    assert "shipment_date IS NOT NULL" in sql
    assert f"to_char({store.DOC_DATE_SQL}, 'YYYY-MM')" in sql  # period lock keys off ship month
    assert "AS doc_date" in sql  # row carries the date the invoice will be posted on
    assert "Asia/Kolkata" in store.DOC_DATE_SQL  # IST, never the DB session zone


def test_claim_ready_order_refuses_unshipped_and_locks_on_ship_month():
    conn = _FakeConn([])
    conn._cursor.rowcount = 0
    conn.commit = lambda: None
    assert store.claim_ready_order(conn, "UC-1") is False
    sql = conn._cursor.last_sql
    assert "shipment_date IS NOT NULL" in sql
    assert f"to_char({store.DOC_DATE_SQL}, 'YYYY-MM')" in sql


def test_get_ready_orders_cutoff_combines_with_optional_filters():
    conn = _FakeConn(["uniware_id", "channel_name", "order_date", "order_json", "attempt_count"])
    store.get_ready_orders(conn, limit=10, cutoff_date="2026-08-01", channel_name="MEESHO")
    cur = conn._cursor
    assert f"{store.DOC_DATE_SQL} >= %s" in cur.last_sql
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


def test_delete_pre_cutoff_ready_orders_deletes_lines_then_order_and_commits():
    """Must delete the companion unicommerce_order_lines rows before the
    unicommerce_orders row itself (same order upsert_order's own refresh
    uses), scoped by pipeline_status='ready' AND order_date < cutoff on
    both the select and the final delete, and commit exactly once."""
    fake_row = ("UC-OLD", "FLIPKART", "ready", "2026-06-04")
    conn = _RecordingConn(["uniware_id", "channel_name", "pipeline_status", "order_date"], [fake_row])

    result = store.delete_pre_cutoff_ready_orders(conn, "2026-08-01")

    assert result == [{"uniware_id": "UC-OLD", "channel_name": "FLIPKART", "pipeline_status": "ready", "order_date": "2026-06-04"}]
    calls = conn._cursor.calls
    assert len(calls) == 3

    select_sql, select_params = calls[0]
    assert "pipeline_status = 'ready'" in select_sql
    assert "order_date < %s" in select_sql
    assert select_params == ("2026-08-01",)

    lines_sql, lines_params = calls[1]
    assert "unicommerce_order_lines" in lines_sql
    assert lines_params == (["UC-OLD"],)

    orders_sql, orders_params = calls[2]
    assert "unicommerce_orders" in orders_sql
    assert "pipeline_status = 'ready'" in orders_sql
    assert orders_params == ("2026-08-01",)

    assert conn.committed is True


def test_delete_pre_cutoff_ready_orders_skips_deletes_when_nothing_found():
    """No rows matched -> must not issue either DELETE (the ids=[] guard),
    but still commits (a no-op transaction is harmless and keeps the
    connection state consistent with the found-rows path)."""
    conn = _RecordingConn(["uniware_id", "channel_name", "pipeline_status", "order_date"], [])

    result = store.delete_pre_cutoff_ready_orders(conn, "2026-08-01")

    assert result == []
    assert len(conn._cursor.calls) == 1
    assert conn.committed is True
