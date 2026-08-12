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


def _no_bom(item_code):
    return []


def test_no_inventory_movement_and_no_batches():
    payload, skipped = build_credit_note_payload(
        _order(), _event(), MAPPING, 4242, INVOICE_LINES, _resolve, _no_bom
    )
    assert skipped == []
    item = payload["DocumentLines"][0]
    assert item["WithoutInventoryMovement"] == "tYES"
    assert "BatchNumbers" not in item  # refunded FBA stock never returns to sellable


def test_base_link_and_inferred_qty():
    payload, _ = build_credit_note_payload(_order(), _event(), MAPPING, 4242, INVOICE_LINES, _resolve, _no_bom)
    item = payload["DocumentLines"][0]
    assert item["BaseType"] == 13
    assert item["BaseEntry"] == 4242
    assert item["BaseLine"] == 0
    assert item["Quantity"] == 1.0  # abs(-13.495) / 13.495
    assert payload["NumAtCard"] == "111-1234567-1234567-2026-07-01T10:00:00Z"
    assert payload["DocDate"] == "2026-07-01"


def test_shipping_reversal_lines():
    payload, _ = build_credit_note_payload(_order(), _event(), MAPPING, 4242, INVOICE_LINES, _resolve, _no_bom)
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
        _order(), event, MAPPING, 4242, INVOICE_LINES, _resolve, _no_bom
    )
    assert payload is None


def test_sku_mapping_drift_falls_back_to_price_match():
    # The current mapping says SKU-A -> FGDC-NEW, but the invoice was
    # posted back when SKU-A mapped to FGDC-OLD (SAP master data changed
    # in between -- confirmed for real 2026-08-11). The CN must reverse
    # what's ACTUALLY on the invoice (FGDC-OLD, found by matching price),
    # not what the SKU maps to today.
    drifted_mapping = {"SKU-A": "FGDC-NEW"}
    invoice_lines = [{"LineNum": 0, "ItemCode": "FGDC-OLD", "UnitPrice": 13.495}]
    payload, skipped = build_credit_note_payload(
        _order(), _event(), drifted_mapping, 4242, invoice_lines, _resolve, _no_bom
    )
    assert skipped == []
    item = payload["DocumentLines"][0]
    assert item["ItemCode"] == "FGDC-OLD"  # the real invoiced code, not today's mapping
    assert item["BaseLine"] == 0


def test_zero_qty_refund_is_skipped_with_a_reason_not_silently_dropped():
    # A $0.00 (or otherwise sub-unit) principal adjustment rounds to zero
    # quantity at the order's unit price — nothing to reverse on the item
    # line, but this must show up in `skipped`, not vanish silently (that
    # looked like an unexplained "no postable lines: []" to Finance).
    event = _event(principal_amount=0.0)
    payload, skipped = build_credit_note_payload(
        _order(), event, MAPPING, 4242, INVOICE_LINES, _resolve, _no_bom
    )
    assert payload is None
    assert len(skipped) == 1
    assert skipped[0]["seller_sku"] == "SKU-A"
    assert "rounds to zero qty" in skipped[0]["reason"]


def test_ship_to_code_matches_order_state():
    payload, _ = build_credit_note_payload(_order(), _event(), MAPPING, 4242, INVOICE_LINES, _resolve, _no_bom)
    assert payload["PayToCode"] == "Amazon USA-TX"
    assert payload["ShipToCode"] == "Amazon USA-TX"


# ── Sales BOM (bundle/combo) refunds ────────────────────────────────────

BOM_MAPPING = {"bundle_sku": "CFGDC00001"}

BOM_INVOICE_LINES = [
    {"LineNum": 0, "ItemCode": "CFGDC00001", "Quantity": 1, "UnitPrice": 0.0},
    {"LineNum": 1, "ItemCode": "CHILD-A", "Quantity": 1, "UnitPrice": 20.0},
    {"LineNum": 2, "ItemCode": "CHILD-B", "Quantity": 1, "UnitPrice": 20.0},
]


def _bom_order():
    o = _order()
    o["lines"] = [{"seller_sku": "bundle_sku", "quantity": 1, "unit_price": 40.0}]
    return o


def _bom_event(**overrides):
    adj = dict(seller_sku="bundle_sku", principal_amount=-40.0, tax_amount=0, shipping_amount=0, shipping_tax_amount=0)
    adj.update(overrides)
    return RefundEvent(
        refund_id="111-1234567-1234567-2026-07-01T10:00:00Z",
        amazon_order_id="111-1234567-1234567",
        posted_date="2026-07-01",
        item_adjustments=[RefundItemAdjustment(**adj)],
    )


def _bom_children(item_code):
    return [("CHILD-A", 1.0), ("CHILD-B", 1.0)] if item_code == "CFGDC00001" else []


def test_bom_refund_reverses_parent_and_child_rows_not_the_zero_price_parent_alone():
    payload, skipped = build_credit_note_payload(
        _bom_order(), _bom_event(), BOM_MAPPING, 4242, BOM_INVOICE_LINES, _resolve, _bom_children
    )
    assert skipped == []
    lines = payload["DocumentLines"]
    parent, child_a, child_b = lines[0], lines[1], lines[2]

    assert parent["ItemCode"] == "CFGDC00001"
    assert parent["BaseLine"] == 0
    assert parent["UnitPrice"] == 0.0
    assert parent["TreeType"] == "iSalesTree"
    assert "WithoutInventoryMovement" not in parent  # SAP rejects it on a tree row

    assert child_a["ItemCode"] == "CHILD-A"
    assert child_a["BaseLine"] == 1
    assert child_a["UnitPrice"] == 20.0  # reverses at the price actually invoiced on that row
    assert child_a["TreeType"] == "iIngredient"
    assert child_a["WithoutInventoryMovement"] == "tYES"

    assert child_b["ItemCode"] == "CHILD-B"
    assert child_b["BaseLine"] == 2
    assert child_b["UnitPrice"] == 20.0


# ── Amendment (Amazon grows a refund event's item_adjustments after we've
# already posted a CN under its refund_id) ──────────────────────────────

def test_amendment_only_reverses_the_new_remainder():
    # A prior CN already reversed 1 of the order's 2 units plus its full
    # tax/shipping; this event has since grown to cover both units
    # (confirmed for real 2026-08-12, same refund_id throughout). The
    # payload must carry only the newly-added unit and its tax delta —
    # shipping is already fully covered, so no shipping line at all.
    already_posted = {"tax": 0.59 + 0.25, "shipping": 3.99, "qty_by_item": {"FGDC0001": 1.0}}
    event = _event(principal_amount=-26.99, tax_amount=-1.18)  # 2 units now, not 1
    payload, skipped = build_credit_note_payload(
        _order(), event, MAPPING, 4242, INVOICE_LINES, _resolve, _no_bom,
        already_posted=already_posted,
    )
    assert skipped == []
    lines = payload["DocumentLines"]
    assert len(lines) == 2  # item + tax only, no shipping line
    item = lines[0]
    assert item["Quantity"] == 1.0  # 2.0 inferred - 1.0 already posted
    tax = lines[1]
    assert tax["ItemCode"] == "EXP000003"
    assert tax["UnitPrice"] == 0.59  # (1.18 + 0.25) - already-posted 0.84


def test_amendment_pool_is_consumed_not_reapplied_to_every_adjustment():
    # Real 2026-08-12 bug: an event can carry TWO item_adjustments for the
    # same item_code (2 refunded units posted as 2 separate lines, not one
    # qty-2 line). Only 1 unit was already covered by the prior CN --
    # subtracting that from EVERY adjustment (instead of draining it as a
    # pool) zeroed out both and silently lost the real second-unit delta.
    already_posted = {"tax": 0.0, "shipping": 0.0, "qty_by_item": {"FGDC0001": 1.0}}
    two_invoice_lines = [
        {"LineNum": 0, "ItemCode": "FGDC0001"},
        {"LineNum": 1, "ItemCode": "FGDC0001"},
    ]
    event = RefundEvent(
        refund_id="111-1234567-1234567-2026-07-01T10:00:00Z",
        amazon_order_id="111-1234567-1234567",
        posted_date="2026-07-01",
        item_adjustments=[
            RefundItemAdjustment(seller_sku="SKU-A", principal_amount=-13.495, tax_amount=0, shipping_amount=0, shipping_tax_amount=0),
            RefundItemAdjustment(seller_sku="SKU-A", principal_amount=-13.495, tax_amount=0, shipping_amount=0, shipping_tax_amount=0),
        ],
    )
    payload, skipped = build_credit_note_payload(
        _order(), event, MAPPING, 4242, two_invoice_lines, _resolve, _no_bom,
        already_posted=already_posted,
    )
    assert payload is not None
    lines = payload["DocumentLines"]
    assert len(lines) == 1  # one adjustment fully covered (skipped), one is the real new delta
    assert lines[0]["Quantity"] == 1.0  # the second unit, not zero


def test_amendment_fully_covered_by_prior_cn_yields_no_payload():
    # Prior CN already reversed everything this event now reports -- pure
    # rounding dust, not a real amendment. Must not manufacture a $0 CN.
    already_posted = {"tax": 0.59 + 0.25, "shipping": 3.99, "qty_by_item": {"FGDC0001": 1.0}}
    payload, skipped = build_credit_note_payload(
        _order(), _event(), MAPPING, 4242, INVOICE_LINES, _resolve, _no_bom,
        already_posted=already_posted,
    )
    assert payload is None


def test_bom_partial_refund_scales_child_quantities():
    # Original invoice had 2 bundle units; this refund is for 1 of them.
    invoice_lines = [
        {"LineNum": 0, "ItemCode": "CFGDC00001", "Quantity": 2, "UnitPrice": 0.0},
        {"LineNum": 1, "ItemCode": "CHILD-A", "Quantity": 2, "UnitPrice": 20.0},
        {"LineNum": 2, "ItemCode": "CHILD-B", "Quantity": 2, "UnitPrice": 20.0},
    ]
    payload, _ = build_credit_note_payload(
        _bom_order(), _bom_event(), BOM_MAPPING, 4242, invoice_lines, _resolve, _bom_children
    )
    lines = payload["DocumentLines"]
    assert lines[0]["Quantity"] == 1.0  # abs(-40) / 40 unit_price
    assert lines[1]["Quantity"] == 1.0  # 2 invoiced * (1/2 scale)
    assert lines[2]["Quantity"] == 1.0
