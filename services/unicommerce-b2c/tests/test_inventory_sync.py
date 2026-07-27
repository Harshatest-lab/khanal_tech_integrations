"""Tests for inventory_sync.py: adjustment-payload building and the
claim/process orchestration."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import inventory_sync, store  # noqa: E402
from unicommerce.settings import UnicommerceSettings  # noqa: E402


def test_build_adjustments_one_per_batch():
    line_items = [
        {
            "ItemCode": "FGHN0035",
            "BatchNumbers": [
                {"BatchNumber": "H202409D11D26", "Quantity": 5.0},
                {"BatchNumber": "D802413E22E26", "Quantity": 3.0},
            ],
        }
    ]
    result = inventory_sync.build_adjustments(line_items, remarks="test")
    assert len(result) == 2
    assert result[0]["itemSKU"] == "FGHN0035"
    assert result[0]["quantity"] == 5.0
    assert result[0]["adjustmentType"] == "REMOVE"
    assert result[0]["shelfCode"] == "DEFAULT"
    assert result[0]["inventoryType"] == "GOOD_INVENTORY"
    assert result[0]["remarks"] == "test"


def test_build_adjustments_falls_back_to_line_qty_when_no_batches():
    line_items = [{"ItemCode": "FGDC0038", "Quantity": 5.0, "BatchNumbers": []}]
    result = inventory_sync.build_adjustments(line_items, remarks="test")
    assert len(result) == 1
    assert result[0]["quantity"] == 5.0


def test_build_adjustments_skips_zero_qty():
    line_items = [{"ItemCode": "X", "BatchNumbers": [{"BatchNumber": "B1", "Quantity": 0}]}]
    assert inventory_sync.build_adjustments(line_items, remarks="test") == []


class _FakeUc:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def adjust_inventory_bulk(self, adjustments, facility_code):
        self.calls.append((adjustments, facility_code))
        return self._response


def _settings():
    return UnicommerceSettings(warehouse_map={"AMAZON_FBA_IN_BLR7": "AMZ-BLR7"})


def _claimed_row():
    return {
        "sap_doc_entry": 60001,
        "from_warehouse": "AMZ-BLR7",
        "to_warehouse": "HN-STR",
        "line_items": [
            {"ItemCode": "FGHN0035", "FromWarehouseCode": "AMZ-BLR7", "WarehouseCode": "HN-STR",
             "BatchNumbers": [{"BatchNumber": "H202409D11D26", "Quantity": 5.0}]},
        ],
        "attempt_count": 0,
    }


def test_process_claimed_success_returns_synced():
    uc = _FakeUc({"successful": True, "inventoryAdjustmentResponses": []})
    result = inventory_sync._process_claimed(_claimed_row(), uc, _settings(), dry_run=False)
    assert result["status"] == "synced"
    assert result["adjusted"] == 1
    assert uc.calls[0][1] == "AMAZON_FBA_IN_BLR7"


def test_process_claimed_raises_on_partial_failure():
    uc = _FakeUc({
        "successful": True,
        "inventoryAdjustmentResponses": [
            {"successful": False, "facilityInventoryAdjustment": {"itemSKU": "FGHN0035"}, "errors": [{"message": "insufficient stock"}]}
        ],
    })
    try:
        inventory_sync._process_claimed(_claimed_row(), uc, _settings(), dry_run=False)
        assert False, "expected UcApiError"
    except Exception as e:
        assert "insufficient stock" in str(e)


def test_process_claimed_dry_run_never_calls_uc():
    result = inventory_sync._process_claimed(_claimed_row(), uc=None, settings=_settings(), dry_run=True)
    assert result["status"] == "dry_run"
    assert "would_adjust" in result


def test_process_claimed_raises_on_unmapped_warehouse():
    row = _claimed_row()
    row["line_items"][0]["FromWarehouseCode"] = "UNMAPPED-WH"
    try:
        inventory_sync._process_claimed(row, _FakeUc({}), _settings(), dry_run=False)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "UNMAPPED-WH" in str(e)
