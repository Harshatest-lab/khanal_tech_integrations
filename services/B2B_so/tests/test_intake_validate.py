import intake_validate


def test_missing_expiry_address_gst_and_lines():
    errors = intake_validate.validate_intake_data(
        "blinkit",
        {"po_expiry_date": "Not Found", "delivered_to": "", "gst_no": "Not Found"},
        [],
    )
    assert any("expiry date" in e.lower() for e in errors)
    assert any("delivery address" in e.lower() for e in errors)
    assert any("gst number" in e.lower() for e in errors)
    assert any("line items" in e.lower() for e in errors)


def test_skip_lines_when_blinkit_has_no_excel():
    errors = intake_validate.validate_intake_data(
        "blinkit",
        {"po_expiry_date": "2026-03-06", "delivered_to": "addr", "gst_no": "29AAAAA0000A1Z5"},
        [],
        skip_lines=True,
    )
    assert errors == []


def test_zero_quantity_lines_blocked():
    errors = intake_validate.validate_intake_data(
        "zepto",
        {"po_expiry_date": "2026-03-06", "delivered_to": "addr", "gst_no": "29AAAAA0000A1Z5"},
        [{"item_code": "SKU1", "quantity": 0}],
    )
    assert any("quantities are zero" in e.lower() for e in errors)


def test_zepto_pdf_csv_po_mismatch():
    err = intake_validate.zepto_pdf_csv_po_mismatch("P5240704", {"P9999999"})
    assert err and "mismatch" in err.lower()


def test_zepto_pdf_csv_po_match_ok():
    assert intake_validate.zepto_pdf_csv_po_mismatch("P5240704", {"P5240704", "P5240704"}) is None


def test_join_errors_dedupes():
    out = intake_validate.join_errors(
        "PO expiry date could not be read.",
        "PO expiry date could not be read. No line items.",
    )
    assert out.count("PO expiry date") == 1
    assert "No line items" in out
