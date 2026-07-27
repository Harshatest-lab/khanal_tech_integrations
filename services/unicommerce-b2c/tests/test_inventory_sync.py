"""Tests for inventory_sync.py: adjustment-payload building (including
the live shelf-code resolution) and the claim/process orchestration."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import inventory_sync, store  # noqa: E402
from unicommerce.settings import UnicommerceSettings  # noqa: E402


class _FakeUc:
    def __init__(self, response=None, shelf_rows=None):
        self._response = response
        self._shelf_rows = shelf_rows if shelf_rows is not None else [{"shelf": "Shelf_001", "type": "GOOD_INVENTORY"}]
        self.calls = []
        self.shelf_lookups = []

    def adjust_inventory_bulk(self, adjustments, facility_code):
        self.calls.append((adjustments, facility_code))
        return self._response

    def get_item_type_inventory(self, sku_code, facility_code):
        self.shelf_lookups.append((sku_code, facility_code))
        return self._shelf_rows


def test_resolve_shelf_uses_first_real_row():
    uc = _FakeUc(shelf_rows=[{"shelf": "Shelf_003", "type": "GOOD_INVENTORY"}, {"shelf": "Shelf_001", "type": "GOOD_INVENTORY"}])
    shelf, inv_type = inventory_sync.resolve_shelf(uc, "FGHN0035", "kfl_ecom_warehouse")
    assert shelf == "Shelf_003"
    assert inv_type == "GOOD_INVENTORY"


def test_resolve_shelf_falls_back_when_facility_has_no_rows():
    # A genuinely new facility with zero UC inventory records -- confirmed
    # live for a brand-new AMZ-* facility that never received stock via UC.
    uc = _FakeUc(shelf_rows=[])
    shelf, inv_type = inventory_sync.resolve_shelf(uc, "FGHN0005", "AMAZON_FBA_IN_BLR7")
    assert shelf == "DEFAULT"
    assert inv_type == "GOOD_INVENTORY"


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
    uc = _FakeUc(shelf_rows=[{"shelf": "Shelf_002", "type": "GOOD_INVENTORY"}])
    result = inventory_sync.build_adjustments(line_items, remarks="test", facility="AMAZON_FBA_IN_BLR7", uc=uc)
    assert len(result) == 2
    assert result[0]["itemSKU"] == "FGHN0035"
    assert result[0]["quantity"] == 5.0
    assert result[0]["adjustmentType"] == "REMOVE"
    # Regression: "Shelf_002" comes from a real per-facility UC lookup,
    # not a generic hardcoded value -- confirmed live that a made-up
    # "DEFAULT" shelf code is rejected outright with INVALID_SHELF_CODE.
    assert result[0]["shelfCode"] == "Shelf_002"
    assert result[0]["inventoryType"] == "GOOD_INVENTORY"
    assert result[0]["remarks"] == "test"
    # Regression: UC rejects an adjustment with "INVALID_FACILITY_CODE"
    # unless facilityCode is repeated per-item, not just the request's
    # Facility header -- confirmed live against a real adjustment.
    assert result[0]["facilityCode"] == "AMAZON_FBA_IN_BLR7"


def test_build_adjustments_only_looks_up_shelf_once_per_unique_sku():
    line_items = [
        {"ItemCode": "FGHN0035", "BatchNumbers": [{"BatchNumber": "B1", "Quantity": 5.0}, {"BatchNumber": "B2", "Quantity": 3.0}]},
    ]
    uc = _FakeUc()
    inventory_sync.build_adjustments(line_items, remarks="test", facility="EC-FG", uc=uc)
    assert uc.shelf_lookups == [("FGHN0035", "EC-FG")]


def test_build_adjustments_falls_back_to_line_qty_when_no_batches():
    line_items = [{"ItemCode": "FGDC0038", "Quantity": 5.0, "BatchNumbers": []}]
    result = inventory_sync.build_adjustments(line_items, remarks="test", facility="EC-FG", uc=_FakeUc())
    assert len(result) == 1
    assert result[0]["quantity"] == 5.0


def test_build_adjustments_skips_zero_qty():
    line_items = [{"ItemCode": "X", "BatchNumbers": [{"BatchNumber": "B1", "Quantity": 0}]}]
    assert inventory_sync.build_adjustments(line_items, remarks="test", facility="EC-FG", uc=_FakeUc()) == []


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


def test_process_claimed_dry_run_resolves_shelf_but_never_writes():
    # dry_run must still resolve real shelf codes (a read) to produce an
    # accurate preview, but must never call the write endpoint.
    uc = _FakeUc(shelf_rows=[{"shelf": "Shelf_001", "type": "GOOD_INVENTORY"}])
    result = inventory_sync._process_claimed(_claimed_row(), uc, settings=_settings(), dry_run=True)
    assert result["status"] == "dry_run"
    assert "would_adjust" in result
    assert result["would_adjust"][0]["shelfCode"] == "Shelf_001"
    assert uc.calls == []  # adjust_inventory_bulk never called
    assert uc.shelf_lookups == [("FGHN0035", "AMAZON_FBA_IN_BLR7")]  # but the read did happen


def test_process_claimed_raises_on_unmapped_warehouse():
    row = _claimed_row()
    row["line_items"][0]["FromWarehouseCode"] = "UNMAPPED-WH"
    try:
        inventory_sync._process_claimed(row, _FakeUc(), _settings(), dry_run=False)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "UNMAPPED-WH" in str(e)
