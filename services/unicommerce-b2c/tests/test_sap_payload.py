"""Tests for the FEFO allocation and warehouse-routing logic, ported from
khanal_tech_integrations/unicommerce/mappers/sap_payload.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce.sap_payload import (  # noqa: E402
    DEFAULT_WAREHOUSE,
    _resolve_freight_gross,
    _resolve_sap_warehouse,
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
