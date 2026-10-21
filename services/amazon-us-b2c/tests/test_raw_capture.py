"""Raw Amazon data and SAP exchanges are kept, and saving them can never break a sync or a posting."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from amazon_us import finances_api, report_parser, sap_client, store  # noqa: E402


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self.conn.fail:
            raise RuntimeError("db down")
        self.conn.executed.append((" ".join(sql.split()), params))


class FakeConn:
    def __init__(self, fail=False):
        self.fail = fail
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_save_raw_stores_the_payload_keyed_by_content_hash():
    conn = FakeConn()
    store.save_raw(conn, "order", "111-1", {"b": 2, "a": 1})
    store.save_raw(conn, "order", "111-1", {"a": 1, "b": 2})  # same content, different key order
    (sql1, p1), (_, p2) = conn.executed
    assert "ON CONFLICT (source, ref, payload_hash) DO NOTHING" in sql1
    assert p1[:2] == ("order", "111-1") and p1[2] == p2[2]  # identical hash, so the second is a no-op row-wise
    assert json.loads(p1[3]) == {"a": 1, "b": 2}


def test_save_raw_ignores_empty_payloads():
    conn = FakeConn()
    store.save_raw(conn, "order_items", "111-1", [])
    store.save_raw(conn, "order", "111-1", None)
    assert conn.executed == []


def test_save_raw_never_raises_when_the_database_fails():
    conn = FakeConn(fail=True)
    store.save_raw(conn, "order", "111-1", {"a": 1})  # must not raise
    assert conn.rollbacks == 1


def test_credit_note_log_writes_an_attempt_row_each_time():
    conn = FakeConn()
    store.upsert_cn_log(conn, "R1", "111-1", "2026-10-01", "failed", error="boom", payload={"p": 1}, response={"error": "x"})
    inserts = [sql for sql, _ in conn.executed if "INSERT INTO amazon_us_cn_attempts" in sql]
    assert len(inserts) == 1
    attempt = [p for sql, p in conn.executed if "amazon_us_cn_attempts" in sql][0]
    assert attempt[:3] == ("R1", "111-1", "failed") and json.loads(attempt[5]) == {"p": 1} and attempt[7] == "boom"


def test_sap_rejection_carries_what_was_sent_and_the_answer(monkeypatch):
    def reject(method, endpoint, body=None):
        return 400, {"error": {"message": {"value": "bad item"}}}

    monkeypatch.setattr(sap_client, "_request", reject)
    try:
        sap_client.post_invoice({"CardCode": "C1"})
    except sap_client.SapPostError as e:
        assert e.payload == {"CardCode": "C1"}
        assert sap_client.error_response(e) == {"error": {"error": {"message": {"value": "bad item"}}}}
    else:
        raise AssertionError("expected SapPostError")


def test_error_response_is_none_for_non_rejections():
    assert sap_client.error_response(RuntimeError("network")) is None


def test_rows_by_order_keeps_every_column_grouped_per_order():
    tsv = "amazon-order-id\tsku\tship-city\tweird-new-column\n111-1\tA\tNYC\tx\n111-1\tB\tNYC\ty\n222-2\tC\tLA\tz\n\t\t\t\n"
    grouped = report_parser.rows_by_order(tsv)
    assert set(grouped) == {"111-1", "222-2"}
    assert [r["sku"] for r in grouped["111-1"]] == ["A", "B"] and grouped["111-1"][0]["weird-new-column"] == "x"


def test_refund_event_keeps_amazons_raw_json():
    raw = {
        "AmazonOrderId": "111-1", "PostedDate": "2026-10-01T03:00:00Z",
        "ShipmentItemAdjustmentList": [{"SellerSKU": "A", "ItemChargeAdjustmentList": [{"ChargeType": "Principal", "ChargeAmount": {"CurrencyAmount": -5}}]}],
    }
    event = finances_api._parse_refund_event(raw)
    assert event.raw is raw


def test_skipped_credit_note_attempt_keeps_the_reason():
    conn = FakeConn()
    store.upsert_cn_log(conn, "R1", "111-1", "2026-10-01", "skipped", skip_reason="no postable lines: SKU-X")
    attempt = [p for sql, p in conn.executed if "amazon_us_cn_attempts" in sql][0]
    assert attempt[2] == "skipped" and attempt[7] == "no postable lines: SKU-X"
