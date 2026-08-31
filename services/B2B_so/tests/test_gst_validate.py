from gst_validate import extract_gst_from_text, normalize_gstin, validate_gst_match


def test_normalize_gstin():
    assert normalize_gstin("29 AABCH1234C1Z5") == "29AABCH1234C1Z5"


def test_extract_gst_from_shipping_block():
    text = """
    Shipping Address
    Zepto Dark Store
    Bengaluru 560066
    GSTIN: 29ABCDE1234F1Z5
    """
    assert extract_gst_from_text(text) == "29ABCDE1234F1Z5"


def test_extract_gst_bigbasket_gstin_no_formats():
    text = """
    Warehouse Address
    Bangalore-2-FV-FMCG-DC
    Bengaluru Urban Karnataka-560067
    GSTIN NO: 29AACCI2053A1Z3
    Supplier
    GSTIN No: 29AAFCK9270L1ZU
    """
    assert extract_gst_from_text(text) == "29AACCI2053A1Z3"

    assert extract_gst_from_text("GSTIN NO:GST: 29AACCI2053A1Z3") == "29AACCI2053A1Z3"


def test_extract_gst_prefers_second_labeled():
    text = """
    GST No.: 29AAAAAAAAAAAAA
    Delivered To: Warehouse
    GST No.: 29BBBBBBBBBBBBB
    """
    assert extract_gst_from_text(text) == "29BBBBBBBBBBBBB"


def test_validate_gst_match_ok():
    sap_addr = {"GSTIN": "29AABCH1234C1Z5"}
    bp = {"GSTIN": "29OTHER0000000"}
    assert validate_gst_match("29 AABCH1234C1Z5", sap_addr, bp) is None


def test_validate_gst_match_fails():
    err = validate_gst_match("29AAAA", {"GSTIN": "29BBBBBBBBBBBBB"}, {})
    assert err and "GST mismatch" in err
