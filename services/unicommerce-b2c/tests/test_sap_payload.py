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
    ar_invoice_payload,
    credit_note_payload,
    fefo_allocate_from_rows,
    sum_tax_total,
    sum_unit_price_total,
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


def test_ar_invoice_payload_fills_batch_numbers_for_fefo_line_without_explicit_batch():
    """Regression: _post_order (post_invoices.py) must pass fefo_rows_cache
    through to ar_invoice_payload -- without it this line's BatchNumbers
    silently comes back [] even though _check_batches already confirmed
    stock was available, and SAP rejects a batch-managed item post with no
    batch allocation."""
    settings = UnicommerceSettings(
        channel_customer_map={"default": "C-DEFAULT"}, ar_invoice_series=10, tax_code_map={"0_intra": "GST0"}
    )
    uc_data = {
        "code": "SO-1",
        "items": [
            {"itemSku": "SKU-A", "quantity": 5, "unitPrice": 100, "taxPercent": 0, "warehouseCode": "EC-FG"}
        ],
    }
    payload, _tax = ar_invoice_payload(
        uniware_id="SO-1", channel="FLIPKART", order_json="{}", uc_data=uc_data, order_date="2026-07-20",
        settings=settings,
        batch_managed_cache={"SKU-A": True},
        fefo_rows_cache={("SKU-A", "EC-FG"): [{"batch_number": "B1", "quantity": 10}]},
    )
    line = payload["DocumentLines"][0]
    assert line["BatchNumbers"] == [{"BatchNumber": "B1", "Quantity": 5.0, "ItemCode": "SKU-A"}]


def test_ar_invoice_payload_batch_numbers_empty_without_fefo_cache():
    """Same setup as above but with no fefo_rows_cache passed (the bug's
    exact shape) -- confirms the failure mode this test class guards
    against, so the fix above can't silently stop mattering."""
    settings = UnicommerceSettings(
        channel_customer_map={"default": "C-DEFAULT"}, ar_invoice_series=10, tax_code_map={"0_intra": "GST0"}
    )
    uc_data = {
        "code": "SO-1",
        "items": [
            {"itemSku": "SKU-A", "quantity": 5, "unitPrice": 100, "taxPercent": 0, "warehouseCode": "EC-FG"}
        ],
    }
    payload, _tax = ar_invoice_payload(
        uniware_id="SO-1", channel="FLIPKART", order_json="{}", uc_data=uc_data, order_date="2026-07-20",
        settings=settings,
        batch_managed_cache={"SKU-A": True},
    )
    assert payload["DocumentLines"][0]["BatchNumbers"] == []


def test_ar_invoice_payload_ignores_ucs_batch_number_uses_fefo_instead():
    """UC's own recorded batchNumber is informational, not a hard
    requirement -- confirmed real incident 2026-08-19: re-posting
    week-old orders hit "have 0.0" on UC's exact recorded batch while a
    different batch of the same item was genuinely available, because
    that batch had been fully consumed by later orders in the meantime.
    The invoice must use whatever FEFO currently has available, not fail
    just because UC's specific batch is gone."""
    settings = UnicommerceSettings(
        channel_customer_map={"default": "C-DEFAULT"}, ar_invoice_series=10, tax_code_map={"0_intra": "GST0"}
    )
    uc_data = {
        "code": "SO-1",
        "items": [
            {
                "itemSku": "SKU-A", "quantity": 5, "unitPrice": 100, "taxPercent": 0, "warehouseCode": "EC-FG",
                "batchNumber": "OLD-BATCH-NOW-DEPLETED",
            }
        ],
    }
    payload, _tax = ar_invoice_payload(
        uniware_id="SO-1", channel="FLIPKART", order_json="{}", uc_data=uc_data, order_date="2026-07-20",
        settings=settings,
        batch_managed_cache={"SKU-A": True},
        # Note: no "OLD-BATCH-NOW-DEPLETED" row here at all -- only a
        # different, currently-available batch.
        fefo_rows_cache={("SKU-A", "EC-FG"): [{"batch_number": "CURRENTLY-AVAILABLE", "quantity": 10}]},
    )
    line = payload["DocumentLines"][0]
    assert line["BatchNumbers"] == [{"BatchNumber": "CURRENTLY-AVAILABLE", "Quantity": 5.0, "ItemCode": "SKU-A"}]


def test_zero_discount_floor_keeps_gross_price_when_order_has_a_paid_line():
    # The BOGO case this whole feature exists for: one paid line, one
    # line whose discount equals its full price. On a channel opted into
    # zero_discount_floor_channels, the free line must keep its gross
    # UnitPrice instead of clamping to 0.0 -- SAP rejects a 0.0 UnitPrice
    # outright ("Unit Price cannot be less than Item Cost").
    settings = UnicommerceSettings(
        channel_customer_map={"default": "C-DEFAULT"}, ar_invoice_series=10, tax_code_map={"0_intra": "GST0"},
        zero_discount_floor_channels={"DOGSEE_SITE_IN": True},
    )
    uc_data = {
        "code": "SO-1",
        "items": [
            {"itemSku": "SKU-PAID", "quantity": 1, "unitPrice": 249.0, "discount": 0, "taxPercent": 0, "warehouseCode": "EC-FG"},
            {"itemSku": "SKU-FREE", "quantity": 1, "unitPrice": 199.0, "discount": 199.0, "taxPercent": 0, "warehouseCode": "EC-FG"},
        ],
    }
    payload, _tax = ar_invoice_payload(
        uniware_id="SO-1", channel="DOGSEE_SITE_IN", order_json="{}", uc_data=uc_data, order_date="2026-08-20",
        settings=settings,
    )
    prices = {line["ItemCode"]: line["UnitPrice"] for line in payload["DocumentLines"]}
    assert prices["SKU-PAID"] == 249.0
    assert prices["SKU-FREE"] == 199.0  # kept gross, not clamped to 0


def test_zero_discount_floor_off_for_channels_not_opted_in():
    # Same exact BOGO shape, but the channel isn't in
    # zero_discount_floor_channels -- must fall back to today's existing
    # clamp-to-0 behavior, unchanged.
    settings = UnicommerceSettings(
        channel_customer_map={"default": "C-DEFAULT"}, ar_invoice_series=10, tax_code_map={"0_intra": "GST0"},
        zero_discount_floor_channels={"DOGSEE_SITE_IN": True},
    )
    uc_data = {
        "code": "SO-1",
        "items": [
            {"itemSku": "SKU-PAID", "quantity": 1, "unitPrice": 249.0, "discount": 0, "taxPercent": 0, "warehouseCode": "EC-FG"},
            {"itemSku": "SKU-FREE", "quantity": 1, "unitPrice": 199.0, "discount": 199.0, "taxPercent": 0, "warehouseCode": "EC-FG"},
        ],
    }
    payload, _tax = ar_invoice_payload(
        uniware_id="SO-1", channel="FLIPKART", order_json="{}", uc_data=uc_data, order_date="2026-08-20",
        settings=settings,
    )
    prices = {line["ItemCode"]: line["UnitPrice"] for line in payload["DocumentLines"]}
    assert prices["SKU-FREE"] == 0.0


def test_zero_discount_floor_stays_off_when_every_line_is_free():
    # The replacement-order concern raised live: an order where EVERY
    # line nets to <= 0 has no paid line to prove it's a genuine promo --
    # this must NOT be floored, even on an opted-in channel, because
    # there's no field that distinguishes a goodwill/warranty replacement
    # from a promo (confirmed live: replacementSaleOrderCode and
    # voucherCode are both null even on confirmed real BOGO orders).
    # Falling through to the old clamp-to-0 leaves this order stuck
    # failing at SAP -- visible and safe, rather than silently booking a
    # free replacement as if it were paid-for.
    settings = UnicommerceSettings(
        channel_customer_map={"default": "C-DEFAULT"}, ar_invoice_series=10, tax_code_map={"0_intra": "GST0"},
        zero_discount_floor_channels={"DOGSEE_SITE_IN": True},
    )
    uc_data = {
        "code": "SO-1",
        "items": [
            {"itemSku": "SKU-REPLACEMENT", "quantity": 1, "unitPrice": 349.0, "discount": 349.0, "taxPercent": 0, "warehouseCode": "EC-FG"},
        ],
    }
    payload, _tax = ar_invoice_payload(
        uniware_id="SO-1", channel="DOGSEE_SITE_IN", order_json="{}", uc_data=uc_data, order_date="2026-08-20",
        settings=settings,
    )
    assert payload["DocumentLines"][0]["UnitPrice"] == 0.0


def test_zero_discount_floor_does_not_affect_normal_partial_discounts():
    # Sanity check: an everyday partial discount (net price still
    # positive) must be completely unaffected by this feature, on any
    # channel, opted in or not.
    settings = UnicommerceSettings(
        channel_customer_map={"default": "C-DEFAULT"}, ar_invoice_series=10, tax_code_map={"0_intra": "GST0"},
        zero_discount_floor_channels={"DOGSEE_SITE_IN": True},
    )
    uc_data = {
        "code": "SO-1",
        "items": [
            {"itemSku": "SKU-A", "quantity": 1, "unitPrice": 183.82, "discount": 99.5, "taxPercent": 0, "warehouseCode": "EC-FG"},
        ],
    }
    payload, _tax = ar_invoice_payload(
        uniware_id="SO-1", channel="DOGSEE_SITE_IN", order_json="{}", uc_data=uc_data, order_date="2026-08-20",
        settings=settings,
    )
    assert payload["DocumentLines"][0]["UnitPrice"] == pytest.approx(84.32)


def test_resolve_sap_warehouse_fba_channel_always_wins():
    settings = UnicommerceSettings(warehouse_map={"some-uc-code": "OTHER-WH"})
    assert _resolve_sap_warehouse(settings, "amazon_fba_in_bom5", "some-uc-code") == "AMZ-BOM5"


def test_resolve_sap_warehouse_routes_blr4_fba_channel():
    settings = UnicommerceSettings(warehouse_map={"some-uc-code": "OTHER-WH"})
    assert _resolve_sap_warehouse(settings, "amazon_fba_in_blr4", "some-uc-code") == "AMZ-BLR4"


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


def test_credit_note_payload_card_code_uses_base_invoice_not_current_settings():
    # settings.channel_customer_map now maps FLIPKART -> C-FLIPKART, but the
    # base invoice was posted back when it mapped to a different customer
    # (channel reassigned since) -- SAP rejects a credit note whose CardCode
    # doesn't match the base document's, so the invoice's own stored
    # CardCode must win over re-resolving from current settings.
    parent = {"channel_name": "FLIPKART", "state": "KA", "payload_json": {"CardCode": "C-OLD-FLIPKART"}}
    payload = credit_note_payload(_cn(), parent, _SETTINGS_WITH_CN)
    assert payload["CardCode"] == "C-OLD-FLIPKART"


def test_credit_note_payload_card_code_falls_back_to_settings_without_payload_json():
    payload = credit_note_payload(_cn(), {"channel_name": "FLIPKART", "state": "KA"}, _SETTINGS_WITH_CN)
    assert payload["CardCode"] == "C-FLIPKART"


def test_credit_note_payload_item_code_prefers_resolved_sap_code_over_raw_sku():
    # sapItemCode is resolved by returns.prepare_credit_note_items against
    # the base invoice's own DocumentLines -- itemSku is just the raw UC
    # seller SKU and can legitimately differ (bundle SKUs, channel-specific
    # SKU codes). SAP rejects a credit note line whose ItemCode doesn't
    # match the base invoice line's ItemCode exactly.
    cn = _cn(
        items=[
            {
                "itemSku": "SELLER-SKU-123",
                "sapItemCode": "SAP-ITEM-A",
                "quantity": 2,
                "sapBaseLine": 0,
                "sapAccountCode": "4001",
                "sapBatchNumbers": [{"BatchNumber": "B1", "Quantity": 5}],
                "warehouseCode": "",
            }
        ]
    )
    payload = credit_note_payload(cn, {"channel_name": "FLIPKART", "state": "KA"}, _SETTINGS_WITH_CN)
    assert payload["DocumentLines"][0]["ItemCode"] == "SAP-ITEM-A"


# ── sum_unit_price_total / sum_tax_total (shared by post_invoices.py and recon.py) ──

def test_sum_unit_price_total_sums_quantity_times_unit_price():
    lines = [{"Quantity": 2, "UnitPrice": 100.0}, {"Quantity": 3, "UnitPrice": 50.5}]
    assert sum_unit_price_total(lines) == 351.5


def test_sum_unit_price_total_skips_bad_values():
    lines = [{"Quantity": "x", "UnitPrice": 100.0}, {"Quantity": 2, "UnitPrice": 10.0}]
    assert sum_unit_price_total(lines) == 20.0


def test_sum_unit_price_total_empty_is_zero():
    assert sum_unit_price_total([]) == 0.0
    assert sum_unit_price_total(None) == 0.0


def test_sum_tax_total_uses_first_matching_key_per_line():
    lines = [{"TaxTotal": 10.0}, {"TaxAmount": 5.0}, {"LineTaxTotal": 2.0, "TaxAmount": 99.0}]
    # LineTaxTotal isn't first in the candidate-key order, but TaxAmount is
    # present on that same line, so TaxAmount (98.0 here) wins for that line.
    assert sum_tax_total(lines) == 10.0 + 5.0 + 99.0


def test_sum_tax_total_ignores_lines_with_no_tax_key():
    assert sum_tax_total([{"Quantity": 1}]) == 0.0
