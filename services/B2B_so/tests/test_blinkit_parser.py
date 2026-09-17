from blinkit import (
    _move_processed,
    _processed_folder,
    extract_header,
    is_amend_subject,
    is_valid_po_pdf_text,
    match_ship_to,
    normalize_gstin,
)


SAMPLE = """
Purchase Order
HANDS ON TRADES PRIVATE LIMITED
PAN: AABCH1234C
GST No.: 29AABCH1234C1Z5
P.O. Number: 2173110010781
PO expiry date: Mar 6, 2026, 11:59 pm
Delivered To: Blinkit DC Whitefield Bangalore 560066
GST No.: 29AABCH1234C1Z5
Contact Name: Warehouse Manager Phone No 99999
Partners Biz Portal
"""


def test_valid_blinkit_markers():
    assert is_valid_po_pdf_text(SAMPLE) is True


def test_extracts_po_and_address():
    header = extract_header(SAMPLE)
    assert header["po_number"] == "2173110010781"
    assert header["company_name"] == "HANDS ON TRADES PRIVATE LIMITED"
    assert "560066" in header["delivered_to"]
    assert header["po_expiry_date"] == "2026-03-06"


def test_gst_normalized():
    assert normalize_gstin("29 AABCH1234C1Z5") == "29AABCH1234C1Z5"


def test_amend_subject_detection():
    assert is_amend_subject("Revised PO 48287510062255") is True
    assert is_amend_subject("Amended purchase order 123") is True
    assert is_amend_subject("Amendment to PO 999") is True
    assert is_amend_subject("Blinkit PO 48287510062255") is False
    assert is_amend_subject("") is False


def test_ship_to_fuzzy_match():
    aliases = [
        {"address_id": "BLR-WF", "address_entry": "Blinkit DC Whitefield Bangalore 560066"},
        {"address_id": "DEL-OK", "address_entry": "Some other warehouse Delhi 110001"},
    ]
    result = match_ship_to("Blinkit DC Whitefield Bangalore 560066 GST", aliases)
    assert result["ok"] is True
    assert result["address_id"] == "BLR-WF"


def test_blinkit_ship_to_uses_pincode_not_wrong_dc():
    header = extract_header(SAMPLE)
    aliases = [
        {
            "address_id": "Blinkit DC Electronic City",
            "address_entry": "Blinkit DC Electronic City Bangalore Karnataka 560100",
        },
        {
            "address_id": "Blinkit DC Whitefield",
            "address_entry": "Blinkit DC Whitefield Bangalore 560066",
        },
    ]
    result = match_ship_to(header["delivered_to"], aliases)
    assert result["ok"] is True
    assert result["address_id"] == "Blinkit DC Whitefield"
    assert "560066" in header["delivered_to"]


def test_blinkit_ship_to_same_pincode_picks_correct_warehouse():
    delivered = "Blinkit DC Whitefield Bangalore 560066"
    aliases = [
        {
            "address_id": "Blinkit DC Hoskote",
            "address_entry": "Blinkit DC Hoskote Rural Koralur Village Bangalore 560066",
        },
        {
            "address_id": "Blinkit DC Whitefield",
            "address_entry": "Blinkit DC Whitefield Bangalore 560066",
        },
    ]
    result = match_ship_to(delivered, aliases)
    assert result["ok"] is True
    assert result["address_id"] == "Blinkit DC Whitefield"


def test_ship_to_fuzzy_match_multiline_warehouse():
    aliases = [
        {"address_id": "BB-BLR2", "address_entry": "Bangalore-2-FV-FMCG-DC\nInnovative Retail Concepts Private Limited ,  No 48 and 49\nHoskate Rural,Koralur Village\nBengaluru Urban Karnataka-560067"},
    ]
    delivered = "Warehouse Address\nBangalore-2-FV-FMCG-DC\nInnovative Retail Concepts Private Limited ,  No 48 and 49\nHoskate Rural,Koralur Village\nBengaluru Urban Karnataka-560067"
    result = match_ship_to(delivered, aliases)
    assert result["ok"] is True
    assert result["address_id"] == "BB-BLR2"


def test_ship_to_prefers_matching_pincode_over_similar_company_name():
    """Bigbasket IRA44122563: Soukya Road 560067 must not map to Dasanapura 562162."""
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
    delivered = (
        "Warehouse Address\n"
        "Bangalore-2-FV-FMCG-DC\n"
        "Innovative Retail Concepts Private Limited , No 48 and 49,Soukya Road,Samethanahalli1 1 1\n"
        "Hoskate Rural,Koralur Village\n"
        "Bengaluru Urban Karnataka-560067"
    )
    result = match_ship_to(delivered, aliases)
    assert result["ok"] is True
    assert result["address_id"] == "Scootsy BLR Ecom2"


def test_ship_to_rejects_different_pincode_even_if_company_name_matches():
    aliases = [
        {
            "address_id": "IRCPL - Bangalore - Dasanapura",
            "address_entry": "Bangalore-FMCG-DC Innovative Retail Concepts Pvt Ltd , SyNo12 1 2 4 SyNo14 1 Adakimaranhalli 1 1 1 Dasanpura Hobli , Makli Post North Taluk Bengaluru - 562162.",
        },
    ]
    delivered = (
        "Bangalore-2-FV-FMCG-DC\n"
        "Innovative Retail Concepts Private Limited , No 48 and 49,Soukya Road,Samethanahalli1 1 1\n"
        "Hoskate Rural,Koralur Village\n"
        "Bengaluru Urban Karnataka-560067"
    )
    result = match_ship_to(delivered, aliases)
    assert result["ok"] is False


def test_ship_to_same_pincode_disambiguates_by_warehouse_text():
    aliases = [
        {
            "address_id": "Scootsy BLR Ecom1",
            "address_entry": "SCOOTSY Hoskate Rural Koralur Village Bengaluru Urban Karnataka - 560067.",
        },
        {
            "address_id": "Scootsy BLR Ecom2",
            "address_entry": "SCOOTSY LOGISTICS ECOM EXPRESS Ekarajapura Village Sulibele Hobli Bangalore Karnataka - 560067.",
        },
    ]
    delivered = "SCOOTSY LOGISTICS ECOM EXPRESS Ekarajapura Village Sulibele Hobli Bangalore Karnataka - 560067"
    result = match_ship_to(delivered, aliases)
    assert result["ok"] is True
    assert result["address_id"] == "Scootsy BLR Ecom2"


def test_processed_folder_name():
    assert _processed_folder("Blinkit") == "Blinkit-Processed"


def test_move_processed_flag():
    import os

    os.environ["B2B_MOVE_PROCESSED_MAIL"] = "true"
    assert _move_processed() is True
    os.environ["B2B_MOVE_PROCESSED_MAIL"] = "false"
    assert _move_processed() is False


def test_folder_for_channel_from_folders(monkeypatch):
    import blinkit

    monkeypatch.setattr(
        blinkit,
        "_load_imap_config",
        lambda: {
            "folders": {
                "blinkit": "Blinkit",
                "zepto": "Zepto",
            },
            "card_codes": {"blinkit": "C03570", "zepto": "C99999"},
        },
    )
    blinkit._imap_config_cache = None
    assert blinkit.folder_for_channel("zepto") == "Zepto"
    assert blinkit.card_code_for_channel("zepto") == "C99999"
    assert blinkit.list_fetch_channels() == ["blinkit", "zepto"]


def test_folder_for_channel_legacy_top_level_folder(monkeypatch):
    import blinkit

    monkeypatch.setattr(
        blinkit,
        "_load_imap_config",
        lambda: {"folder": "Blinkit"},
    )
    blinkit._imap_config_cache = None
    assert blinkit.folder_for_channel("blinkit") == "Blinkit"
    assert blinkit.list_fetch_channels() == ["blinkit"]
