"""Saving SAP's reply is a debugging copy: it must be written when possible and must NEVER raise, because it
runs right after SAP accepted a document (the columns come from a manual DDL step and may not exist yet)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import store  # noqa: E402


class _Conn:
    def __init__(self, fail=False):
        self.fail, self.sql, self.params, self.committed, self.rolled_back = fail, None, None, False, False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        if self.fail:
            raise RuntimeError('column "sap_response_json" does not exist')
        self.sql, self.params = sql, params

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_order_reply_is_saved_as_json():
    conn = _Conn()
    store.save_order_sap_response(conn, "SO-1", {"DocEntry": 7})
    assert "sap_response_json" in conn.sql and conn.committed
    assert json.loads(conn.params[0]) == {"DocEntry": 7} and conn.params[1] == "SO-1"


def test_credit_note_request_and_reply_are_saved_together():
    conn = _Conn()
    store.save_cn_sap_exchange(conn, "RET-1", {"CardCode": "C1"}, {"DocEntry": 9})
    assert json.loads(conn.params[0]) == {"CardCode": "C1"} and json.loads(conn.params[1]) == {"DocEntry": 9}
    assert conn.params[2] == "RET-1"


def test_nothing_to_save_does_not_touch_the_database():
    conn = _Conn()
    store.save_order_sap_response(conn, "SO-1", None)
    store.save_cn_sap_exchange(conn, "RET-1", None, None)
    assert conn.sql is None


def test_a_missing_column_is_swallowed_and_rolled_back():
    conn = _Conn(fail=True)
    store.save_order_sap_response(conn, "SO-1", {"DocEntry": 7})  # must not raise
    store.save_cn_sap_exchange(conn, "RET-1", {"a": 1}, {"b": 2})
    assert conn.rolled_back and not conn.committed


def test_attempt_is_appended_with_request_response_and_error():
    conn = _Conn()
    store.record_attempt(conn, "invoice", "SO-1", "posted", request={"a": 1}, response={"DocEntry": 7}, sap_doc_entry="7")
    assert conn.sql.startswith("INSERT INTO unicommerce_posting_attempts") and conn.committed
    doc_type, doc_id, outcome, entry, request, response, error = conn.params
    assert (doc_type, doc_id, outcome, entry, error) == ("invoice", "SO-1", "posted", "7", None)
    assert json.loads(request) == {"a": 1} and json.loads(response) == {"DocEntry": 7}


def test_a_missing_attempts_table_never_breaks_a_posting():
    conn = _Conn(fail=True)
    store.record_attempt(conn, "credit_note", "RET-1", "failed", error="boom")  # must not raise
    assert conn.rolled_back
