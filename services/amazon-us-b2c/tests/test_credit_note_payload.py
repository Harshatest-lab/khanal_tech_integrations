"""Credit note rules: no inventory movement, shipping reversal, inferred
quantity, base-linking."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from amazon_us.credit_note_payload import build_credit_note_payload  # noqa: E402
from amazon_us.finances_api import RefundEvent, RefundItemAdjustment  # noqa: E402

MAPPING = {"SKU-A": "FGDC0001"}


def _order():
    return {
        "amazon_order_id": "111-1234567-1234567",
        "purchase_date": "2026-06-10 08:15:00",
        "actual_ship_date": "2026-06-12",
        "ship_city": "Austin",
        "ship_state": "TX",
        "ship_postal_code": "73301",
        "ship_country": "US",
        "buyer_name": "",
        "sap_doc_entry": 4242,
        "lines": [{"seller_sku": "SKU-A", "quantity": 2, "unit_price": 13.495}],
    }


def _event(**overrides):
    adj = dict(
        seller_sku="SKU-A", principal_amount=-13.495, tax_amount=-0.59,
        shipping_amount=-3.99, shipping_tax_amount=-0.25,
    )
    adj.update(overrides)
    return RefundEvent(
        refund_id="111-1234567-1234567-2026-07-01T10:00:00Z",
        amazon_order_id="111-1234567-1234567",
        posted_date="2026-07-01",
        item_adjustments=[RefundItemAdjustment(**adj)],
    )


INVOICE_LINES = [
    {"LineNum": 0, "ItemCode": "FGDC0001", "BatchNumbers": [{"BatchNumber": "B1", "Quantity": 2}]}
]


def _resolve(fmt):
    return {"22200000-01-001-01": "TAXGL", "41400000-01-001-01": "SHIPGL"}[fmt]


def test_no_inventory_movement_and_no_batches():
    payload, skipped = build_credit_note_payload(
        _order(), _event(), MAPPING, 4242, INVOICE_LINES, _resolve
    )
    assert skipped == []
    item = payload["DocumentLines"][0]
    assert item["WithoutInventoryMovement"] == "tYES"
    assert "BatchNumbers" not in item  # refunded FBA stock never returns to sellable


def test_base_link_and_inferred_qty():
    payload, _ = build_credit_note_payload(_order(), _event(), MAPPING, 4242, INVOICE_LINES, _resolve)
    item = payload["DocumentLines"][0]
    assert item["BaseType"] == 13
    assert item["BaseEntry"] == 4242
    assert item["BaseLine"] == 0
    assert item["Quantity"] == 1.0  # abs(-13.495) / 13.495
    assert payload["NumAtCard"] == "111-1234567-1234567-2026-07-01T10:00:00Z"
    assert payload["DocDate"] == "2026-07-01"


def test_shipping_reversal_lines():
    payload, _ = build_credit_note_payload(_order(), _event(), MAPPING, 4242, INVOICE_LINES, _resolve)
    lines = payload["DocumentLines"]
    assert len(lines) == 3
    tax = lines[1]
    assert tax["ItemCode"] == "EXP000003"
    assert tax["UnitPrice"] == round(0.59 + 0.25, 2)  # product tax + shipping tax folded
    ship = lines[2]
    assert ship["ItemCode"] == "EXP000004"
    assert ship["UnitPrice"] == 3.99
    for line in lines:
        assert line["TaxCode"] == "etax0"  # every line needs a TaxCode or SAP rejects


def test_unmatched_sku_is_skipped_not_posted():
    event = _event(seller_sku="SKU-OTHER")
    payload, skipped = build_credit_note_payload(
        _order(), event, MAPPING, 4242, INVOICE_LINES, _resolve
    )
    assert payload is None
    assert skipped[0]["seller_sku"] == "SKU-OTHER"


def test_ship_to_code_matches_order_state():
    payload, _ = build_credit_note_payload(_order(), _event(), MAPPING, 4242, INVOICE_LINES, _resolve)
    assert payload["PayToCode"] == "Amazon USA-TX"
    assert payload["ShipToCode"] == "Amazon USA-TX"
