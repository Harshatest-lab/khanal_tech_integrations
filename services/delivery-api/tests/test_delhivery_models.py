from datetime import date

from src.delhivery_models import (
    body_json,
    frappe_grn_key_from_delhivery_status,
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
        "timestamp": 1609459200,
    }
    out = normalize_shipment_webhook(payload)
    assert out is not None
    assert out["lrnum"] == "220256841"
    assert out["delhivery_status"] == "DELIVERED"
    assert out["status_type"] == "DL"
    assert out["waybill"] == "85334610000033"
    assert out["status_date"] == date(2021, 1, 1)


def test_normalize_shipment_webhook_missing_lrnum() -> None:
    assert normalize_shipment_webhook({"status": "X"}) is None


def test_normalize_shipment_webhook_no_timestamp() -> None:
    out = normalize_shipment_webhook({"lrnum": "1", "status": "Manifested"})
    assert out is not None
    assert out["status_date"] is None


def test_body_json() -> None:
    ev = {"body": '{"a": 1}'}
    assert body_json(ev) == {"a": 1}


def test_frappe_grn_key_from_delhivery_status() -> None:
    assert frappe_grn_key_from_delhivery_status("Delivered") == 1
    assert frappe_grn_key_from_delhivery_status("DELIVERED") == 1
    assert frappe_grn_key_from_delhivery_status("RTO") == 2
    assert frappe_grn_key_from_delhivery_status("rto") == 2
    assert frappe_grn_key_from_delhivery_status("RTO Initiated") is None
    assert frappe_grn_key_from_delhivery_status("Manifested") is None
    assert frappe_grn_key_from_delhivery_status(None) is None
