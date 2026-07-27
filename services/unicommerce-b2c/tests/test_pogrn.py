"""Tests for pogrn.py: PO/GRN payload building, round-robin batching,
facility-code resolution, and the claim/process orchestration."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import pogrn, store  # noqa: E402
from unicommerce.sap_payload import CHANNEL_WAREHOUSE_MAP, DEFAULT_WAREHOUSE  # noqa: E402
from unicommerce.settings import UnicommerceSettings  # noqa: E402


def test_watched_warehouses_includes_ec_fg_and_all_amz():
    result = pogrn.watched_warehouses()
    assert DEFAULT_WAREHOUSE in result
    assert set(CHANNEL_WAREHOUSE_MAP.values()) <= result


def test_facility_code_for_warehouse_inverts_warehouse_map():
    settings = UnicommerceSettings(warehouse_map={"kfl_ecom_warehouse": "EC-FG", "AMAZON_FBA_IN_BLR7": "AMZ-BLR7"})
    assert pogrn.facility_code_for_warehouse(settings, "EC-FG") == "kfl_ecom_warehouse"
    assert pogrn.facility_code_for_warehouse(settings, "AMZ-BLR7") == "AMAZON_FBA_IN_BLR7"
    assert pogrn.facility_code_for_warehouse(settings, "UNKNOWN-WH") is None


def test_aggregate_po_items_sums_quantity_per_sku():
    line_items = [
        {"ItemCode": "FGHN0035", "Quantity": 54.0},
        {"ItemCode": "FGHN0035", "Quantity": 42.0},
        {"ItemCode": "FGHN0059", "Quantity": 240.0},
    ]
    result = pogrn.aggregate_po_items(line_items)
    by_sku = {r["itemSKU"]: r["quantity"] for r in result}
    assert by_sku == {"FGHN0035": 96.0, "FGHN0059": 240.0}


def test_aggregate_po_items_skips_zero_or_missing_sku():
    line_items = [{"ItemCode": "", "Quantity": 5}, {"ItemCode": "X", "Quantity": 0}]
    assert pogrn.aggregate_po_items(line_items) == []


def test_build_grn_items_one_entry_per_batch():
    line_items = [
        {
            "ItemCode": "FGHN0035",
            "BatchNumbers": [
                {"BatchNumber": "H202409D11D26", "Quantity": 54.0, "ExpiryDate": "2027-04-10", "ManufacturingDate": "2026-04-11"},
                {"BatchNumber": "D802413E22E26", "Quantity": 42.0, "ExpiryDate": "2027-05-21", "ManufacturingDate": "2026-05-22"},
            ],
        }
    ]
    result = pogrn.build_grn_items(line_items)
    assert len(result) == 2
    assert result[0]["skuCode"] == "FGHN0035"
    assert result[0]["quantity"] == 54.0
    assert result[0]["wsBatchDetail"]["wsBatchGroupFieldValue"]["vendorBatchNumber"] == "H202409D11D26"
    assert result[0]["expiry"] == "2027-04-10T00:00:00.000Z"
    # Regression: UC's "SAP_BATCHCODE_WITH_EXPDATE" batch group rejects a
    # third field ("mfd") with "Extra fields values are present" --
    # confirmed live against a real PO (PO8975). Only these two belong here.
    assert set(result[0]["wsBatchDetail"]["wsBatchGroupFieldValue"].keys()) == {"vendorBatchNumber", "expiryDate"}


def test_build_grn_items_falls_back_to_line_qty_when_no_batches():
    line_items = [{"ItemCode": "FGDC0038", "Quantity": 5.0, "BatchNumbers": []}]
    result = pogrn.build_grn_items(line_items)
    assert len(result) == 1
    assert result[0]["skuCode"] == "FGDC0038"
    assert result[0]["quantity"] == 5.0
    assert result[0]["wsBatchDetail"]["wsBatchGroupFieldValue"]["vendorBatchNumber"] == "NA"


def test_round_robin_splits_duplicate_skus_across_rounds():
    # 2 batches of FGHN0035 + 1 of FGHN0059 -> round 1 has both SKUs,
    # round 2 has the leftover FGHN0035 batch (UC forbids dup SKUs/GRN).
    items = [
        {"skuCode": "FGHN0035", "quantity": 54.0},
        {"skuCode": "FGHN0035", "quantity": 42.0},
        {"skuCode": "FGHN0059", "quantity": 240.0},
    ]
    rounds = pogrn.round_robin_grn_rounds(items)
    assert len(rounds) == 2
    assert {i["skuCode"] for i in rounds[0]} == {"FGHN0035", "FGHN0059"}
    assert [i["skuCode"] for i in rounds[1]] == ["FGHN0035"]


def test_round_robin_empty_input_returns_no_rounds():
    assert pogrn.round_robin_grn_rounds([]) == []


# ── _process_claimed orchestration (mocked store/uc) ────────────────────

class _FakeUc:
    def __init__(self):
        self.po_calls = []
        self.grn_create_calls = []
        self.add_item_calls = []
        self._grn_seq = 0

    def create_po_approved(self, vendor_code, items, facility_code):
        self.po_calls.append((vendor_code, items, facility_code))
        return {"purchaseOrderCode": "PO999"}

    def create_inflow_receipt(self, po_code, invoice_number, invoice_date_uc, facility_code):
        self._grn_seq += 1
        self.grn_create_calls.append((po_code, invoice_number, facility_code))
        return {"inflowReceiptCode": f"GRN{self._grn_seq}"}

    def add_grn_item(self, grn_code, item, facility_code):
        self.add_item_calls.append((grn_code, item["skuCode"], facility_code))
        return {"successful": True}


def _settings():
    return UnicommerceSettings(warehouse_map={"kfl_ecom_warehouse": "EC-FG"}, vendor_code="EC-B2C")


def _claimed_row(uc_po_id="", uc_grn_ids=None):
    return {
        "sap_doc_entry": 53974,
        "from_warehouse": "HN-FG",
        "to_warehouse": "EC-FG",
        "line_items": [
            {"ItemCode": "FGHN0035", "Quantity": 54.0, "WarehouseCode": "EC-FG", "FromWarehouseCode": "HN-FG",
             "BatchNumbers": [{"BatchNumber": "H202409D11D26", "Quantity": 54.0, "ExpiryDate": "2027-04-10", "ManufacturingDate": "2026-04-11"}]},
        ],
        "uc_po_id": uc_po_id,
        "uc_grn_ids": uc_grn_ids or [],
        "attempt_count": 0,
    }


def test_process_claimed_creates_po_then_grn_when_nothing_done_yet(monkeypatch):
    calls = {"po_created": [], "grn_appended": [], "done": []}
    monkeypatch.setattr(store, "mark_pogrn_po_created", lambda conn, doc_entry, po_id: calls["po_created"].append((doc_entry, po_id)))
    monkeypatch.setattr(store, "append_pogrn_grn_id", lambda conn, doc_entry, grn_id: calls["grn_appended"].append((doc_entry, grn_id)))
    monkeypatch.setattr(store, "mark_pogrn_done", lambda conn, doc_entry: calls["done"].append(doc_entry))

    uc = _FakeUc()
    result = pogrn._process_claimed(None, _claimed_row(), uc, _settings(), dry_run=False)

    assert result["status"] == "grn_done"
    assert len(uc.po_calls) == 1
    assert calls["po_created"] == [(53974, "PO999")]
    assert len(uc.grn_create_calls) == 1
    assert calls["grn_appended"] == [(53974, "GRN1")]
    assert calls["done"] == [53974]


def test_process_claimed_resumes_at_grn_when_po_already_created(monkeypatch):
    """Crash-recovery re-entry: uc_po_id already set -- must not call
    create_po_approved again."""
    monkeypatch.setattr(store, "append_pogrn_grn_id", lambda *a: None)
    monkeypatch.setattr(store, "mark_pogrn_done", lambda *a: None)

    uc = _FakeUc()
    result = pogrn._process_claimed(None, _claimed_row(uc_po_id="PO_EXISTING"), uc, _settings(), dry_run=False)

    assert uc.po_calls == []  # never re-created
    assert result["uc_po_id"] == "PO_EXISTING"
    assert result["status"] == "grn_done"


def test_process_claimed_dry_run_never_calls_uc():
    result = pogrn._process_claimed(None, _claimed_row(), uc=None, settings=_settings(), dry_run=True)
    assert result["status"] == "dry_run"
    assert "would_create_po" in result
    assert "would_create_grn_rounds" in result  # full preview, not just the PO half


def test_process_claimed_dry_run_previews_grn_even_when_po_already_created():
    result = pogrn._process_claimed(None, _claimed_row(uc_po_id="PO_EXISTING"), uc=None, settings=_settings(), dry_run=True)
    assert result["status"] == "dry_run"
    assert result["uc_po_id"] == "PO_EXISTING"
    assert "would_create_po" not in result
    assert "would_create_grn_rounds" in result


def test_process_claimed_raises_on_unmapped_warehouse():
    row = _claimed_row()
    row["line_items"][0]["WarehouseCode"] = "UNMAPPED-WH"
    try:
        pogrn._process_claimed(None, row, uc=_FakeUc(), settings=_settings(), dry_run=False)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "UNMAPPED-WH" in str(e)
