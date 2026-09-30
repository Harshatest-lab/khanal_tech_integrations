"""Finances API returns SellerSKU HTML-escaped; staged order lines carry it raw."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from amazon_us import finances_api  # noqa: E402


def _raw(sku):
    return {
        "AmazonOrderId": "113-1823665-9234661",
        "PostedDate": "2026-09-10T03:00:00Z",
        "ShipmentItemAdjustmentList": [
            {
                "SellerSKU": sku,
                "ItemChargeAdjustmentList": [
                    {"ChargeType": "Principal", "ChargeAmount": {"CurrencyAmount": "-18.71"}}
                ],
            }
        ],
    }


def test_refund_event_sku_is_html_unescaped_to_match_staged_order_lines():
    # orders 111-2977129-8568210 ("chews_skin&amp;coat") and
    # 113-1823665-9234661 ("Foodtopper_S&amp;C") were skipped as unmapped.
    event = finances_api._parse_refund_event(_raw("Foodtopper_S&amp;C"))
    assert event.item_adjustments[0].seller_sku == "Foodtopper_S&C"


def test_plain_sku_is_unchanged():
    event = finances_api._parse_refund_event(_raw("DC_L_1P"))
    assert event.item_adjustments[0].seller_sku == "DC_L_1P"
