"""MFN ship-date proxy: Amazon has no true ship-date field for
merchant-fulfilled orders, so last-updated-date at the Shipped transition
stands in for it. AFN (FBA) orders are untouched by this — they keep
getting actual_ship_date from the FBA shipments report."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from amazon_us.fetch_sync import _mfn_ship_date  # noqa: E402
from amazon_us.report_parser import parse_orders_report  # noqa: E402


def test_mfn_shipped_uses_last_updated_date():
    assert _mfn_ship_date("MFN", "Shipped", "2026-07-10T18:30:00Z") == "2026-07-10"


def test_afn_orders_get_no_mfn_ship_date():
    assert _mfn_ship_date("AFN", "Shipped", "2026-07-10T18:30:00Z") is None


def test_mfn_not_yet_shipped_gets_no_ship_date():
    assert _mfn_ship_date("MFN", "Unshipped", "2026-07-10T18:30:00Z") is None


def test_missing_last_updated_date_returns_none():
    assert _mfn_ship_date("MFN", "Shipped", "") is None


def test_report_parser_extracts_channel_and_last_updated():
    tsv = (
        "amazon-order-id\torder-status\tpurchase-date\tfulfillment-channel\t"
        "last-updated-date\tship-city\tship-state\tship-postal-code\tship-country\t"
        "sku\tquantity\titem-price\titem-tax\tshipping-price\tshipping-tax\torder-item-id\tasin\tproduct-name\n"
        "111-1234567-1234567\tShipped\t2026-07-01T10:00:00Z\tMFN\t"
        "2026-07-10T18:30:00Z\tSacramento\tCalifornia\t94203\tUS\t"
        "SKU-A\t1\t10.00\t0.50\t0\t0\tOI-1\tASIN1\tWidget\n"
    )
    orders = parse_orders_report(tsv)
    header = orders["111-1234567-1234567"]
    assert header["fulfillment_channel"] == "MFN"
    assert header["last_updated_date"] == "2026-07-10T18:30:00Z"
