import store
from blinkit import decide_pipeline, rematch_po_state, should_notify_po_update, unparsed_po_number


def test_unparsed_po_number():
    assert unparsed_po_number("12") == "UNPARSED-12"


def test_pipeline_ready_when_lines_and_no_error():
    assert decide_pipeline("", [{"item_code": "1"}]) == store.STATUS_READY


def test_pipeline_needs_review_when_error_even_without_mapped_lines():
    assert decide_pipeline("No mapping found for item codes: 10142879", []) == store.STATUS_NEEDS_REVIEW


def test_pipeline_parsed_when_no_lines_and_no_error():
    assert decide_pipeline("", []) == store.STATUS_PARSED


def test_rematch_applies_new_sku_and_ship_to():
    aliases = [{"address_id": "DC-BLR", "address_entry": "Blinkit DC Whitefield Bangalore 560066"}]
    sku = {"10164944": "FGHN0063", "10142879": "FGHN0054"}
    state = rematch_po_state(
        "Blinkit DC Whitefield Bangalore 560066",
        [{"item_code": "10164944", "sap_code": ""}, {"item_code": "10142879", "sap_code": ""}],
        aliases,
        sku,
    )
    assert state["ok"] is True
    assert state["matched_address_name"] == "DC-BLR"
    assert {row["item_code"]: row["sap_code"] for row in state["lines"]} == sku


def test_rematch_still_blocked_when_sku_missing():
    state = rematch_po_state(
        "unknown warehouse",
        [{"item_code": "10142879", "sap_code": ""}],
        [],
        {},
    )
    assert state["ok"] is False
    assert "address" in state["last_error"].lower() or "similar" in state["last_error"].lower()
    assert "10142879" in state["last_error"]


def test_taxcode_from_item_master_rate():
    from blinkit import _taxcode, enrich_lines_from_item_master
    from unittest.mock import patch

    assert _taxcode("KT", 5) == "KACS5"
    assert _taxcode("TN", 5) == "KAIG5"
    assert _taxcode("KT", 18) == "KACS18"
    assert _taxcode("", 18) == "KAIG18"
    item = {
        "FGHN0054": {
            "ItemCode": "FGHN0054",
            "ItemName": "Pink Salt",
            "U_TaxRate": 5,
            "U_GstTax": "GST@5",
            "U_IgstTax": "IGST@5",
        }
    }
    with patch("blinkit.get_items", return_value=item):
        out = enrich_lines_from_item_master([{"item_code": "10142879", "sap_code": "FGHN0054"}], "KT")
    assert out[0]["tax_rate"] == 5
    assert out[0]["gst_tax"] == "GST@5"
    assert out[0]["taxcode"] == "KACS5"


def test_tax_rate_falls_back_to_igst_udf_when_u_taxrate_missing():
    from blinkit import _parse_tax_rate, _taxcode

    item = {"U_IgstTax": "IGST@18", "U_GstTax": "GST@18"}
    assert _parse_tax_rate(item) == 18
    assert _taxcode("MH", 18) == "KAIG18"


def test_should_notify_on_first_po():
    assert should_notify_po_update(None, pipeline_status="ready", last_error="")


def test_should_skip_notify_when_duplicate_ready_email():
    prev = {"pipeline_status": "ready", "last_error": ""}
    assert not should_notify_po_update(prev, pipeline_status="ready", last_error="")


def test_should_notify_when_status_changes():
    prev = {"pipeline_status": "needs_review", "last_error": "bad address"}
    assert should_notify_po_update(prev, pipeline_status="ready", last_error="")


def test_dedupe_notifications_keeps_newest_per_po():
    rows = [
        {"id": 3, "channel": "blinkit", "po_number": "123", "created_at": "2026-08-28T10:00:00Z"},
        {"id": 2, "channel": "blinkit", "po_number": "123", "created_at": "2026-08-28T09:00:00Z"},
        {"id": 1, "channel": "blinkit", "po_number": "", "created_at": "2026-08-28T08:00:00Z"},
    ]
    out = store._dedupe_notifications(rows)
    assert [r["id"] for r in out] == [3, 1]
