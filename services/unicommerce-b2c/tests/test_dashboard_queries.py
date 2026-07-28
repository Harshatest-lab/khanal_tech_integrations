"""Tests for the dashboard query helpers (clamps + envelope shape).

The SQL itself is exercised live against the dev DB; these lock the
input-sanitizing and result-shaping that don't need a real connection."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import dashboard_queries as dq  # noqa: E402


def test_clamp_limit_default_and_bounds():
    assert dq._clamp_limit(None) == dq.DEFAULT_LIMIT
    assert dq._clamp_limit("abc") == dq.DEFAULT_LIMIT
    assert dq._clamp_limit("0") == 1           # floored to 1
    assert dq._clamp_limit("5000") == dq.MAX_LIMIT
    assert dq._clamp_limit("25") == 25
    assert dq._clamp_limit(None, default=20) == 20


def test_clamp_offset_never_negative():
    assert dq._clamp_offset(None) == 0
    assert dq._clamp_offset("-5") == 0
    assert dq._clamp_offset("bad") == 0
    assert dq._clamp_offset("40") == 40


class _FakeCursor:
    """Replays a queued list of (description_cols, rows) per execute()."""

    def __init__(self, script):
        self._script = list(script)
        self.description = None
        self._rows = []

    def execute(self, *a, **k):
        cols, rows = self._script.pop(0)
        self.description = [(c,) for c in cols] if cols is not None else None
        self._rows = rows

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, script):
        self._script = script

    def cursor(self):
        return _FakeCursor(self._script)


def test_fetch_orders_envelope_and_row_mapping():
    script = [
        ([None], [(2,)]),  # COUNT(*)
        (
            ["uniware_id", "channel_name", "pipeline_status"],
            [("A", "FLIPKART", "fetched"), ("B", "CRED", "ready")],
        ),
    ]
    out = dq.fetch_orders(_FakeConn(script), limit=50, offset=0)
    assert out["total"] == 2
    assert out["limit"] == 50 and out["offset"] == 0
    assert out["items"][0] == {"uniware_id": "A", "channel_name": "FLIPKART", "pipeline_status": "fetched"}


def test_fetch_returns_envelope():
    script = [
        ([None], [(1,)]),
        (["uc_return_id", "status"], [("RP0584", "pending")]),
    ]
    out = dq.fetch_returns(_FakeConn(script), limit=50)
    assert out["total"] == 1
    assert out["items"] == [{"uc_return_id": "RP0584", "status": "pending"}]


def test_fetch_order_detail_missing_header_returns_none_header():
    script = [(["uniware_id"], [])]  # header query returns no rows
    out = dq.fetch_order_detail(_FakeConn(script), "nope")
    assert out["header"] is None
    assert out["lines"] == []
    assert out["credit_notes"] == []


def test_fetch_recon_gaps_includes_mismatch_reason():
    script = [
        (
            ["uniware_id", "channel_name", "ar_invoice_tax_diff", "ar_invoice_mismatch_reason"],
            [("A", "FLIPKART", 1.5, "line 2: qty mismatch (expected 3, SAP shows 2)")],
        ),
    ]
    out = dq.fetch_recon_gaps(_FakeConn(script), limit=50)
    assert out["items"][0]["ar_invoice_mismatch_reason"] == "line 2: qty mismatch (expected 3, SAP shows 2)"
