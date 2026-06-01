import json
from unittest.mock import MagicMock, patch

import pytest

from khanal_shared.config import get_settings
from src.delhivery_models import normalize_pod_webhook
from src.delhivery_sqs_worker import _process_one_record


def test_worker_raw_shipment_with_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELHIVERY_WEBHOOK_SECRET", raising=False)
    get_settings.cache_clear()
    pool = MagicMock()
    conn_cm = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn_cm)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    record = {
        "body": json.dumps({"lrnum": "220260516", "status": "Pending"}),
        "messageAttributes": {
            "webhookKind": {"stringValue": "shipment_status", "dataType": "String"},
        },
    }
    with patch("src.delhivery_sqs_worker.apply_shipment_status_webhook") as apply_fn:
        _process_one_record(pool, record)
    apply_fn.assert_called_once()


def test_worker_legacy_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELHIVERY_WEBHOOK_SECRET", raising=False)
    get_settings.cache_clear()
    pool = MagicMock()
    conn_cm = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn_cm)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    envelope = {
        "kind": "shipment_status",
        "headers": {},
        "body": {"lrnum": "LR9", "status": "Pending"},
    }
    record = {"body": json.dumps(envelope), "messageAttributes": {}}
    with patch("src.delhivery_sqs_worker.apply_shipment_status_webhook") as apply_fn:
        _process_one_record(pool, record)
    apply_fn.assert_called_once()


def test_worker_wrong_secret_from_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELHIVERY_WEBHOOK_SECRET", "correct-secret")
    get_settings.cache_clear()
    pool = MagicMock()
    record = {
        "body": json.dumps({"lrnum": "LR1", "status": "Pending"}),
        "messageAttributes": {
            "webhookKind": {"stringValue": "shipment_status", "dataType": "String"},
            "webhookSecret": {"stringValue": "wrong", "dataType": "String"},
        },
    }
    with patch("src.delhivery_sqs_worker.apply_shipment_status_webhook") as apply_fn:
        _process_one_record(pool, record)
    apply_fn.assert_not_called()


def test_worker_pod_from_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELHIVERY_WEBHOOK_SECRET", raising=False)
    get_settings.cache_clear()
    pool = MagicMock()
    conn_cm = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn_cm)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    record = {
        "body": json.dumps({"waybill": "WB1", "EPOD": "abc=", "lrnum": "LR1"}),
        "messageAttributes": {
            "webhookKind": {"stringValue": "pod", "dataType": "String"},
        },
    }
    with patch("src.delhivery_sqs_worker.insert_pod_event") as insert_fn:
        _process_one_record(pool, record)
    insert_fn.assert_called_once()


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
