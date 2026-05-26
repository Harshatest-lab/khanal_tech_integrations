import pytest

from khanal_shared.config import get_settings

from src.frappe_ingest import handler as frappe_handler
from src.delhivery_webhook import handler as webhook_handler


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
