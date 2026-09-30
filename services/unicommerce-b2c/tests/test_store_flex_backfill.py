"""store.backfill_flex_shipment_dates: dry run by default, only pre-posting
Flex rows, only fills NULLs (never a 'posted' row, never overwrites)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import store  # noqa: E402


class _Cur:
    def __init__(self, rows):
        self._rows = rows
        self.calls: list[tuple[str, object]] = []
        self.rowcount = len(rows)

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self.cur = _Cur(rows)
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True


_ROWS = [("FLEX-1", "ready", "2026-09-01", "2026-09-03T08:00:00+00:00")]


def test_backfill_is_dry_run_by_default_and_never_writes():
    conn = _Conn(_ROWS)
    out = store.backfill_flex_shipment_dates(conn)
    assert out["dry_run"] is True and out["matched"] == 1 and out["updated"] == 0
    assert out["orders"][0]["uniware_id"] == "FLEX-1"
    assert len(conn.cur.calls) == 1  # just the SELECT
    assert conn.committed is False


def test_backfill_apply_updates_only_null_shipment_flex_rows_not_posted():
    conn = _Conn(_ROWS)
    out = store.backfill_flex_shipment_dates(conn, dry_run=False)
    assert out["updated"] == 1 and conn.committed is True
    update_sql, params = conn.cur.calls[-1]
    assert update_sql.lstrip().startswith("UPDATE unicommerce_orders SET shipment_date")
    assert "channel_name = 'AMAZON_FLEX'" in update_sql
    assert "shipment_date IS NULL" in update_sql  # fills NULLs, never overwrites
    assert "posted" not in params[0]  # an already-posted row is never in scope
    assert "fetched" in params[0] and "ready" in params[0]


def test_backfill_only_casts_iso_formatted_updated_values():
    # A non-ISO `updated` (epoch-millis) would make ::timestamptz abort the
    # whole UPDATE; the WHERE must exclude it before the cast runs.
    conn = _Conn(_ROWS)
    store.backfill_flex_shipment_dates(conn, dry_run=False)
    for sql, _ in conn.cur.calls:
        assert "~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'" in sql


def test_backfill_apply_with_no_matches_does_not_update():
    conn = _Conn([])
    out = store.backfill_flex_shipment_dates(conn, dry_run=False)
    assert out["matched"] == 0 and out["updated"] == 0
    assert len(conn.cur.calls) == 1
