"""Regression test for store.get_cn's jsonb handling.

psycopg returns jsonb columns already parsed (list/dict), not JSON
strings -- get_cn used to json.loads(items_json) unconditionally, which
crashed live 2026-07-24 with "the JSON object must be str, bytes or
bytearray, not list" the moment postCreditNotes processed a real staged
return. Third instance of this jsonb-vs-string pattern in the port."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import store  # noqa: E402

_COLS = [
    "uc_return_id", "uc_order_id", "parent_sap_doc_entry", "return_date",
    "items_json", "status", "sap_cn_docentry", "attempt_count",
]


class _Desc:
    def __init__(self, name):
        self.name = name


class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.description = [_Desc(c) for c in _COLS]

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _FakeCursor(self._row)


def test_get_cn_handles_jsonb_items_as_list():
    # items_json comes back from psycopg as an already-parsed list.
    row = ("RP-1", "SO-1", "9001", "2026-07-01", [{"itemSku": "A"}], "pending", None, 0)
    result = store.get_cn(_FakeConn(row), "RP-1")
    assert result["items"] == [{"itemSku": "A"}]
    assert "items_json" not in result


def test_get_cn_still_handles_json_string_items():
    row = ("RP-1", "SO-1", "9001", "2026-07-01", '[{"itemSku": "B"}]', "pending", None, 0)
    result = store.get_cn(_FakeConn(row), "RP-1")
    assert result["items"] == [{"itemSku": "B"}]


def test_get_cn_none_items_becomes_empty_list():
    row = ("RP-1", "SO-1", "9001", "2026-07-01", None, "pending", None, 0)
    result = store.get_cn(_FakeConn(row), "RP-1")
    assert result["items"] == []


def test_get_cn_missing_row_returns_none():
    assert store.get_cn(_FakeConn(None), "nope") is None
