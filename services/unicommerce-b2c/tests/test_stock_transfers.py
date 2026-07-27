"""Tests for stock_transfers.py: SAP StockTransfers polling/filtering used
by PoGrn (inward) and InventorySync (outward)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import sap_client, stock_transfers  # noqa: E402


def test_list_since_uses_docentry_gt_filter_ascending(monkeypatch):
    seen = {}

    def fake_request(method, path):
        seen["method"] = method
        seen["path"] = path
        return 200, {"value": [{"DocEntry": 100}, {"DocEntry": 101}]}

    monkeypatch.setattr(sap_client, "_request", fake_request)
    result = stock_transfers.list_since(99, limit=10)

    assert result == [{"DocEntry": 100}, {"DocEntry": 101}]
    assert "DocEntry%20gt%2099" in seen["path"]
    assert "orderby=DocEntry%20asc" in seen["path"]
    assert "top=10" in seen["path"]


def test_list_since_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(sap_client, "_request", lambda *a, **k: (400, {"error": "bad"}))
    try:
        stock_transfers.list_since(0)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_detail_fetches_single_document_by_docentry(monkeypatch):
    seen = {}

    def fake_request(method, path):
        seen["path"] = path
        return 200, {"DocEntry": 53974, "StockTransferLines": []}

    monkeypatch.setattr(sap_client, "_request", fake_request)
    result = stock_transfers.detail(53974)

    assert result["DocEntry"] == 53974
    assert seen["path"] == "StockTransfers(53974)"


def _transfer(*lines):
    return {"DocEntry": 1, "StockTransferLines": list(lines)}


def test_inward_lines_filters_to_watched_destination():
    transfer = _transfer(
        {"WarehouseCode": "EC-FG", "ItemCode": "A"},
        {"WarehouseCode": "AMZ-BLR7", "ItemCode": "B"},
        {"WarehouseCode": "HN-STR", "ItemCode": "C"},  # not watched
    )
    result = stock_transfers.inward_lines(transfer, {"EC-FG", "AMZ-BLR7"})
    assert [r["ItemCode"] for r in result] == ["A", "B"]


def test_outward_lines_matches_ec_fg_to_ec_b2c_pair_specifically():
    transfer = _transfer(
        {"FromWarehouseCode": "EC-FG", "WarehouseCode": "EC-B2C", "ItemCode": "A"},
        {"FromWarehouseCode": "EC-FG", "WarehouseCode": "HN-STR", "ItemCode": "B"},  # wrong destination, not matched
    )
    result = stock_transfers.outward_lines(transfer, amz_warehouses=set())
    assert [r["ItemCode"] for r in result] == ["A"]


def test_outward_lines_matches_any_destination_from_amz_warehouse():
    transfer = _transfer(
        {"FromWarehouseCode": "AMZ-BLR7", "WarehouseCode": "HN-STR", "ItemCode": "A"},
        {"FromWarehouseCode": "AMZ-BOM5", "WarehouseCode": "EC-FG", "ItemCode": "B"},
        {"FromWarehouseCode": "EC-FG", "WarehouseCode": "AMZ-BLR7", "ItemCode": "C"},  # inward to AMZ, not outward FROM it
    )
    result = stock_transfers.outward_lines(transfer, amz_warehouses={"AMZ-BLR7", "AMZ-BOM5"})
    assert [r["ItemCode"] for r in result] == ["A", "B"]
