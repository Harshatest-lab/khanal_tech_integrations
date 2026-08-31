from bigbasket import extract_from_rows, parse_excel_attachment
from blinkit import extract_po_header, parse_channel_lines


def test_extracts_bigbasket_gstin_after_warehouse_pincode():
    rows = [
        ["Warehouse Address", "Delivery Address"],
        ["Bangalore-2-FV-FMCG-DC", "Bangalore-2-FV-FMCG-DC"],
        ["Innovative Retail Concepts Private Limited ,  No 48 and 49", "Innovative Retail Concepts Private Limited ,  No 48 and 49"],
        ["Hoskate Rural,Koralur Village", "Hoskate Rural,Koralur Village"],
        ["Bengaluru Urban Karnataka-560067", "Bengaluru Urban Karnataka-560067"],
        ["GSTIN NO: 29AACCI2053A1Z3", "GSTIN NO:GST: 29AACCI2053A1Z3"],
        ["", "Supplier"],
        ["", "49364 - Khanal Foods Private Limited-BLR"],
        ["", "GSTIN No: 29AAFCK9270L1ZU"],
        ["PO Number:IRA30826412", "", "PO Expiry date:18/Dec/2025"],
        ["S.No", "SkuCode", "Description", "Quantity", "Basic Cost", "Landing Cost", "MRP", "GST Amount", "Total Value"],
        ["1", "40198148", "Honey", "6", "346.99", "364.35", "599", "104.16", "2186.1"],
    ]
    parsed = extract_from_rows(rows)
    assert parsed["gst_no"] == "29AACCI2053A1Z3"
    assert parsed["po_number"] == "IRA30826412"
    assert "560067" in parsed["delivered_to"]
    assert "29AACCI2053A1Z3" not in parsed["delivered_to"]
    assert "Delivery Address" not in parsed["delivered_to"]


def test_extracts_bigbasket_po_from_rows():
    rows = [
        ["PO Number: BB12345"],
        ["PO Expiry date: 15/Mar/2026"],
        ["Warehouse Address"],
        ["BB Now Whitefield Warehouse"],
        ["Bengaluru 560066"],
        ["S.No", "SkuCode", "Description", "Quantity", "Basic Cost", "Landing Cost", "MRP", "GST Amount", "Total Value"],
        ["1", "SKU9", "Honey", "6", "80", "88", "150", "8", "528"],
        ["Total", "", "", "6", "", "", "", "", "528"],
    ]
    parsed = extract_from_rows(rows)
    assert parsed["po_number"] == "BB12345"
    assert parsed["po_expiry_date"] == "2026-03-15"
    assert "560066" in parsed["delivered_to"]
    assert parsed["lines"][0]["item_code"] == "SKU9"
    assert parsed["lines"][0]["quantity"] == 6
    assert parsed["lines"][0]["total_amount"] == 528


def test_bigbasket_html_xls():
    html = """
    <table>
      <tr><td>PO Number: BB777</td></tr>
      <tr><td>Warehouse Address</td></tr>
      <tr><td>Hub Hyderabad 500081</td></tr>
      <tr><td>S.No</td><td>SKU Code</td><td>Quantity</td><td>Total Value</td></tr>
      <tr><td>1</td><td>FG01</td><td>3</td><td>99.5</td></tr>
      <tr><td>Total</td><td></td><td>3</td><td>99.5</td></tr>
    </table>
    """
    parsed = parse_excel_attachment(html.encode("utf-8"), "po.xls")
    assert parsed["po_number"] == "BB777"
    assert parsed["lines"][0]["item_code"] == "FG01"


def test_extract_po_header_rejects_bigbasket_pdf_path():
    try:
        extract_po_header(b"%PDF", "bigbasket")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "Excel" in str(exc)
        assert "Blinkit" not in str(exc)


def test_parse_channel_lines_bigbasket_html():
    html = """
    <table>
      <tr><td>PO Number: BB1</td></tr>
      <tr><td>S.No</td><td>SkuCode</td><td>Quantity</td><td>Total Value</td></tr>
      <tr><td>1</td><td>A1</td><td>2</td><td>10</td></tr>
    </table>
    """
    rows = parse_channel_lines(html.encode("utf-8"), channel="bigbasket", filename="bb.xls")
    assert rows[0]["item_code"] == "A1"
