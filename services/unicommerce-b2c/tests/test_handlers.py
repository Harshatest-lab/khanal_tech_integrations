"""Tests for the dashboard API's retry-invoices/retry-cn hand-off.

Batch retries are async-invoked against the scheduled Lambda (900s budget)
instead of running inline behind httpApi's 30s cap -- these lock the
invoke payload/shape and confirm the single-order path still runs
synchronously, untouched."""

from __future__ import annotations

import datetime
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import handlers  # noqa: E402


class _FakeConnCtx:
    def __enter__(self):
        return "fake-conn"

    def __exit__(self, *a):
        return False


class _FakeLambdaClient:
    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return {"StatusCode": 202}


def _post_event(path: str, query: dict | None = None) -> dict:
    return {
        "requestContext": {"http": {"method": "POST"}},
        "rawPath": path,
        "queryStringParameters": query or {},
    }


def _get_event(path: str, query: dict | None = None) -> dict:
    return {
        "requestContext": {"http": {"method": "GET"}},
        "rawPath": path,
        "queryStringParameters": query or {},
    }


def test_orders_route_passes_exclude_cancelled_and_status_to_query(monkeypatch):
    seen = {}

    def fake_fetch_orders(conn, **kwargs):
        seen.update(kwargs)
        return {"total": 0, "items": [], "limit": kwargs["limit"], "offset": kwargs["offset"]}

    monkeypatch.setattr(handlers.store, "connect", lambda: _FakeConnCtx())
    monkeypatch.setattr(handlers.dashboard_queries, "fetch_orders", fake_fetch_orders)

    resp = handlers.dashboard_handler(
        _get_event("/v1/unicommerce/orders", {"exclude_cancelled": "true", "status": "CANCELLED"}), None
    )
    assert resp["statusCode"] == 200
    assert seen["exclude_cancelled"] is True
    assert seen["order_status"] == "CANCELLED"

    # Absent/false params keep the old behaviour (no filtering).
    seen.clear()
    handlers.dashboard_handler(_get_event("/v1/unicommerce/orders", {"exclude_cancelled": "0"}), None)
    assert seen["exclude_cancelled"] is False
    assert seen["order_status"] is None


def test_invoke_retry_batch_invokes_correct_function_with_manual_retry_trigger(monkeypatch):
    fake_client = _FakeLambdaClient()
    monkeypatch.setattr(handlers, "_lambda_client", fake_client)
    monkeypatch.setenv("POST_INVOICES_FUNCTION_ARN", "arn:aws:lambda:ap-south-1:123:function:postInvoices")

    result = handlers._invoke_retry_batch("POST_INVOICES_FUNCTION_ARN", {"limit": "30", "start_date": "2026-07-01"})

    assert result == {
        "accepted": True,
        "trigger": "manual_retry",
        "limit": 30,
        "start_date": "2026-07-01",
        "end_date": None,
    }
    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["FunctionName"] == "arn:aws:lambda:ap-south-1:123:function:postInvoices"
    assert call["InvocationType"] == "Event"
    payload = json.loads(call["Payload"])
    assert payload == {"trigger": "manual_retry", "limit": 30, "start_date": "2026-07-01", "end_date": None}


def test_retry_invoices_batch_returns_202_and_invokes_async(monkeypatch):
    fake_client = _FakeLambdaClient()
    monkeypatch.setattr(handlers, "_lambda_client", fake_client)
    monkeypatch.setenv("POST_INVOICES_FUNCTION_ARN", "arn:fn:post-invoices")

    resp = handlers.dashboard_handler(_post_event("/v1/unicommerce/retry-invoices", {"limit": "20"}), None)

    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    assert body["accepted"] is True
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["FunctionName"] == "arn:fn:post-invoices"


def test_retry_cn_batch_returns_202_and_invokes_async(monkeypatch):
    fake_client = _FakeLambdaClient()
    monkeypatch.setattr(handlers, "_lambda_client", fake_client)
    monkeypatch.setenv("POST_CREDIT_NOTES_FUNCTION_ARN", "arn:fn:post-credit-notes")

    resp = handlers.dashboard_handler(_post_event("/v1/unicommerce/retry-cn", {"limit": "20"}), None)

    assert resp["statusCode"] == 202
    assert fake_client.calls[0]["FunctionName"] == "arn:fn:post-credit-notes"


def test_retry_invoices_single_order_stays_synchronous(monkeypatch):
    fake_client = _FakeLambdaClient()
    monkeypatch.setattr(handlers, "_lambda_client", fake_client)
    monkeypatch.setattr(handlers.store, "connect", lambda: _FakeConnCtx())
    monkeypatch.setattr(handlers.post_invoices, "post_one_order", lambda conn, uid: {"uniware_id": uid, "result": "posted"})

    resp = handlers.dashboard_handler(_post_event("/v1/unicommerce/retry-invoices", {"uniware_id": "UC123"}), None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"uniware_id": "UC123", "result": "posted"}
    assert fake_client.calls == []  # single-order path never touches the async invoke


def test_retry_cn_single_return_stays_synchronous(monkeypatch):
    fake_client = _FakeLambdaClient()
    monkeypatch.setattr(handlers, "_lambda_client", fake_client)
    monkeypatch.setattr(handlers.store, "connect", lambda: _FakeConnCtx())
    monkeypatch.setattr(
        handlers.credit_notes, "post_one_credit_note", lambda conn, rid: {"uc_return_id": rid, "result": "posted"}
    )

    resp = handlers.dashboard_handler(_post_event("/v1/unicommerce/retry-cn", {"uc_return_id": "RP0584"}), None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"uc_return_id": "RP0584", "result": "posted"}
    assert fake_client.calls == []


def test_run_coerces_date_datetime_decimal_so_lambda_can_marshal_the_response():
    """Confirmed real 2026-08-25: a handler returning raw DB rows (date
    columns included) crashed at the Lambda runtime's JSON-marshal step
    -- AFTER the handler's own work (a DB delete) had already committed,
    losing the caller's only copy of the response until it was recovered
    from this function's own log line. _run must make every return value
    JSON-safe, not just handlers that happen to remember to do it
    themselves."""
    result = handlers._run(
        "test_label",
        lambda: {
            "a_date": datetime.date(2026, 7, 28),
            "a_datetime": datetime.datetime(2026, 8, 25, 11, 10, 46, tzinfo=datetime.timezone.utc),
            "a_decimal": Decimal("12.50"),
            "nested": {"list": [datetime.date(2026, 1, 1)]},
        },
    )

    # Every value must now be a plain JSON-serializable type -- json.dumps
    # on the result itself must not raise.
    json.dumps(result)
    assert result["a_date"] == "2026-07-28"
    assert result["a_decimal"] == "12.50"
    assert result["nested"]["list"] == ["2026-01-01"]
