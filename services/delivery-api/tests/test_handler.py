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
    monkeypatch.delenv("DELHIVERY_SKIP_WEBHOOK_SECRET", raising=False)
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


def test_worker_skip_secret_allows_without_webhook_secret_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DELHIVERY_SKIP_WEBHOOK_SECRET is set, missing webhookSecret on SQS must not block DB write."""
    monkeypatch.setenv("DELHIVERY_WEBHOOK_SECRET", "correct-secret")
    monkeypatch.setenv("DELHIVERY_SKIP_WEBHOOK_SECRET", "true")
    get_settings.cache_clear()
    pool = MagicMock()
    conn_cm = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn_cm)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    record = {
        "body": json.dumps({"lrnum": "LR-SKIP", "status": "Pending"}),
        "messageAttributes": {
            "webhookKind": {"stringValue": "shipment_status", "dataType": "String"},
        },
    }
    with patch("src.delhivery_sqs_worker.apply_shipment_status_webhook") as apply_fn:
        _process_one_record(pool, record)
    apply_fn.assert_called_once()


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
    with patch("src.delhivery_sqs_worker.apply_pod_webhook") as apply_fn:
        apply_fn.return_value = (1, "LR1")
        with patch("src.delhivery_sqs_worker.maybe_sync_invoice_pod_after_db"):
            _process_one_record(pool, record)
    apply_fn.assert_called_once()


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
    assert fields["pod_url"] is None
    assert fields["pod_format"] == "base64"
    assert fields.get("pod_date") is None
    assert fields["payload"] is payload


def test_normalize_pod_date_from_unix_timestamp() -> None:
    from datetime import date

    # 2021-01-01 00:00:00 UTC
    payload = {"lrnum": "1", "EPOD": "e30=", "timestamp": 1609459200}
    fields = normalize_pod_webhook(payload)
    assert fields is not None
    assert fields["pod_date"] == date(2021, 1, 1)


def test_normalize_pod_date_from_iso_string() -> None:
    from datetime import date

    payload = {"lrnum": "1", "EPOD": "e30=", "PODDate": "2025-12-01"}
    fields = normalize_pod_webhook(payload)
    assert fields is not None
    assert fields["pod_date"] == date(2025, 12, 1)


def test_normalize_pod_webhook_pod_url_only() -> None:
    payload = {"lrnum": "LR9", "PODUrl": "https://example.com/signed-pod"}
    fields = normalize_pod_webhook(payload)
    assert fields is not None
    assert fields["lrnum"] == "LR9"
    assert fields["pod_url"] == "https://example.com/signed-pod"
    assert fields["pod_format"] == "url"
    assert fields["epod_b64"] is None


def test_normalize_pod_webhook_waybill_only_for_lr_lookup() -> None:
    """If lrnum is absent, worker resolves parent ``delhivery_lr`` via waybill; ``lrnum`` stays unset here."""
    payload = {"waybill": "WB001", "EPOD": "abc="}
    fields = normalize_pod_webhook(payload)
    assert fields is not None
    assert fields["lrnum"] is None
    assert fields["waybill"] == "WB001"
    assert fields["epod_b64"] == "abc="


def test_normalize_pod_webhook_accepts_int_lrnum() -> None:
    payload = {"lrnum": 220256789, "EPOD": "x"}
    fields = normalize_pod_webhook(payload)
    assert fields is not None
    assert fields["lrnum"] == "220256789"


def test_normalize_pod_webhook_strips_zero_width_in_lrnum() -> None:
    payload = {"lrnum": "\u200b220256789", "EPOD": "x"}
    fields = normalize_pod_webhook(payload)
    assert fields is not None
    assert fields["lrnum"] == "220256789"


def test_normalize_pod_webhook_mixed_epod_and_url() -> None:
    payload = {
        "lrnum": "L1",
        "EPOD": "AAA",
        "pod_url": "https://x/y",
    }
    fields = normalize_pod_webhook(payload)
    assert fields is not None
    assert fields["pod_format"] == "mixed"


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
    assert fields["pod_format"] == "base64"
