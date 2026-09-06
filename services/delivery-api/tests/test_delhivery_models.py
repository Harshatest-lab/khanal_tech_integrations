from datetime import date, datetime

from src.delhivery_models import (
    SITE_TZ,
    body_json,
    coerce_to_site_date,
    extract_pod_webhook_date,
    extract_status_payload_date,
    frappe_grn_key_from_delhivery_status,
    lrnum_from_track_response,
    normalize_shipment_webhook,
    resolve_u_pod_date,
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


def test_normalize_shipment_webhook_accepts_waybill_key() -> None:
    """Delhivery / test clients often send ``waybill`` instead of ``mwn`` / ``wbn``."""
    out = normalize_shipment_webhook(
        {
            "lrnum": "220308320",
            "status": "In Transit",
            "waybill": "85334610001875",
        }
    )
    assert out is not None
    assert out["waybill"] == "85334610001875"


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
    assert frappe_grn_key_from_delhivery_status("Partial Delivered") == 1
    assert frappe_grn_key_from_delhivery_status("RTO") == 2
    assert frappe_grn_key_from_delhivery_status("rto") == 2
    assert frappe_grn_key_from_delhivery_status("RTO Initiated") == 2
    assert frappe_grn_key_from_delhivery_status("Manifested") is None
    assert frappe_grn_key_from_delhivery_status(None) is None


def test_coerce_epoch_ms_and_forecast_ignored() -> None:
    # 2021-01-01T00:00:00Z in ms
    assert coerce_to_site_date(1609459200000) == date(2021, 1, 1)
    assert coerce_to_site_date("26/07/2026") == date(2026, 7, 26)
    assert extract_status_payload_date(
        {"expected_delivery_date": "2026-07-01", "timestamp": 1609459200}
    ) == date(2021, 1, 1)
    assert extract_status_payload_date({"promised_delivery_date": "2026-07-01"}) is None


def test_extract_pod_webhook_date_priority_keys_only() -> None:
    assert extract_pod_webhook_date({"PODDate": "2025-12-01", "timestamp": 1609459200}) == date(2025, 12, 1)
    assert extract_pod_webhook_date({"timestamp": 1609459200}) is None


def test_resolve_u_pod_date_priority() -> None:
    d, origin = resolve_u_pod_date(
        pod_payload={"pod_date": "2025-11-02"},
        delhivery_status="Delivered",
        last_status_webhook={"timestamp": 1609459200},
    )
    assert d == "2025-11-02"
    assert origin == "pod_raw_payload"

    d, origin = resolve_u_pod_date(
        pod_payload={"timestamp": 1609459200},
        delhivery_status="Delivered",
        last_status_webhook={"status": "Delivered", "timestamp": 1609459200},
    )
    assert d == "2021-01-01"
    assert origin == "last_status_webhook"

    d, origin = resolve_u_pod_date(
        delhivery_status="Delivered",
        last_status_webhook={"status": "Delivered"},
        delivered_event_payload={"status": "Delivered", "deliveryDate": "2024-06-15"},
    )
    assert d == "2024-06-15"
    assert origin == "status_events_payload"

    d, origin = resolve_u_pod_date(
        delhivery_status="Delivered",
        last_status_webhook=None,
        delivered_event_payload={"status": "Delivered"},
        delivered_event_received_at=datetime(2023, 5, 1, 10, 0, tzinfo=SITE_TZ),
    )
    assert d == "2023-05-01"
    assert origin == "status_events_received_at"

    d, origin = resolve_u_pod_date(
        delhivery_status="Delivered",
        lr_updated_at=datetime(2022, 3, 4, 12, 0, tzinfo=SITE_TZ),
    )
    assert d == "2022-03-04"
    assert origin == "delhivery_lr_updated_at"

    d, origin = resolve_u_pod_date(delhivery_status="Manifested", last_status_webhook={"timestamp": 1609459200})
    assert d is None
    assert origin == "none"
