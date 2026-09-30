"""Unit tests for the pure UC saleOrderDTO/return extraction logic ported
from khanal_tech_integrations/unicommerce/workers/fetch.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from unicommerce.dates import epoch_millis_to_ist_date, extract_order_date  # noqa: E402
from unicommerce.mappers import (  # noqa: E402
    build_line_items,
    compute_uc_financials,
    eligible_line_items,
    extract_shipping_package_fields,
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


_SHIPPED_PKG = [{"dispatched": "2026-09-12T02:30:00Z", "status": "DISPATCHED"}]


def test_order_ready_for_invoice_requires_complete_status_and_lines():
    ready, _ = order_ready_for_invoice(
        {
            "status": "COMPLETE",
            "shippingPackages": _SHIPPED_PKG,
            "saleOrderItems": [{"itemSku": "A", "quantity": 1, "statusCode": "COMPLETE"}],
        }
    )
    assert ready is True

    not_ready, reason = order_ready_for_invoice({"status": "PROCESSING", "saleOrderItems": []})
    assert not_ready is False
    assert "PROCESSING" in reason

    no_lines, reason2 = order_ready_for_invoice(
        {"status": "COMPLETE", "shippingPackages": _SHIPPED_PKG, "saleOrderItems": []}
    )
    assert no_lines is False
    assert "no invoice-eligible" in reason2


# ── Ship date = SAP DocDate: extraction, skip-if-missing, Flex ─────────────────

_ONE_LINE = [{"itemSku": "A", "quantity": 1, "statusCode": "COMPLETE"}]


def test_order_without_ship_date_is_not_ready_even_if_complete():
    ready, reason = order_ready_for_invoice({"status": "COMPLETE", "saleOrderItems": _ONE_LINE})
    assert ready is False
    assert "No shipment/dispatch date" in reason

    ready2, _ = order_ready_for_invoice(
        {"status": "COMPLETE", "shippingPackages": [{"status": "CREATED"}], "saleOrderItems": _ONE_LINE}
    )
    assert ready2 is False


def test_shipment_date_uses_latest_dispatched_across_all_packages_in_utc():
    order = {
        "shippingPackages": [
            {"dispatched": "2026-09-08T10:00:00Z"},
            {"dispatched": "2026-09-12T10:00:00+05:30"},  # 04:30Z, later than the 08th
            {"dispatched": None},
        ]
    }
    fields = extract_shipping_package_fields(order)
    assert fields["shipment_date"] == "2026-09-12 04:30:00"  # stored as UTC, tz-normalised


def test_shipment_date_epoch_millis_dispatched():
    # 2026-06-30T19:00:00Z
    fields = extract_shipping_package_fields({"shippingPackages": [{"dispatched": 1782846000000}]})
    assert fields["shipment_date"] == "2026-06-30 19:00:00"


def test_flex_uses_amazon_shipment_date_from_synthetic_order():
    order = {
        "channel": "AMAZON_FLEX",
        "created": "2026-09-09T05:00:00+00:00",  # purchase-date
        "updated": "2026-09-11T08:15:00+00:00",  # Amazon shipment-date
        "saleOrderItems": _ONE_LINE,
        "status": "COMPLETE",
    }
    assert extract_shipping_package_fields(order)["shipment_date"] == "2026-09-11 08:15:00"
    ready, _ = order_ready_for_invoice(order)
    assert ready is True


def test_non_flex_order_never_treats_updated_as_a_ship_date():
    order = {"channel": "FLIPKART", "updated": "2026-09-11T08:15:00+00:00", "status": "COMPLETE", "saleOrderItems": _ONE_LINE}
    assert extract_shipping_package_fields(order) == {}
    ready, _ = order_ready_for_invoice(order)
    assert ready is False


def test_flex_cutoff_applies_to_ship_date_not_order_date():
    base = {"channel": "AMAZON_FLEX", "status": "COMPLETE", "saleOrderItems": _ONE_LINE, "created": "2026-07-30T05:00:00+00:00"}
    shipped_before = dict(base, updated="2026-07-31T08:00:00+00:00")
    shipped_after = dict(base, updated="2026-08-02T08:00:00+00:00")
    assert order_ready_for_invoice(shipped_before)[0] is False
    assert order_ready_for_invoice(shipped_after)[0] is True


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


class _FakeSettings:
    def __init__(self, zero_discount_floor_channels):
        self.zero_discount_floor_channels = zero_discount_floor_channels


def _bogo_order(channel="DOGSEE_SITE_IN"):
    return {
        "channel": channel,
        "saleOrderItems": [
            {"sellingPriceWithoutTaxesAndDiscount": 249.0, "discount": 0.0, "quantity": 1},
            {"sellingPriceWithoutTaxesAndDiscount": 199.0, "discount": 199.0, "quantity": 1},
        ],
    }


def test_compute_uc_financials_floors_when_order_has_a_paid_line(monkeypatch):
    # Mirrors sap_payload._append_sales_line's floor guard (2026-09-01: now
    # posts UnitPrice at gross to satisfy SAP's cost floor, but applies a
    # DiscountPercent on the same line so the real invoice total nets to 0)
    # so the recon "expected" figure matches what actually gets posted,
    # instead of false-flagging a mismatch on the free line of a real BOGO
    # order.
    import unicommerce.mappers as mappers_mod

    monkeypatch.setattr(mappers_mod, "get_settings", lambda: _FakeSettings({"DOGSEE_SITE_IN": True}))
    result = compute_uc_financials(_bogo_order())
    # 249 (paid, unaffected) + 0 (free line nets to its real economic value)
    assert result["uc_taxable_value"] == pytest.approx(249.0)


def test_compute_uc_financials_stays_clamped_when_every_line_is_free(monkeypatch):
    # Same replacement-order safeguard as sap_payload: no paid line
    # anywhere in the order means this must NOT be floored, even on an
    # opted-in channel.
    import unicommerce.mappers as mappers_mod

    monkeypatch.setattr(mappers_mod, "get_settings", lambda: _FakeSettings({"DOGSEE_SITE_IN": True}))
    order = {
        "channel": "DOGSEE_SITE_IN",
        "saleOrderItems": [
            {"sellingPriceWithoutTaxesAndDiscount": 349.0, "discount": 349.0, "quantity": 1},
        ],
    }
    result = compute_uc_financials(order)
    assert result["uc_taxable_value"] == pytest.approx(0.0)


def test_compute_uc_financials_unaffected_when_channel_not_opted_in(monkeypatch):
    import unicommerce.mappers as mappers_mod

    monkeypatch.setattr(mappers_mod, "get_settings", lambda: _FakeSettings({"DOGSEE_SITE_IN": True}))
    result = compute_uc_financials(_bogo_order(channel="FLIPKART"))
    # Free line clamps to 0 same as today -- only the paid line counts.
    assert result["uc_taxable_value"] == pytest.approx(249.0)


def test_compute_uc_financials_falls_back_to_flat_tax_percentage_when_gst_fields_absent(monkeypatch):
    """Confirmed real incident 2026-09-01: flex_fetch.py's synthetic
    Amazon Flex items only carry a flat taxPercentage, never UC-native
    integratedGstPercentage/centralGstPercentage/stateGstPercentage --
    without this fallback, igst/cgst/sgst all read 0 and uc_total silently
    drops the entire tax component (SAP posted correctly regardless,
    since sap_payload.py already had this same fallback; only our own
    recorded total was wrong, understating every Flex order by its GST
    amount)."""
    import unicommerce.mappers as mappers_mod

    monkeypatch.setattr(mappers_mod, "get_settings", lambda: _FakeSettings({}))
    order = {
        "channel": "AMAZON_FLEX",
        "saleOrderItems": [
            {"sellingPriceWithoutTaxesAndDiscount": 143.22, "quantity": 1, "taxPercentage": 18},
        ],
    }
    result = compute_uc_financials(order)
    assert result["uc_taxable_value"] == pytest.approx(143.22)
    assert result["uc_igst"] == pytest.approx(round(143.22 * 0.18, 2))
    assert result["uc_total"] == pytest.approx(round(143.22 * 1.18, 2))


def test_compute_uc_financials_prefers_native_gst_fields_over_flat_taxpercentage(monkeypatch):
    """A real UC order carrying both its native GST fields and (for
    whatever reason) a stray taxPercentage must still use the native
    fields -- the fallback is for when they're genuinely absent, not a
    general override."""
    import unicommerce.mappers as mappers_mod

    monkeypatch.setattr(mappers_mod, "get_settings", lambda: _FakeSettings({}))
    order = {
        "channel": "FLIPKART",
        "saleOrderItems": [
            {
                "sellingPriceWithoutTaxesAndDiscount": 100.0, "quantity": 1,
                "centralGstPercentage": 9, "stateGstPercentage": 9,
                "taxPercentage": 999,  # must be ignored
            },
        ],
    }
    result = compute_uc_financials(order)
    assert result["uc_cgst"] == pytest.approx(9.0)
    assert result["uc_sgst"] == pytest.approx(9.0)
    assert result["uc_igst"] == pytest.approx(0.0)
    assert result["uc_total"] == pytest.approx(118.0)
