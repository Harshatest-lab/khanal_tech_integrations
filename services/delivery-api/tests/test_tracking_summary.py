"""Delhivery LTL track summary (waybill / mwbn) — no HTTP deps."""

from src.portal_delhivery_parse import tracking_summary


def test_tracking_summary_uses_top_level_mwbn():
    raw = {
        "success": True,
        "data": {
            "status": "MANIFESTED",
            "location": "Delhi_Rohini_RP (Delhi)",
            "scan_remark": "Manifest uploaded",
            "lrnum": "220308320",
            "mwbn": "85334610001875",
        },
    }
    summary = tracking_summary(raw)
    assert summary["waybill"] == "85334610001875"
    assert summary["delhivery_status"] == "MANIFESTED"
    assert summary["location"] == "Delhi_Rohini_RP (Delhi)"
    assert summary["shipment_remark"] == "Manifest uploaded"


def test_tracking_summary_prefers_wbns_wbn():
    raw = {
        "data": {
            "status": "In Transit",
            "mwbn": "111",
            "wbns": [{"wbn": "222", "status": "In Transit", "location": "Hub"}],
        }
    }
    summary = tracking_summary(raw)
    assert summary["waybill"] == "222"
    assert summary["location"] == "Hub"
