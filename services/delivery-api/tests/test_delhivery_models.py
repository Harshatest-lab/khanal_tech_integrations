import pytest

from khanal_shared.config import get_settings

from src.delhivery_models import (
    body_json,
    lrnum_from_track_response,
    normalize_shipment_webhook,
)


def test_lrnum_from_track_response() -> None:
    body = {"success": True, "data": {"lrnum": "220256841", "status": "MANIFESTED"}}
    assert lrnum_from_track_response(body) == "220256841"


def test_normalize_shipment_webhook() -> None:
    payload = {
        "lrnum": "220256841",
        "status": "DELIVERED",
        "status_type": "DL",
        "shipment_remark": "ok",
        "location": "Hub",
        "mwn": "85334610000033",
    }
    out = normalize_shipment_webhook(payload)
    assert out is not None
    assert out["lrnum"] == "220256841"
    assert out["delhivery_status"] == "DELIVERED"
    assert out["status_type"] == "DL"
    assert out["waybill"] == "85334610000033"


def test_normalize_shipment_webhook_missing_lrnum() -> None:
    assert normalize_shipment_webhook({"status": "X"}) is None


def test_body_json() -> None:
    ev = {"body": '{"a": 1}'}
    assert body_json(ev) == {"a": 1}


def test_frappe_snapshot_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FRAPPE_INGEST_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    from src.handler import frappe_delhivery_tracking_snapshot_handler

    resp = frappe_delhivery_tracking_snapshot_handler({"headers": {}}, object())
    get_settings.cache_clear()
    assert resp["statusCode"] == 503


def test_frappe_snapshot_rejects_bad_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRAPPE_INGEST_API_KEY", "good")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    from src.handler import frappe_delhivery_tracking_snapshot_handler

    resp = frappe_delhivery_tracking_snapshot_handler(
        {"headers": {"x-api-key": "wrong"}, "body": "{}"},
        object(),
    )
    get_settings.cache_clear()
    assert resp["statusCode"] == 401
