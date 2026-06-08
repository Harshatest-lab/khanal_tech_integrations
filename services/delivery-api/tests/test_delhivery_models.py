from src.delhivery_models import (
    body_json,
    frappe_grn_key_for_sap_pod_patch,
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


def test_frappe_grn_key_from_delhivery_status() -> None:
    assert frappe_grn_key_from_delhivery_status("Delivered") == 1
    assert frappe_grn_key_from_delhivery_status("DELIVERED") == 1
    assert frappe_grn_key_from_delhivery_status("RTO") == 2
    assert frappe_grn_key_from_delhivery_status("rto") == 2
    assert frappe_grn_key_from_delhivery_status("RTO Initiated") is None
    assert frappe_grn_key_from_delhivery_status("Manifested") is None
    assert frappe_grn_key_from_delhivery_status(None) is None


def test_frappe_grn_key_for_sap_pod_patch_payload_then_db() -> None:
    fields = {"payload": {"status": "Delivered"}}
    assert frappe_grn_key_for_sap_pod_patch(fields, "RTO") == 1

    fields2 = {"payload": {"lrnum": "x"}}  # no status key
    assert frappe_grn_key_for_sap_pod_patch(fields2, "RTO") == 2

    fields3 = {"payload": {}}
    assert frappe_grn_key_for_sap_pod_patch(fields3, "Delivered") == 1

    fields4 = {"payload": {}}
    assert frappe_grn_key_for_sap_pod_patch(fields4, "In Transit") is None
