"""Unit tests for the pure UC saleOrderDTO/return extraction logic ported
from khanal_tech_integrations/unicommerce/workers/fetch.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce.dates import epoch_millis_to_ist_date, extract_order_date  # noqa: E402
from unicommerce.mappers import (  # noqa: E402
    build_line_items,
    eligible_line_items,
    extract_uc_order_id,
    order_ready_for_invoice,
    safe_str,
)


def test_epoch_millis_pinned_to_ist_not_server_timezone():
    # 2026-06-30 19:00:00 UTC == 2026-07-01 00:30:00 IST -- must land on
    # the IST date, not the UTC date, or late-night IST orders roll over
    # to the wrong day on a UTC-local Lambda.
    epoch_ms = 1782846000000  # 2026-06-30T19:00:00Z / 2026-07-01T00:30:00+05:30
    assert epoch_millis_to_ist_date(epoch_ms) == "2026-07-01"


def test_epoch_millis_zero_or_missing_returns_none():
    assert epoch_millis_to_ist_date(0) is None
    assert epoch_millis_to_ist_date(None) is None


def test_extract_order_date_prefers_display_order_datetime():
    order = {"displayOrderDateTime": 1782846000000, "created": 1700000000000}
    assert extract_order_date(order) == "2026-07-01"


def test_extract_order_date_falls_back_to_created():
    order = {"created": 1782846000000}
    assert extract_order_date(order) == "2026-07-01"


def test_extract_uc_order_id_prefers_sale_order_code():
    assert extract_uc_order_id({"saleOrderCode": "SO1", "code": "SO2"}) == "SO1"
    assert extract_uc_order_id({"code": "SO2"}) == "SO2"
    assert extract_uc_order_id({}) is None


def test_safe_str_preserves_falsy_numeric_values():
    # `value or ""` would wrongly turn 0 into "" -- must not use that pattern.
    assert safe_str(0) == "0"
    assert safe_str(0.0) == "0.0"
    assert safe_str(None) == ""
    assert safe_str("  padded  ") == "padded"


def test_eligible_line_items_excludes_cancelled_and_on_hold():
    order = {
        "saleOrderItems": [
            {"itemSku": "A", "quantity": 1, "statusCode": "COMPLETE"},
            {"itemSku": "B", "quantity": 1, "cancellationReason": "customer request"},
            {"itemSku": "C", "quantity": 1, "onHold": True},
            {"itemSku": "E", "quantity": 1, "statusCode": "CANCELLED"},
        ]
    }
    active = eligible_line_items(order)
    assert [i["itemSku"] for i in active] == ["A"]


def test_eligible_line_items_quantity_zero_bug_inherited_from_frappe():
    # Documents the known inherited bug (see mappers.eligible_line_items):
    # an explicit quantity of 0 is falsy, so the `or` fallback chain
    # silently defaults it to 1 instead of excluding the item. This test
    # exists to make the behavior visible and force an update here if it's
    # ever fixed, not to endorse it.
    order = {"saleOrderItems": [{"itemSku": "D", "quantity": 0, "statusCode": "COMPLETE"}]}
    active = eligible_line_items(order)
    assert [i["itemSku"] for i in active] == ["D"]


def test_order_ready_for_invoice_requires_complete_status_and_lines():
    ready, _ = order_ready_for_invoice(
        {"status": "COMPLETE", "saleOrderItems": [{"itemSku": "A", "quantity": 1, "statusCode": "COMPLETE"}]}
    )
    assert ready is True

    not_ready, reason = order_ready_for_invoice({"status": "PROCESSING", "saleOrderItems": []})
    assert not_ready is False
    assert "PROCESSING" in reason

    no_lines, reason2 = order_ready_for_invoice({"status": "COMPLETE", "saleOrderItems": []})
    assert no_lines is False
    assert "no invoice-eligible" in reason2


def test_build_line_items_maps_batch_and_return_status():
    order = {
        "returns": [{"statusCode": "RETURN_COMPLETED", "returnItems": [{"saleOrderItemCode": "L1"}]}],
        "saleOrderItems": [
            {
                "id": "1",
                "itemSku": "SKU-A",
                "quantity": 2,
                "code": "L1",
                "batchDTO": {"batchCode": "B1", "batchFieldsDTO": {"vendorBatchNumber": "VB1"}},
            },
            {"id": "2", "itemSku": "SKU-B", "quantity": 1, "code": "L2"},
        ],
    }
    lines = build_line_items(order)
    assert len(lines) == 2
    l1 = next(l for l in lines if l["code"] == "L1")
    assert l1["batchcode"] == "B1"
    assert l1["vendorbatchnumber"] == "VB1"
    assert l1["uniware_return_status"] == "RETURN_COMPLETED"

    l2 = next(l for l in lines if l["code"] == "L2")
    assert l2["uniware_return_status"] == ""
