"""Tests for return-item -> SAP-invoice-line resolution, ported from
khanal_tech_integrations/unicommerce/returns.py's prepare_credit_note_items."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce.returns import prepare_credit_note_items  # noqa: E402

_PARENT_PAYLOAD = {
    "DocumentLines": [
        {
            "LineNum": 0,
            "ItemCode": "SKU-A",
            "U_Sale_order_itemcode": "ITEM-1",
            "WarehouseCode": "EC-FG",
            "AccountCode": "4001",
            "BatchNumbers": [{"BatchNumber": "B1", "Quantity": 5}],
        },
        {
            "LineNum": 1,
            "ItemCode": "SKU-B",
            "U_Sale_order_itemcode": "ITEM-2",
            "WarehouseCode": "EC-FG",
            "AccountCode": "4002",
            "BatchNumbers": [{"BatchNumber": "B2", "Quantity": 3}],
        },
    ]
}


def _parent(payload_json=_PARENT_PAYLOAD):
    return {"payload_json": payload_json}


def test_resolves_by_sale_order_item_code_first():
    result = prepare_credit_note_items(_parent(), [{"skuCode": "SKU-A", "saleOrderItemCode": "ITEM-1", "quantity": 2}])
    assert result["resolved_count"] == 1
    assert result["items"][0]["sapBaseLine"] == 0
    assert result["items"][0]["sapAccountCode"] == "4001"


def test_falls_back_to_item_code_when_no_sale_order_item_code():
    result = prepare_credit_note_items(_parent(), [{"skuCode": "SKU-B", "quantity": 1}])
    assert result["resolved_count"] == 1
    assert result["items"][0]["sapBaseLine"] == 1


def test_unresolved_when_no_matching_line():
    result = prepare_credit_note_items(_parent(), [{"skuCode": "SKU-ZZZ", "quantity": 1}])
    assert result["resolved_count"] == 0
    assert result["unresolved_count"] == 1


def test_second_return_of_same_sku_does_not_reuse_first_line():
    # Two return rows for SKU-A/ITEM-1's SKU -- only one payload line exists
    # for it, so the second return item must be left unresolved rather than
    # silently double-claiming the first line's batch.
    parent = _parent(
        {
            "DocumentLines": [
                {"LineNum": 0, "ItemCode": "SKU-A", "BatchNumbers": [{"BatchNumber": "B1", "Quantity": 5}]},
            ]
        }
    )
    result = prepare_credit_note_items(parent, [{"skuCode": "SKU-A", "quantity": 1}, {"skuCode": "SKU-A", "quantity": 1}])
    assert result["resolved_count"] == 1
    assert result["unresolved_count"] == 1


def test_empty_payload_json_leaves_everything_unresolved():
    result = prepare_credit_note_items({"payload_json": None}, [{"skuCode": "SKU-A", "quantity": 1}])
    assert result["resolved_count"] == 0
    assert result["unresolved_count"] == 1


def test_fallback_warehouse_used_when_item_has_none():
    result = prepare_credit_note_items(_parent(), [{"skuCode": "SKU-ZZZ", "quantity": 1}], fallback_warehouse="EC-QA")
    assert result["items"][0]["warehouseCode"] == "EC-QA"
