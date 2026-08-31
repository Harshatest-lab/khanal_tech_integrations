from blinkit import extract_po_header, parse_channel_lines
from zepto import (
    extract_header_from_text,
    is_valid_zepto_pdf_text,
    parse_attachment,
    resolve_po_expiry,
    _extract_po_expiry_from_text,
)


ZEPTO_SAMPLE = """
Purchase Order
PO No: ZT998877
Vendor: Khanal Foods
Shipping Address
Zepto Dark Store Whitefield
Bengaluru Karnataka 560066
GSTIN: 29ABCDE1234F1Z5
"""

PDF_WITH_EXPIRY = """
Purchase Order
PO No: P5240704
PO Expiry Date: 2026-09-02
Shipping Address
Zepto Hub
Bengaluru 560066
"""

CSV_TEMPLATE = (
    "PoNumber,Sku,SkuDesc,Quantity,UnitBaseCost,LandingCost,MRP,TotalAmount,IGSTPercentage,PoExpiryDate\n"
    "P5240704,sku1,Honey,4,100,110,199,440,5,{expiry}\n"
)


def _csv_bytes(expiry: str) -> bytes:
    return CSV_TEMPLATE.format(expiry=expiry).encode("utf-8")


def test_valid_zepto_markers():
    assert is_valid_zepto_pdf_text(ZEPTO_SAMPLE) is True
    assert is_valid_zepto_pdf_text("P.O. Number: 2173110010781 HANDS ON TRADES") is False


def test_extracts_zepto_po_and_address():
    header = extract_header_from_text(ZEPTO_SAMPLE)
    assert header["po_number"] == "ZT998877"
    assert "560066" in header["delivered_to"]
    assert header["company_name"] == "Zepto"
    assert header["gst_no"] == "29ABCDE1234F1Z5"
    assert header["po_expiry_date"] == "Not Found"


def test_extracts_zepto_po_expiry_from_pdf_text():
    assert _extract_po_expiry_from_text(PDF_WITH_EXPIRY) == "2026-09-02"
    header = extract_header_from_text(PDF_WITH_EXPIRY)
    assert header["po_number"] == "P5240704"
    assert header["po_expiry_date"] == "Not Found"


def test_zepto_expiry_csv_first():
    parsed = parse_attachment(_csv_bytes("2026-03-15 00:00:00"))
    assert resolve_po_expiry(parsed, PDF_WITH_EXPIRY) == "2026-03-15"


def test_zepto_expiry_pdf_fallback_when_csv_empty():
    parsed = parse_attachment(_csv_bytes(""))
    assert resolve_po_expiry(parsed, PDF_WITH_EXPIRY) == "2026-09-02"


def test_zepto_csv_expiry_datetime():
    parsed = parse_attachment(_csv_bytes("2026-09-02 00:00:00"))
    assert parsed["po_expiry_date"] == "2026-09-02"


def test_zepto_csv_lines():
    csv_bytes = (
        "PoNumber,Sku,SkuDesc,Quantity,UnitBaseCost,LandingCost,MRP,TotalAmount,IGSTPercentage,PoExpiryDate\n"
        "ZT998877,14108,Honey 500g,4,100,110,199,440,5,2026-03-15\n"
    ).encode("utf-8")
    parsed = parse_attachment(csv_bytes)
    assert parsed["lines"][0]["item_code"] == "14108"
    assert parsed["lines"][0]["quantity"] == 4
    assert parsed["po_expiry_date"] == "2026-03-15"
    assert "ZT998877" in parsed["po_numbers"]


def test_extract_po_header_dispatches_to_zepto(monkeypatch):
    called = {}

    def fake_extract(pdf_bytes):
        called["ok"] = pdf_bytes
        return {"po_number": "ZT1", "delivered_to": "addr", "company_name": "Zepto"}, "text"

    monkeypatch.setattr("zepto.extract_header", fake_extract)
    header, text = extract_po_header(b"pdf-bytes", "zepto")
    assert called["ok"] == b"pdf-bytes"
    assert header["po_number"] == "ZT1"
    assert text == "text"


def test_parse_channel_lines_zepto():
    csv_bytes = b"PoNumber,Sku,SkuDesc,Quantity,UnitBaseCost,LandingCost,MRP,TotalAmount\n1,SKU1,Item,2,10,11,20,22\n"
    rows = parse_channel_lines(csv_bytes, channel="zepto", filename="po.csv")
    assert rows[0]["item_code"] == "SKU1"
    assert rows[0]["total_amount"] == 22
