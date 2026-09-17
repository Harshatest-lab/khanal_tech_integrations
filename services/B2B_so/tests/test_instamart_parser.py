from blinkit import match_ship_to
from instamart import (
    extract_delivered_to_from_text,
    extract_header_from_text,
    is_valid_instamart_pdf_text,
    lines_from_item_details,
)


INSTAMART_SAMPLE = """
SCOOTSY LOGISTICS PRIVATE LIMITED
PO No : IM1234567890
PO Expiry Date : Mar 15, 2026
Vendor Name : Khanal Foods Private Limited
"""


def test_valid_instamart_markers():
    assert is_valid_instamart_pdf_text(INSTAMART_SAMPLE) is True
    assert is_valid_instamart_pdf_text("Blinkit purchase order") is False


def test_extracts_instamart_po_and_expiry():
    header = extract_header_from_text(INSTAMART_SAMPLE)
    assert header["po_number"] == "IM1234567890"
    assert header["company_name"] == "SCOOTSY LOGISTICS PRIVATE LIMITED"
    assert header["po_expiry_date"] == "2026-03-15"


FC5PO233897_ADDRESS_BLOCK = """
Vendor Name : PO No : FC5PO233897
PO Expiry Date: Sep 19, 2025
Billing Address Shipping Address
SCOOTSY LOGISTICS PRIVATE LIMITED
IndoSpace Logistics Park Luhari, B200 VPO Luhari
Pataudi-Kulana Road,MDR-132
Jhajjar, Haryana - 124108, India
ggnpod.procurement@scootsy.com
Contact : 9991832930
GSTIN :06AAVCS1691R1Z9
SCOOTSY LOGISTICS PRIVATE LIMITED
IndoSpace Logistics Park Luhari, B200 VPO Luhari
Pataudi-Kulana Road,MDR-132
Jhajjar, Haryana - 124108, India
ggnpod.procurement@scootsy.com
Contact : 9991832930
GSTIN :06AAVCS1691R1Z9
S.
No
Item
Code
1 14108 Himalayan Natives Organic Bilona Ghee
"""


def test_extracts_shipping_address_from_fc5_pdf_text():
    delivered = extract_delivered_to_from_text(FC5PO233897_ADDRESS_BLOCK)
    assert "SCOOTSY LOGISTICS PRIVATE LIMITED" in delivered
    assert "124108" in delivered
    assert "Jhajjar" in delivered
    assert "ggnpod" not in delivered


def test_fc5_header_includes_pdf_shipping_address():
    header = extract_header_from_text(FC5PO233897_ADDRESS_BLOCK)
    assert header["po_number"] == "FC5PO233897"
    assert "124108" in header["delivered_to"]
    assert header["gst_no"] == "06AAVCS1691R1Z9"


MBIPO_XML_SNIPPET = """<?xml version="1.0"?>
<Workbook xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
  <Worksheet ss:Name="Sheet1">
    <Table>
      <Row>
        <Cell ss:Index="12"><Data ss:Type="String">PO No :</Data></Cell>
        <Cell ss:Index="19"><Data ss:Type="String">MBIPO261442</Data></Cell>
      </Row>
      <Row>
        <Cell ss:Index="12"><Data ss:Type="String">PO Expiry Date:</Data></Cell>
        <Cell ss:Index="19"><Data ss:Type="String">Oct 17, 2025</Data></Cell>
      </Row>
      <Row>
        <Cell ss:Index="1"><Data ss:Type="String">Billing Address</Data></Cell>
        <Cell ss:Index="12"><Data ss:Type="String">Shipping Address</Data></Cell>
      </Row>
      <Row>
        <Cell ss:Index="1"><Data ss:Type="String">SCOOTSY LOGISTICS PRIVATE LIMITED&#10;Survey no 95/2&#10;Bangalore, Karnataka - 562114, India</Data></Cell>
        <Cell ss:Index="12"><Data ss:Type="String">SCOOTSY LOGISTICS PRIVATE LIMITED&#10;Survey no 95/2&#10;Bangalore, Karnataka - 562114, India</Data></Cell>
      </Row>
      <Row>
        <Cell ss:Index="1"><Data ss:Type="String">S.</Data></Cell>
        <Cell ss:Index="2"><Data ss:Type="String">Item Code</Data></Cell>
      </Row>
      <Row>
        <Cell ss:Index="1"><Data ss:Type="String">No</Data></Cell>
      </Row>
      <Row>
        <Cell ss:Index="1"><Data ss:Type="Number">1</Data></Cell>
        <Cell ss:Index="2"><Data ss:Type="String">14108</Data></Cell>
        <Cell ss:Index="3"><Data ss:Type="String">Honey Ghee</Data></Cell>
        <Cell ss:Index="4"><Data ss:Type="String">04059020</Data></Cell>
        <Cell ss:Index="6"><Data ss:Type="String">24</Data></Cell>
        <Cell ss:Index="7"><Data ss:Type="Number">949</Data></Cell>
        <Cell ss:Index="8"><Data ss:Type="Number">469.52</Data></Cell>
        <Cell ss:Index="10"><Data ss:Type="Number">11268.57</Data></Cell>
        <Cell ss:Index="11"><Data ss:Type="Number">2.5</Data></Cell>
        <Cell ss:Index="13"><Data ss:Type="Number">281.71</Data></Cell>
        <Cell ss:Index="14"><Data ss:Type="Number">2.5</Data></Cell>
        <Cell ss:Index="16"><Data ss:Type="Number">281.71</Data></Cell>
        <Cell ss:Index="17"><Data ss:Type="Number">0</Data></Cell>
        <Cell ss:Index="18"><Data ss:Type="Number">0</Data></Cell>
        <Cell ss:Index="20"><Data ss:Type="Number">0</Data></Cell>
        <Cell ss:Index="21"><Data ss:Type="Number">0</Data></Cell>
        <Cell ss:Index="22"><Data ss:Type="Number">0</Data></Cell>
        <Cell ss:Index="23"><Data ss:Type="Number">11832</Data></Cell>
      </Row>
    </Table>
  </Worksheet>
</Workbook>
"""


def test_xml_xls_reads_address_and_items_from_nested_cells():
    from instamart import read_xml_excel

    parsed = read_xml_excel(MBIPO_XML_SNIPPET)
    assert parsed["PO Details"]["PO No"] == "MBIPO261442"
    assert parsed["PO Details"]["PO Expiry Date"] == "2025-10-17"
    assert "562114" in parsed["Main Address"]
    assert len(parsed["Item Details"]) == 1
    assert parsed["Item Details"][0]["Item Code"] == "14108"
    assert parsed["Item Details"][0]["Quantity"] == 24


def test_apply_excel_to_header_prefers_excel_address():
    from instamart import apply_excel_to_header

    header = {"po_number": "Not Found", "delivered_to": "pdf-address", "po_expiry_date": "Not Found", "gst_no": "Not Found"}
    apply_excel_to_header(
        header,
        {
            "Main Address": "SCOOTSY LOGISTICS PRIVATE LIMITED Bangalore - 562114",
            "Shipping Address": "SCOOTSY LOGISTICS PRIVATE LIMITED Bangalore - 562114\nGSTIN :29AAVCS1691R1Z1",
            "PO Details": {"PO No": "MBIPO261442", "PO Expiry Date": "Oct 17, 2025"},
        },
    )
    assert header["delivered_to"].startswith("SCOOTSY")
    assert header["po_number"] == "MBIPO261442"
    assert header["po_expiry_date"] == "2025-10-17"
    assert header["gst_no"] == "29AAVCS1691R1Z1"


def test_instamart_lines_map_to_pipeline_rows():
    rows = lines_from_item_details(
        [
            {
                "Item Code": "SKU1",
                "Item Description": "Honey 500g",
                "Quantity": 10,
                "Unit Base Cost (INR)": 120.5,
                "IGST": 5,
                "Taxable Value (INR)": 1205,
                "MRP": 199,
                "Total (INR)": 1265.25,
            }
        ]
    )
    assert rows[0]["item_code"] == "SKU1"
    assert rows[0]["quantity"] == 10
    assert rows[0]["basic_cost_price"] == 120.5
    assert rows[0]["total_amount"] == 1265.25


def test_instamart_ship_to_uses_pincode_not_wrong_bangalore_dc():
    """Same matcher as Bigbasket: 560067 must not map to Dasanapura 562162."""
    delivered = (
        "SCOOTSY LOGISTICS PRIVATE LIMITED ECOM EXPRESS LIMITED "
        "2/3, 3/1, 3/2, 21/1, 21/3, Ekarajapura Village, Sulibele Hobli, "
        "Bangalore, Karnataka - 560067"
    )
    aliases = [
        {
            "address_id": "IRCPL - Bangalore - Dasanapura",
            "address_entry": (
                "Bangalore-FMCG-DC Innovative Retail Concepts Pvt Ltd , SyNo12 1 2 4 "
                "SyNo14 1 Adakimaranhalli 1 1 1 Dasanpura Hobli , Makli Post North Taluk "
                "Bengaluru - 562162."
            ),
        },
        {
            "address_id": "Scootsy BLR Ecom2",
            "address_entry": (
                "SCOOTSY LOGISTICS PRIVATE LIMITED ECOM EXPRESS LIMITED 2/3, 3/1, 3/2, "
                "21/1, 21/3, Ekarajapura Village, Sulibele Hobli, Bangalore, Karnataka - 560067."
            ),
        },
    ]
    result = match_ship_to(delivered, aliases)
    assert result["ok"] is True
    assert result["address_id"] == "Scootsy BLR Ecom2"


def test_instamart_ship_to_same_pincode_picks_correct_warehouse():
    """Two warehouses at 560067: fuzzy text among pin-matched aliases decides."""
    delivered = (
        "SCOOTSY LOGISTICS PRIVATE LIMITED ECOM EXPRESS LIMITED "
        "Ekarajapura Village, Sulibele Hobli, Bangalore, Karnataka - 560067"
    )
    aliases = [
        {
            "address_id": "Scootsy BLR Ecom1",
            "address_entry": (
                "SCOOTSY LOGISTICS PRIVATE LIMITED Some Other Hub "
                "Hoskate Rural, Koralur Village, Bengaluru Urban Karnataka - 560067."
            ),
        },
        {
            "address_id": "Scootsy BLR Ecom2",
            "address_entry": (
                "SCOOTSY LOGISTICS PRIVATE LIMITED ECOM EXPRESS LIMITED "
                "Ekarajapura Village, Sulibele Hobli, Bangalore, Karnataka - 560067."
            ),
        },
    ]
    result = match_ship_to(delivered, aliases)
    assert result["ok"] is True
    assert result["address_id"] == "Scootsy BLR Ecom2"
