"""The destination city rides in the document's address override for invoices and credit notes alike."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from amazon_us.invoice_payload import build_address_extension  # noqa: E402


def test_domestic_order_sends_the_city_only():
    assert build_address_extension({"ship_city": "Austin"}, "US", True) == {"ShipToCity": "Austin", "BillToCity": "Austin"}


def test_foreign_order_sends_city_and_country():
    ext = build_address_extension({"ship_city": " Toronto "}, "CA", False)
    assert ext == {"ShipToCity": "Toronto", "BillToCity": "Toronto", "ShipToCountry": "CA", "BillToCountry": "CA"}


def test_no_city_means_no_city_fields():
    assert build_address_extension({"ship_city": ""}, "US", True) == {}
    assert build_address_extension({}, "CA", False) == {"ShipToCountry": "CA", "BillToCountry": "CA"}


def test_city_is_cut_to_sap_limit():
    assert len(build_address_extension({"ship_city": "x" * 300}, "US", True)["ShipToCity"]) == 100
