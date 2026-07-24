"""Tests for the FEFO allocation and warehouse-routing logic, ported from
khanal_tech_integrations/unicommerce/mappers/sap_payload.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from unicommerce.sap_payload import (  # noqa: E402
    DEFAULT_WAREHOUSE,
    _allocate_return_batches,
    _resolve_freight_gross,
    _resolve_sap_warehouse,
    credit_note_payload,
    fefo_allocate_from_rows,
)
from unicommerce.settings import UnicommerceSettings  # noqa: E402


def test_fefo_allocate_single_batch_covers_need():
    rows = [{"batch_number": "B1", "quantity": 10}]
    allocs = fefo_allocate_from_rows(rows, 5)
    assert allocs == [{"BatchNumber": "B1", "Quantity": 5.0}]


def test_fefo_allocate_spans_multiple_batches_in_row_order():
    # Rows must already be FEFO-sorted by the caller (earliest expiry first) --
    # this function just fills in that order.
    rows = [{"batch_number": "B1", "quantity": 3}, {"batch_number": "B2", "quantity": 10}]
    allocs = fefo_allocate_from_rows(rows, 5)
    assert allocs == [{"BatchNumber": "B1", "Quantity": 3.0}, {"BatchNumber": "B2", "Quantity": 2.0}]


def test_fefo_allocate_insufficient_stock_returns_empty():
    rows = [{"batch_number": "B1", "quantity": 2}]
    assert fefo_allocate_from_rows(rows, 5) == []


def test_fefo_allocate_within_tolerance_still_succeeds():
    # BATCH_QTY_TOLERANCE = 0.01 -- a hair's-width shortage from rounding
    # must not falsely trigger a batch_hold.
    rows = [{"batch_number": "B1", "quantity": 4.995}]
    allocs = fefo_allocate_from_rows(rows, 5.0)
    assert allocs == [{"BatchNumber": "B1", "Quantity": 4.995}]


def test_fefo_allocate_zero_or_negative_need_returns_empty():
    assert fefo_allocate_from_rows([{"batch_number": "B1", "quantity": 10}], 0) == []
    assert fefo_allocate_from_rows([{"batch_number": "B1", "quantity": 10}], -1) == []


def test_resolve_sap_warehouse_fba_channel_always_wins():
    settings = UnicommerceSettings(warehouse_map={"some-uc-code": "OTHER-WH"})
    assert _resolve_sap_warehouse(settings, "amazon_fba_in_bom5", "some-uc-code") == "AMZ-BOM5"


def test_resolve_sap_warehouse_settings_map_before_default():
    settings = UnicommerceSettings(warehouse_map={"UC-WH-1": "SAP-WH-1"})
    assert _resolve_sap_warehouse(settings, "FLIPKART", "UC-WH-1") == "SAP-WH-1"


def test_resolve_sap_warehouse_falls_back_to_default_ec_fg():
    settings = UnicommerceSettings()
    assert _resolve_sap_warehouse(settings, "FLIPKART", "unknown-uc-code") == DEFAULT_WAREHOUSE


def test_resolve_freight_gross_sums_all_charge_components():
    item = {"shippingCharges": 10, "shippingMethodCharges": 5, "cashOnDeliveryCharges": 2}
    assert _resolve_freight_gross(item) == 17.0


def test_resolve_freight_gross_never_negative():
    item = {"shippingCharges": -50}
    assert _resolve_freight_gross(item) == 0.0


def test_allocate_return_batches_spans_multiple_batches_in_order():
    source = [{"BatchNumber": "B1", "Quantity": 2}, {"BatchNumber": "B2", "Quantity": 10}]
    assert _allocate_return_batches(source, 5) == [
        {"BatchNumber": "B1", "Quantity": 2.0},
        {"BatchNumber": "B2", "Quantity": 3.0},
    ]


def test_allocate_return_batches_absorbs_rounding_drift_into_last_allocation():
    # Return qty exceeds the parent line's recorded batch qty by a hair --
    # must not silently drop the excess or throw, absorb it into the tail.
    source = [{"BatchNumber": "B1", "Quantity": 4.999}]
    result = _allocate_return_batches(source, 5)
    assert result == [{"BatchNumber": "B1", "Quantity": 5.0}]


def test_allocate_return_batches_zero_qty_returns_empty():
    assert _allocate_return_batches([{"BatchNumber": "B1", "Quantity": 5}], 0) == []


_SETTINGS_WITH_CN = UnicommerceSettings(
    channel_customer_map={"FLIPKART": "C-FLIPKART", "default": "C-DEFAULT"},
    credit_note_series=42,
)


def _cn(**overrides):
    base = {
        "uc_return_id": "RET-1",
        "uc_order_id": "SO-1",
        "parent_sap_doc_entry": "9001",
        "return_date": "2026-07-20",
        "items": [
            {
                "itemSku": "SKU-A",
                "sapItemCode": "SKU-A",
                "quantity": 2,
                "sapBaseLine": 0,
                "sapAccountCode": "4001",
                "sapBatchNumbers": [{"BatchNumber": "B1", "Quantity": 5}],
                "warehouseCode": "",
            }
        ],
    }
    base.update(overrides)
    return base


def test_credit_note_payload_builds_expected_shape():
    payload = credit_note_payload(_cn(), {"channel_name": "FLIPKART", "state": "KA"}, _SETTINGS_WITH_CN)
    assert payload["CardCode"] == "C-FLIPKART"
    assert payload["Series"] == 42
    assert payload["U_UCReturnID"] == "RET-1"
    assert payload["DocDate"] == "2026-07-20"
    line = payload["DocumentLines"][0]
    assert line["ItemCode"] == "SKU-A"
    assert line["BaseType"] == 13
    assert line["BaseEntry"] == 9001
    assert line["BaseLine"] == 0
    assert line["BatchNumbers"] == [{"BatchNumber": "B1", "Quantity": 2.0}]
    assert line["AccountCode"] == "4001"


def test_credit_note_payload_fba_channel_routes_to_dedicated_warehouse():
    payload = credit_note_payload(_cn(), {"channel_name": "amazon_fba_in_bom5", "state": "KA"}, _SETTINGS_WITH_CN)
    assert payload["DocumentLines"][0]["WarehouseCode"] == "AMZ-BOM5"


def test_credit_note_payload_missing_parent_doc_entry_raises():
    with pytest.raises(ValueError, match="parent_sap_doc_entry"):
        credit_note_payload(_cn(parent_sap_doc_entry=""), {"channel_name": "FLIPKART"}, _SETTINGS_WITH_CN)


def test_credit_note_payload_missing_series_raises():
    settings = UnicommerceSettings(channel_customer_map={"default": "C-DEFAULT"})
    with pytest.raises(ValueError, match="credit_note_series"):
        credit_note_payload(_cn(), {"channel_name": "FLIPKART"}, settings)


def test_credit_note_payload_missing_batches_raises():
    cn = _cn(items=[{"itemSku": "SKU-A", "quantity": 2, "sapBaseLine": 0, "sapBatchNumbers": []}])
    with pytest.raises(ValueError, match="batch mapping"):
        credit_note_payload(cn, {"channel_name": "FLIPKART"}, _SETTINGS_WITH_CN)
