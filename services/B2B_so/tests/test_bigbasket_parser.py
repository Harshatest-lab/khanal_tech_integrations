from bigbasket import BIGBASKET_CARD_NAME, extract_from_rows, parse_excel_attachment
from blinkit import extract_po_header, match_ship_to, parse_channel_lines


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
    assert parsed["company_name"] == BIGBASKET_CARD_NAME
    assert parsed["company_name"] == "Innovative Retail Concepts Private Limited"
    assert parsed["gst_no"] == "29AACCI2053A1Z3"
    assert parsed["po_number"] == "IRA30826412"
    assert "560067" in parsed["delivered_to"]
    assert "29AACCI2053A1Z3" not in parsed["delivered_to"]
    assert "Delivery Address" not in parsed["delivered_to"]


def test_bigbasket_ira44122563_ship_to_uses_pincode_not_dasanapura():
    rows = [
        ["Warehouse Address", "Delivery Address"],
        ["Bangalore-2-FV-FMCG-DC", "Bangalore-2-FV-FMCG-DC"],
        ["Innovative Retail Concepts Private Limited , No 48 and 49,Soukya Road,Samethanahalli1 1 1"],
        ["Hoskate Rural,Koralur Village"],
        ["Bengaluru Urban Karnataka-560067"],
        ["GSTIN NO: 29AACCI2053A1Z3"],
        ["PO Number:IRA44122563", "", "PO Expiry date:18/Dec/2025"],
        ["S.No", "SkuCode", "Description", "Quantity", "Basic Cost", "Landing Cost", "MRP", "GST Amount", "Total Value"],
        ["1", "40198148", "Honey", "6", "346.99", "364.35", "599", "104.16", "2186.1"],
    ]
    parsed = extract_from_rows(rows)
    aliases = [
        {
            "address_id": "IRCPL - Bangalore - Dasanapura",
            "address_entry": "Bangalore-FMCG-DC Innovative Retail Concepts Pvt Ltd , SyNo12 1 2 4 SyNo14 1 Adakimaranhalli 1 1 1 Dasanpura Hobli , Makli Post North Taluk Bengaluru - 562162.",
        },
        {
            "address_id": "Scootsy BLR Ecom2",
            "address_entry": "SCOOTSY LOGISTICS PRIVATE LIMITED ECOM EXPRESS LIMITED 2/3, 3/1, 3/2, 21/1, 21/3, Ekarajapura Village, Sulibele Hobli, Bangalore, Karnataka - 560067.",
        },
    ]
    result = match_ship_to(parsed["delivered_to"], aliases)
    assert result["ok"] is True
    assert result["address_id"] == "Scootsy BLR Ecom2"


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
