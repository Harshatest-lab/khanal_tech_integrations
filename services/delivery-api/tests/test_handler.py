import json

import pytest

from khanal_shared.config import get_settings

from src.delhivery_models import normalize_pod_webhook
from src.delhivery_webhook import handler as webhook_handler
from src.delhivery_pod_webhook import handler as pod_handler
from src.frappe_ingest import handler as frappe_handler


def test_frappe_snapshot_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FRAPPE_INGEST_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    resp = frappe_handler({"headers": {}}, object())
    get_settings.cache_clear()
    assert resp["statusCode"] == 503


def test_frappe_snapshot_rejects_bad_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRAPPE_INGEST_API_KEY", "good")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    resp = frappe_handler({"headers": {"x-api-key": "wrong"}, "body": "{}"}, object())
    get_settings.cache_clear()
    assert resp["statusCode"] == 401


def test_delhivery_webhook_missing_lrnum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELHIVERY_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    resp = webhook_handler({"headers": {}, "body": '{"status": "DELIVERED"}'}, object())
    get_settings.cache_clear()
    assert resp["statusCode"] in (400, 503)


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


# ---------------------------------------------------------------------------
# POD webhook handler integration tests (no real DB — checks early exits)
# ---------------------------------------------------------------------------

def test_pod_webhook_no_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELHIVERY_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    body = json.dumps({"waybill": "WB1", "EPOD": "abc=", "lrnum": "LR1"})
    resp = pod_handler({"headers": {}, "body": body}, object())
    get_settings.cache_clear()
    assert resp["statusCode"] == 503


def test_pod_webhook_rejects_wrong_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELHIVERY_WEBHOOK_SECRET", "correct-secret")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    body = json.dumps({"waybill": "WB1", "EPOD": "abc=", "lrnum": "LR1"})
    resp = pod_handler(
        {"headers": {"x-delhivery-webhook-secret": "wrong"}, "body": body},
        object(),
    )
    get_settings.cache_clear()
    assert resp["statusCode"] == 401


def test_pod_webhook_missing_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELHIVERY_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    resp = pod_handler({"headers": {}, "body": '{"EPOD": "abc="}'}, object())
    get_settings.cache_clear()
    assert resp["statusCode"] in (400, 503)
