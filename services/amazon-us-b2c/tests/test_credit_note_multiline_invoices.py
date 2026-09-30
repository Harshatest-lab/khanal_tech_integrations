"""Credit notes against invoices that carry the same item on several lines
(order 112-7159795-8197858, 2026-09-25): a refund is spread over those lines,
each capped at its invoiced quantity, instead of one adjustment per line."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from amazon_us.credit_note_payload import build_credit_note_payload  # noqa: E402
from amazon_us.finances_api import RefundEvent, RefundItemAdjustment  # noqa: E402

MAPPING = {"SKU-A": "FGDC0001"}


def _resolve(fmt):
    return {"22200000-01-001-01": "TAXGL", "41400000-01-001-01": "SHIPGL"}[fmt]


def _no_bom(item_code):
    return []


def _order():
    return {
        "amazon_order_id": "112-7159795-8197858",
        "ship_state": "TX",
        "ship_country": "US",
        "lines": [{"seller_sku": "SKU-A", "quantity": 30, "unit_price": 19.09}],
    }


def _event(units):
    return RefundEvent(
        refund_id="112-7159795-8197858-2026-09-25T06:55:42Z",
        amazon_order_id="112-7159795-8197858",
        posted_date="2026-09-25",
        item_adjustments=[
            RefundItemAdjustment(seller_sku="SKU-A", principal_amount=-round(19.09 * u, 2), tax_amount=-1.44 * u)
            for u in units
        ],
    )


# Same item invoiced as a 1-unit line and a 29-unit line at the same price.
def _invoice_lines():
    return [
        {"LineNum": 0, "ItemCode": "FGDC0001", "Quantity": 1, "UnitPrice": 19.09},
        {"LineNum": 1, "ItemCode": "FGDC0001", "Quantity": 29, "UnitPrice": 19.09},
        {"LineNum": 2, "ItemCode": "EXP000003", "Quantity": 1, "UnitPrice": 41.76},
    ]


def _item_lines(payload):
    return [(line["BaseLine"], line["Quantity"]) for line in payload["DocumentLines"] if "BaseLine" in line]


def _build(units, invoice_lines=None):
    return build_credit_note_payload(
        _order(), _event(units), MAPPING, 65273, invoice_lines or _invoice_lines(), _resolve, _no_bom
    )


def test_refund_larger_than_first_line_spills_onto_the_next_same_item_line():
    # The real failing refund: adjustments of 4,1,1,1,2,1,1,1 units (12).
    # The 4-unit one used to be linked whole to the 1-unit base line -> SAP -10.
    payload, skipped = _build([4, 1, 1, 1, 2, 1, 1, 1])
    assert skipped == []
    lines = _item_lines(payload)
    assert lines[0] == (0, 1.0)  # first unit fills the 1-unit line
    assert lines[1] == (1, 3.0)  # rest of the 4-unit adjustment spills to line 1
    per_line: dict[int, float] = {}
    for base_line, qty in lines:
        per_line[base_line] = per_line.get(base_line, 0) + qty
    assert per_line == {0: 1.0, 1: 11.0}  # 12 units, no line above its invoiced qty


def test_single_unit_refund_still_links_to_the_first_line():
    payload, _ = _build([1])
    assert _item_lines(payload) == [(0, 1.0)]


def test_two_adjustments_fit_one_multi_unit_invoice_line():
    # Used to skip the second adjustment ("No matching invoice line found").
    payload, skipped = _build([1, 1], [{"LineNum": 0, "ItemCode": "FGDC0001", "Quantity": 2, "UnitPrice": 19.09}])
    assert skipped == []
    assert _item_lines(payload) == [(0, 1.0), (0, 1.0)]


def test_refund_beyond_everything_invoiced_fails_loudly_not_as_a_partial_credit():
    # 31 units against 30 invoiced: the excess rides on the last line so SAP
    # rejects the document (-10) rather than us silently crediting only 30.
    payload, _ = _build([31])
    assert _item_lines(payload) == [(0, 1.0), (1, 30.0)]  # line 1 holds 29 -> 1 over


def test_lines_without_a_known_quantity_keep_the_one_line_per_adjustment_behaviour():
    payload, skipped = _build(
        [1, 1],
        [
            {"LineNum": 0, "ItemCode": "FGDC0001", "UnitPrice": 19.09},
            {"LineNum": 1, "ItemCode": "FGDC0001", "UnitPrice": 19.09},
        ],
    )
    assert skipped == []
    assert _item_lines(payload) == [(0, 1.0), (1, 1.0)]
