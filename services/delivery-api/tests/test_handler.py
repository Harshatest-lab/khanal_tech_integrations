import json
from unittest.mock import MagicMock, patch

import pytest

from src.delhivery_models import normalize_pod_webhook
from src.delhivery_webhook_ingest import handler as ingest_handler


def test_ingest_missing_lrnum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELHIVERY_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("DELHIVERY_WEBHOOK_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/queue")
    mock_sqs = MagicMock()
    with patch("src.delhivery_webhook_ingest._sqs", return_value=mock_sqs):
        resp = ingest_handler(
            {
                "headers": {},
                "rawPath": "/v1/webhooks/delhivery/shipment-status",
                "body": '{"status": "DELIVERED"}',
            },
            object(),
        )
    assert resp["statusCode"] == 400
    mock_sqs.send_message.assert_not_called()


def test_ingest_wrong_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELHIVERY_WEBHOOK_SECRET", "correct-secret")
    monkeypatch.setenv("DELHIVERY_WEBHOOK_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/queue")
    mock_sqs = MagicMock()
    with patch("src.delhivery_webhook_ingest._sqs", return_value=mock_sqs):
        resp = ingest_handler(
            {
                "headers": {"x-delhivery-webhook-secret": "wrong"},
                "rawPath": "/v1/webhooks/delhivery/shipment-status",
                "body": json.dumps({"lrnum": "LR1", "status": "Pending"}),
            },
            object(),
        )
    assert resp["statusCode"] == 401
    mock_sqs.send_message.assert_not_called()


def test_ingest_enqueues_shipment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELHIVERY_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("DELHIVERY_WEBHOOK_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/queue")
    mock_sqs = MagicMock()
    with patch("src.delhivery_webhook_ingest._sqs", return_value=mock_sqs):
        resp = ingest_handler(
            {
                "headers": {},
                "rawPath": "/v1/webhooks/delhivery/shipment-status",
                "body": json.dumps({"lrnum": "220260516", "status": "Pending"}),
            },
            object(),
        )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body == {"ok": True, "lrnum": "220260516"}
    mock_sqs.send_message.assert_called_once()
    call_kw = mock_sqs.send_message.call_args[1]
    envelope = json.loads(call_kw["MessageBody"])
    assert envelope["kind"] == "shipment_status"
    assert envelope["body"]["lrnum"] == "220260516"


def test_ingest_enqueues_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELHIVERY_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("DELHIVERY_WEBHOOK_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/queue")
    mock_sqs = MagicMock()
    body = json.dumps({"waybill": "WB1", "EPOD": "abc=", "lrnum": "LR1"})
    with patch("src.delhivery_webhook_ingest._sqs", return_value=mock_sqs):
        resp = ingest_handler(
            {
                "headers": {},
                "rawPath": "/v1/webhooks/delhivery/pod",
                "body": body,
            },
            object(),
        )
    assert resp["statusCode"] == 200
    mock_sqs.send_message.assert_called_once()
    envelope = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
    assert envelope["kind"] == "pod"


# ---------------------------------------------------------------------------
# normalize_pod_webhook unit tests (pure, no I/O)
# ---------------------------------------------------------------------------


def test_normalize_pod_webhook_full_payload() -> None:
    payload = {
        "waybill": "1234567890",
        "EPOD": "base64encodedstring==",
        "orderID": "ORD-99",
        "lrnum": "220256789",
    }
    fields = normalize_pod_webhook(payload)
    assert fields is not None
    assert fields["lrnum"] == "220256789"
    assert fields["waybill"] == "1234567890"
    assert fields["order_id"] == "ORD-99"
    assert fields["epod_b64"] == "base64encodedstring=="
    assert fields["payload"] is payload


def test_normalize_pod_webhook_uses_waybill_as_fallback() -> None:
    """If lrnum is absent, waybill becomes the identifier."""
    payload = {"waybill": "WB001", "EPOD": "abc="}
    fields = normalize_pod_webhook(payload)
    assert fields is not None
    assert fields["lrnum"] == "WB001"
    assert fields["waybill"] == "WB001"


def test_normalize_pod_webhook_rejects_empty_payload() -> None:
    assert normalize_pod_webhook({}) is None
    assert normalize_pod_webhook({"orderID": "X"}) is None


def test_normalize_pod_webhook_strips_whitespace() -> None:
    payload = {"lrnum": "  LR123  ", "waybill": " WB456 ", "EPOD": " b64 "}
    fields = normalize_pod_webhook(payload)
    assert fields is not None
    assert fields["lrnum"] == "LR123"
    assert fields["waybill"] == "WB456"
    assert fields["epod_b64"] == "b64"
