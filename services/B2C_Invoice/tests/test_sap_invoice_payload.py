"""Tests for Walmart order charge parsing and SAP Invoice payload helpers."""

from walmart.order_pricing import line_quantity, parse_line_charges
from walmart.sap_invoice_payload import _order_date_str


def test_parse_line_charges_product_only() -> None:
    line = {
        "charges": {
            "charge": [
                {
                    "chargeType": "PRODUCT",
                    "chargeName": "ItemPrice",
                    "chargeAmount": {"amount": 28.99, "currency": "USD"},
                    "tax": {"taxAmount": {"amount": 2.54}},
                }
            ]
        },
        "orderLineQuantity": {"amount": "1"},
    }
    charges = parse_line_charges(line)
    assert charges["unit_price"] == 28.99
    assert charges["product_tax"] == 2.54
    assert charges["shipping"] is None
    assert line_quantity(line) == 1.0


def test_order_date_str_from_ms() -> None:
    # 2026-07-01-ish ms example
    assert _order_date_str(1782921272364).count("-") == 2
