"""Invoice payload rules: gross pricing, ship-date refusal, verified UDFs,
tax/shipping GL lines, all-or-nothing line posting."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from amazon_us.invoice_payload import build_invoice_payload, build_line_udfs  # noqa: E402


def _order(**overrides):
    order = {
        "amazon_order_id": "111-1234567-1234567",
        "purchase_date": "2026-06-10 08:15:00",
        "actual_ship_date": "2026-06-12",
        "buyer_name": "",
        "ship_city": "Sacramento",
        "ship_state": "California",
        "ship_postal_code": "94203-1234",
        "ship_country": "US",
        "lines": [
            {
                "seller_sku": "SKU-A",
                "quantity": 2,
                "unit_price": 13.495,
                "item_price_amount": 26.99,
                "item_tax_amount": 1.18,
                "shipping_price_amount": 3.99,
                "shipping_tax_amount": 0.25,
            }
        ],
    }
    order.update(overrides)
    return order


MAPPING = {"SKU-A": "FGDC0001"}


def _allocate(item_code, qty, warehouse):
    return [{"BatchNumber": "B1", "Quantity": qty}]


def _resolve(fmt):
    return {"22200000-01-001-01": "_SYS00000000059", "41400000-01-001-01": "_SYS00000000084"}[fmt]


def _no_bom(item_code):
    return []


def test_gross_pricing_and_structure():
    payload, skipped = build_invoice_payload(_order(), MAPPING, _allocate, _resolve, _no_bom)
    assert skipped == []
    assert payload["NumAtCard"] == "111-1234567-1234567"
    assert payload["DocDate"] == "2026-06-12"  # ship date, never purchase date
    lines = payload["DocumentLines"]
    assert len(lines) == 3  # item + tax + shipping
    item = lines[0]
    assert item["UnitPrice"] == 13.495  # gross — no promo netting anywhere
    assert item["TaxCode"] == "etax0"
    assert item["BatchNumbers"] == [{"BatchNumber": "B1", "Quantity": 2.0}]
    tax = lines[1]
    assert tax["ItemCode"] == "EXP000003"
    assert tax["UnitPrice"] == round(1.18 + 0.25, 2)  # item tax + shipping tax
    assert tax["AccountCode"] == "_SYS00000000059"
    ship = lines[2]
    assert ship["ItemCode"] == "EXP000004"
    assert ship["UnitPrice"] == 3.99  # gross shipping
    assert ship["AccountCode"] == "_SYS00000000084"


def test_refuses_without_ship_date():
    payload, skipped = build_invoice_payload(
        _order(actual_ship_date=None), MAPPING, _allocate, _resolve, _no_bom
    )
    assert payload is None
    assert skipped == [{"reason": "ship_date_missing"}]


def test_all_or_nothing_on_unmapped_sku():
    order = _order()
    order["lines"].append({"seller_sku": "SKU-UNMAPPED", "quantity": 1, "unit_price": 5.0})
    payload, skipped = build_invoice_payload(order, MAPPING, _allocate, _resolve, _no_bom)
    assert payload is None  # partial invoices can never reconcile with the MTR
    assert any(s.get("seller_sku") == "SKU-UNMAPPED" for s in skipped)


def test_line_udfs_verified_set_only():
    udfs = build_line_udfs(_order())
    assert udfs["U_Order"] == "111-1234567-1234567"
    assert udfs["U_State"] == "CA"  # normalized to USPS code
    assert udfs["U_City"] == "Sacramento"
    assert udfs["U_PINCode"] == "94203-1234"
    assert udfs["U_OrderedOn"] == "2026-06-10"
    # U_Channel / U_OrderID do not exist on INC INV1 — SL drops them silently.
    assert "U_Channel" not in udfs
    assert "U_OrderID" not in udfs


def test_udf_truncation_to_editsize():
    udfs = build_line_udfs(_order(ship_city="A" * 40))
    assert len(udfs["U_City"]) == 20


def test_non_batch_item_omits_batchnumbers():
    payload, _ = build_invoice_payload(_order(), MAPPING, lambda *a: [], _resolve, _no_bom)
    assert "BatchNumbers" not in payload["DocumentLines"][0]


def test_ship_to_code_is_per_order_state():
    payload, _ = build_invoice_payload(_order(), MAPPING, _allocate, _resolve, _no_bom)
    # "Amazon USA-CA" — this customer master's actual address-code
    # convention, confirmed against SAP BPAddresses (not a bare state
    # code like Walmart's).
    assert payload["PayToCode"] == "Amazon USA-CA"
    assert payload["ShipToCode"] == "Amazon USA-CA"


def test_ship_to_code_falls_back_without_state():
    payload, _ = build_invoice_payload(_order(ship_state=""), MAPPING, _allocate, _resolve, _no_bom)
    assert payload["PayToCode"] == "Amazon USA-B2C"  # constants.SAP_PAY_TO_CODE default
    assert payload["ShipToCode"] == "Amazon USA-B2C"
    assert "AddressExtension" not in payload


def test_non_us_order_uses_address_extension_not_state_prefix():
    # A Canadian province in ship_state must never become "Amazon USA-CA"
    # — that code already means California. Non-US ships fall back to the
    # generic code and carry the real country via AddressExtension instead.
    order = _order(ship_state="Alberta", ship_country="CA")
    payload, _ = build_invoice_payload(order, MAPPING, _allocate, _resolve, _no_bom)
    assert payload["PayToCode"] == "Amazon USA-B2C"
    assert payload["ShipToCode"] == "Amazon USA-B2C"
    assert payload["AddressExtension"] == {"ShipToCountry": "CA", "BillToCountry": "CA"}


# ── Sales BOM (bundle/combo) items ─────────────────────────────────────

BOM_MAPPING = {"bundle_sku": "CFGDC00001"}


def _bom_children(item_code):
    return [("CHILD-A", 1.0), ("CHILD-B", 1.0)] if item_code == "CFGDC00001" else []


def _bom_allocate(item_code, qty, warehouse):
    return [{"BatchNumber": f"B-{item_code}", "Quantity": qty}]


def test_bom_parent_line_is_zero_price_sales_tree():
    order = _order(lines=[{
        "seller_sku": "bundle_sku", "quantity": 1, "unit_price": 40.0,
        "item_price_amount": 40.0, "item_tax_amount": 0, "shipping_price_amount": 0, "shipping_tax_amount": 0,
    }])
    payload, skipped = build_invoice_payload(order, BOM_MAPPING, _bom_allocate, _resolve, _bom_children)
    assert skipped == []
    parent = payload["DocumentLines"][0]
    assert parent["ItemCode"] == "CFGDC00001"
    assert parent["Quantity"] == 1
    assert parent["UnitPrice"] == 0.0
    assert parent["TreeType"] == "iSalesTree"
    assert "BatchNumbers" not in parent  # parent never carries batch data


def test_bom_children_get_own_lines_with_batches_and_price_split():
    order = _order(lines=[{
        "seller_sku": "bundle_sku", "quantity": 1, "unit_price": 40.0,
        "item_price_amount": 40.0, "item_tax_amount": 0, "shipping_price_amount": 0, "shipping_tax_amount": 0,
    }])
    payload, _ = build_invoice_payload(order, BOM_MAPPING, _bom_allocate, _resolve, _bom_children)
    lines = payload["DocumentLines"]
    child_a, child_b = lines[1], lines[2]
    assert child_a["ItemCode"] == "CHILD-A"
    assert child_a["TreeType"] == "iIngredient"
    assert child_a["Quantity"] == 1.0
    assert child_a["BatchNumbers"] == [{"BatchNumber": "B-CHILD-A", "Quantity": 1.0}]
    assert child_b["ItemCode"] == "CHILD-B"
    assert child_b["TreeType"] == "iIngredient"
    # Even split of the bundle's $40 across 2 components, at their own qty.
    assert child_a["UnitPrice"] == 20.0
    assert child_b["UnitPrice"] == 20.0
    assert round(child_a["UnitPrice"] * child_a["Quantity"] + child_b["UnitPrice"] * child_b["Quantity"], 2) == 40.0


def test_bom_child_quantity_scales_by_qty_per_unit():
    def two_per_unit(item_code):
        return [("CHILD-A", 2.0)] if item_code == "CFGDC00001" else []

    order = _order(lines=[{
        "seller_sku": "bundle_sku", "quantity": 3, "unit_price": 10.0,
        "item_price_amount": 30.0, "item_tax_amount": 0, "shipping_price_amount": 0, "shipping_tax_amount": 0,
    }])
    payload, _ = build_invoice_payload(order, BOM_MAPPING, _bom_allocate, _resolve, two_per_unit)
    child = payload["DocumentLines"][1]
    assert child["Quantity"] == 6.0  # 2 per bundle unit * 3 bundles ordered
