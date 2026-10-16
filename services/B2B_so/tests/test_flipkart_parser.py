import io

import openpyxl
import pytest

import flipkart
from flipkart import parse_excel

ITEM_HEADER = [
    "S.No", "FSN/ISBN13", "Title", "HSN/SA Code", "Quantity", "Supplier MRP", "Supplier Price",
    "Taxable Value", "IGST Rate", "Tax Amount", "Total Amount",
]

SHEET_ROWS = [
    ["Flipkart Internet Pvt Ltd", "PO#PO12345", "", "PO Expiry: 15-03-26"],
    [
        "SHIPPED TO ADDRESS",
        "Flipkart Warehouse, Hoskote Road, Bengaluru, Karnataka 560067 "
        "GSTIN: 29AACCI2053A1Z3 STATE CODE: 29",
    ],
    ITEM_HEADER,
    ["1", "HONEY001", "Honey 500g", "0409", "6", "599", "346.99", "2081.94", "5", "104.1", "2186.04"],
    ["2", "GHEE002", "Ghee 1L", "0405", "2", "700", "500", "1000", "12", "120", "1120"],
    ["", "Total Quantity= 8"],
    ["Important Notification", "ignored"],
]


def _xlsx_bytes(rows):
    book = openpyxl.Workbook()
    for row in rows:
        book.active.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_reads_header_fields_from_xlsx():
    parsed = parse_excel(_xlsx_bytes(SHEET_ROWS))
    assert parsed["po_number"] == "PO12345"
    assert parsed["po_expiry_date"] == "2026-03-15"
    assert parsed["gst_no"] == "29AACCI2053A1Z3"
    assert "560067" in parsed["delivered_to"]
    assert "GSTIN" not in parsed["delivered_to"]


def test_reads_line_items_and_stops_at_total():
    lines = parse_excel(_xlsx_bytes(SHEET_ROWS))["lines"]
    assert [row["item_code"] for row in lines] == ["HONEY001", "GHEE002"]
    first = lines[0]
    assert first["quantity"] == 6
    assert first["mrp"] == 599
    assert first["basic_cost_price"] == 346.99
    assert first["tax_amount"] == 104.1
    assert first["total_amount"] == 2186.04


def test_reads_binary_xls_with_xlrd(monkeypatch):
    """No .xls writer is installed, so fake the xlrd sheet to prove the .xls branch is used."""
    import types

    class FakeSheet:
        nrows, ncols = len(SHEET_ROWS), max(len(r) for r in SHEET_ROWS)

        def cell_value(self, r, c):
            row = SHEET_ROWS[r]
            return row[c] if c < len(row) else ""

    fake_book = types.SimpleNamespace(sheet_by_index=lambda i: FakeSheet())
    fake_xlrd = types.SimpleNamespace(open_workbook=lambda file_contents: fake_book)
    monkeypatch.setitem(__import__("sys").modules, "xlrd", fake_xlrd)

    parsed = parse_excel(b"\xd0\xcf\x11\xe0 fake")
    assert parsed["po_number"] == "PO12345"
    assert len(parsed["lines"]) == 2


def test_rejects_file_without_po_number():
    with pytest.raises(ValueError, match="missing PO#"):
        parse_excel(_xlsx_bytes([["nothing useful"]]))


ALIASES = [
    {"address_id": "BLR-DC", "address_entry": "Flipkart Warehouse, Hoskote Road, Bengaluru, Karnataka 560067"},
    {"address_id": "DEL-DC", "address_entry": "Flipkart Hub, Plot 9, Gurgaon, Haryana 122001"},
]


def test_address_match_picks_closest_saved_address():
    assert flipkart.match_address("Flipkart Warehouse, Hoskote Road, Bengaluru, Karnataka 560067", ALIASES) == "BLR-DC"
    assert flipkart.match_address("Totally different place", ALIASES) == ""


def test_tax_code_intrastate_vs_interstate():
    assert flipkart.tax_code("KT", 12) == "KACS12"
    assert flipkart.tax_code("MH", 12) == "KAIG12"


def test_check_address_uses_sap_address_state(monkeypatch):
    partner = {"BPAddresses": [{"AddressName": "BLR-DC", "State": "KT", "GSTIN": "29AACCI2053A1Z3"}]}
    monkeypatch.setattr(flipkart, "get_business_partner", lambda code: partner)
    parsed = {"delivered_to": ALIASES[0]["address_entry"], "gst_no": "29AACCI2053A1Z3"}
    assert flipkart.check_address(parsed, ALIASES, "C1") == ("BLR-DC", "KT", "")

    monkeypatch.setattr(flipkart, "get_business_partner", lambda code: {"BPAddresses": []})
    _name, _state, error = flipkart.check_address(parsed, ALIASES, "C1")
    assert "No matching SAP address" in error


def test_check_items_maps_sku_and_picks_tax_code(monkeypatch):
    monkeypatch.setattr(
        flipkart, "get_items", lambda codes: {"SAP-HONEY": {"ItemName": "Honey", "U_TaxRate": 5}}
    )
    lines = [{"item_code": "HONEY001"}, {"item_code": "UNKNOWN9"}]
    checked, errors = flipkart.check_items(lines, {"HONEY001": "SAP-HONEY"}, "MH")
    assert checked[0]["sap_code"] == "SAP-HONEY"
    assert checked[0]["taxcode"] == "KAIG5"
    assert checked[1]["sap_code"] == ""
    assert errors == ["No mapping found for item codes: UNKNOWN9"]


REAL_LAYOUT_ROWS = [
    ["PO#", "FAGWN08514362", "", "PO Expiry", "19-10-26"],
    ["BILLED TO ADDRESS", "", "Flipkart Billed Address", "GSTIN", "29AABCF8078M1ZX", "SHIPPED TO ADDRESS", "",
     "Flipkart India Pvt. Ltd., Survey No 518, Bengaluru 560103", "GSTIN", "29AABCF8078M1ZX", "State Code", "IN-KA"],
    ["S. no.", "HSN/SA Code", "FSN/ISBN13", "Quantity", "Title", "Supplier MRP", "Supplier Price", "Taxable Value", "IGST Rate", "SGST/UTGST Rate", "CGST Rate", "Tax Amount", "Total Amount"],
    ["1.0", "04059020", "GHEFUS2NFYJRHDDF", "72.0", "Ghee 500 ml", "949.0 INR", "660.0 INR", "45257.76 INR", "0.0%", "2.5%", "2.5%", "2262.24 INR", "47520.00"],
    ["", "Total Quantity=", "72"],
]


def test_real_flipkart_layout():
    parsed = parse_excel(_xlsx_bytes(REAL_LAYOUT_ROWS))
    assert parsed["po_expiry_date"] == "2026-10-19"
    assert parsed["delivered_to"] == "Flipkart India Pvt. Ltd., Survey No 518, Bengaluru 560103"
    assert parsed["gst_no"] == "29AABCF8078M1ZX"
    assert parsed["state_code"] == "KA"
    line = parsed["lines"][0]
    # 'Supplier Price' (660) includes GST: SAP gets the price before tax, 45257.76 / 72.
    assert (line["mrp"], line["basic_cost_price"], line["landing_rate"], line["tax_amount"]) == (949.0, 628.58, 660.0, 2262.24)
    assert line["igst_percentage"] == 5.0  # intrastate: CGST 2.5 + SGST 2.5
    # SAP adds 5% to the price before tax; Flipkart rounds tax per unit, so allow paise of difference.
    assert abs(line["basic_cost_price"] * line["quantity"] * 1.05 - line["total_amount"]) < 1


def test_connect_mailbox_reports_missing_label(monkeypatch):
    class FakeMail:
        def __init__(self, *args): pass
        def login(self, *args): pass
        def select(self, name): return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
        def list(self): return "OK", [b'(\\HasNoChildren) "/" "Flipkart-Orders"', b'(\\HasNoChildren) "/" "Blinkit"']

    monkeypatch.setattr(flipkart.imaplib, "IMAP4_SSL", FakeMail)
    settings = {"imap_server": "x", "email": "e", "password": "p", "folders": {"flipkart": "Flipkart"}}
    with pytest.raises(RuntimeError, match="Flipkart-Orders"):
        flipkart.connect_mailbox(settings)


SAP_ADDRESSES = [{"AddressName": "Flipkart BLR Anekal (FBAWG)", "State": "KT", "GSTIN": "29AABCF8078M1ZX"}]
PARSED_NEW = {
    "delivered_to": "Flipkart India Pvt. Ltd., Survey No 99, Hoskote Bangalore Urban Karnataka 562114 Bangalore 562114",
    "gst_no": "29AABCF8078M1ZX",
}


def test_build_new_address_uses_sap_state_and_splits_the_po_address():
    address = flipkart.build_new_address(PARSED_NEW, "Flipkart BLR Hoskote (FBHK)", SAP_ADDRESSES)
    assert address["State"] == "KT"  # copied from the SAP address with the same GSTIN, not 'IN-KA'
    assert address["ZipCode"] == "562114"
    assert address["City"] == "Bangalore"
    assert address["Street"].endswith("562114")
    assert address["AddressType"] == "bo_ShipTo"


@pytest.mark.parametrize(
    "name, parsed, message",
    [
        ("", PARSED_NEW, "empty"),
        ("Flipkart BLR Anekal (FBAWG)", PARSED_NEW, "already has"),
        ("Flipkart X", {**PARSED_NEW, "gst_no": "not found"}, "not a valid GSTIN"),
        ("Flipkart X", {**PARSED_NEW, "gst_no": "27AAAAA0000A1Z5"}, "different company"),
        ("Flipkart X", {**PARSED_NEW, "delivered_to": "no pincode here"}, "pincode"),
    ],
)
def test_build_new_address_refuses_bad_input(name, parsed, message):
    with pytest.raises(ValueError, match=message):
        flipkart.build_new_address(parsed, name, SAP_ADDRESSES)


def test_add_address_sends_one_patch_and_checks_it_afterwards(monkeypatch):
    import b2b_sappost

    sent = []
    monkeypatch.setattr(b2b_sappost, "_request", lambda method, path, body=None: sent.append((method, path, body)) or (204, {}))
    reads = iter([{"BPAddresses": SAP_ADDRESSES}, {"BPAddresses": SAP_ADDRESSES + [{"AddressName": "Flipkart BLR Hoskote (FBHK)"}]}])
    monkeypatch.setattr(flipkart, "get_business_partner", lambda code: next(reads))
    result = flipkart.add_address_to_sap("C1", PARSED_NEW, "Flipkart BLR Hoskote (FBHK)")
    assert [(m, p) for m, p, _ in sent] == [("PATCH", "BusinessPartners('C1')")]
    assert sent[0][2]["BPAddresses"][0]["AddressName"] == "Flipkart BLR Hoskote (FBHK)"
    assert (result["addresses_before"], result["addresses_after"]) == (1, 2)


def test_add_address_fails_loudly_when_sap_did_not_keep_it(monkeypatch):
    import b2b_sappost

    monkeypatch.setattr(b2b_sappost, "_request", lambda method, path, body=None: (204, {}))
    monkeypatch.setattr(flipkart, "get_business_partner", lambda code: {"BPAddresses": SAP_ADDRESSES})
    with pytest.raises(RuntimeError, match="look wrong afterwards"):
        flipkart.add_address_to_sap("C1", PARSED_NEW, "Flipkart BLR Hoskote (FBHK)")


def test_suggest_address_name_follows_sap_style():
    parsed = {"po_number": "FNNWN08523565", "delivered_to": "Flipkart, Block-3 NEW DELHI - 110061 New Delhi 110061"}
    assert flipkart.suggest_address_name(parsed) == "Flipkart New Delhi (FNNW)"


class FakeConn:
    pass


def _auto_add_setup(monkeypatch, sap_names, aliases):
    calls = {"added": [], "aliases": []}
    monkeypatch.setattr(flipkart.store, "list_ship_to_aliases", lambda conn, ch: aliases)
    monkeypatch.setattr(flipkart.store, "upsert_ship_to_alias", lambda conn, name, text, channel: calls["aliases"].append(name))
    monkeypatch.setattr(flipkart, "get_business_partner", lambda code: {"BPAddresses": [{"AddressName": n} for n in sap_names]})
    monkeypatch.setattr(flipkart, "add_address_to_sap", lambda code, parsed, name: calls["added"].append(name) or {"address": {}})
    return calls


PARSED_AUTO = {"po_number": "FAGWN1", "delivered_to": "Flipkart Hoskote 562114 Bangalore 562114", "gst_no": "29X"}


def test_new_warehouse_is_added_to_sap_and_the_ship_to_map(monkeypatch):
    calls = _auto_add_setup(monkeypatch, sap_names=["Other"], aliases=[])
    assert flipkart.ensure_address_in_sap(FakeConn(), PARSED_AUTO, "C1") == ""
    assert calls["added"] == ["Flipkart Bangalore (FAGW)"]
    assert calls["aliases"] == ["Flipkart Bangalore (FAGW)"]


def test_known_warehouse_is_not_added_again(monkeypatch):
    name = "Flipkart BLR Hoskote (X)"
    aliases = [{"address_id": name, "address_entry": PARSED_AUTO["delivered_to"]}]
    calls = _auto_add_setup(monkeypatch, sap_names=[name], aliases=aliases)
    assert flipkart.ensure_address_in_sap(FakeConn(), PARSED_AUTO, "C1") == ""
    assert calls == {"added": [], "aliases": []}


def test_alias_exists_but_sap_lacks_it_adds_to_sap_only(monkeypatch):
    name = "Flipkart BLR Hoskote (X)"
    aliases = [{"address_id": name, "address_entry": PARSED_AUTO["delivered_to"]}]
    calls = _auto_add_setup(monkeypatch, sap_names=["Other"], aliases=aliases)
    assert flipkart.ensure_address_in_sap(FakeConn(), PARSED_AUTO, "C1") == ""
    assert calls == {"added": [name], "aliases": []}


def test_sap_refusal_becomes_a_po_error_not_a_crash(monkeypatch):
    _auto_add_setup(monkeypatch, sap_names=[], aliases=[])
    def refuse(code, parsed, name):
        raise RuntimeError("SAP refused")
    monkeypatch.setattr(flipkart, "add_address_to_sap", refuse)
    assert "SAP refused" in flipkart.ensure_address_in_sap(FakeConn(), PARSED_AUTO, "C1")


def test_inbox_row_for_new_po_and_silence_when_nothing_changed(monkeypatch):
    rows = []
    monkeypatch.setattr(flipkart.store, "add_notification", lambda conn, **kw: rows.append(kw))
    flipkart.notify_inbox(FakeConn(), "PO1", "ready", "", None)
    assert rows[-1]["kind"] == "success" and rows[-1]["title"] == "Stored: PO PO1" and rows[-1]["channel"] == "flipkart"
    flipkart.notify_inbox(FakeConn(), "PO1", "needs_review", "No mapping found", None)
    assert rows[-1]["kind"] == "error" and rows[-1]["body"] == "No mapping found"
    count = len(rows)
    flipkart.notify_inbox(FakeConn(), "PO1", "needs_review", "No mapping found", {"pipeline_status": "needs_review", "last_error": "No mapping found"})
    assert len(rows) == count


def test_new_gstin_of_same_company_takes_its_state_from_the_po_state_code():
    parsed = {**PARSED_NEW, "gst_no": "27AABCF8078M1Z7", "state_code": "MH"}  # same PAN as the SAP address
    sap_addresses = [{"AddressName": "x", "State": "KT", "GSTIN": "29AABCF8078M1ZX"}]
    address = flipkart.build_new_address(parsed, "Flipkart Pune (FPNE)", sap_addresses)
    assert address["State"] == "MH" and address["GSTIN"] == "27AABCF8078M1Z7"


def test_new_gstin_without_a_po_state_code_is_refused():
    parsed = {**PARSED_NEW, "gst_no": "27AABCF8078M1Z7", "state_code": ""}
    sap_addresses = [{"AddressName": "x", "State": "KT", "GSTIN": "29AABCF8078M1ZX"}]
    with pytest.raises(ValueError, match="no State Code"):
        flipkart.build_new_address(parsed, "Flipkart Pune (FPNE)", sap_addresses)


FULL_PO_ROWS = [
    ["Flipkart", "", "PURCHASE ORDER # P1", "Registered Address", "Flipkart HQ, Bangalore"],
    ["PO#", "P1", "Nature of Transaction", "Intra-State", "PO Expiry", "19-10-26", "CATEGORY", "Gourmet"],
    ["Billed by", "Supplier Rd", "GSTIN", "29AAFCK9270L1ZU", "State Code", "IN-KA",
     "Shipped From", "Supplier Rd", "GSTIN", "29AAFCK9270L1ZU", "State Code", "IN-KA"],
    ["SHIPPED TO ADDRESS", "Warehouse 560103", "GSTIN", "29AABCF8078M1ZX", "State Code", "IN-KA"],
    ["PAYMENT DETAILS"],
    ["MODE OF PAYMENT", "EFT", "CREDIT TERM", "PAY BY 21 DAYS"],
    ["APPROVAL DETAILS"],
    ["NAME", "_", "EMAIL", "_"],
    ["ORDER DETAILS"],
    ["S. no.", "HSN/SA Code", "FSN/ISBN13", "Quantity", "UOM", "Brand", "Taxable Value", "Total Amount"],
    ["1.0", "04059020", "ITEM1", "2.0", "pcs", "Himalayan Natives", "100.00 INR", "105.00"],
    ["", "Total Quantity=", "2", "Total=", "100.00 INR", "5.00 INR", "105.00 INR"],
    ["Important Notification"],
    ["1. Please mention PO number on the invoice"],
]


def test_full_extraction_keeps_every_header_field_and_every_item_column():
    parsed = parse_excel(_xlsx_bytes(FULL_PO_ROWS))
    header = parsed["all_header_fields"]
    assert header["Title"] == "Flipkart PURCHASE ORDER # P1"
    assert header["Nature of Transaction"] == "Intra-State" and header["CATEGORY"] == "Gourmet"
    assert header["Billed by - GSTIN"] == "29AAFCK9270L1ZU"
    assert header["SHIPPED TO ADDRESS - GSTIN"] == "29AABCF8078M1ZX"  # each address block keeps its own GSTIN
    assert header["SHIPPED TO ADDRESS - State Code"] == "IN-KA"
    assert header["MODE OF PAYMENT"] == "EFT" and header["Approval Details - NAME"] == "_"
    assert parsed["totals"] == {
        "Total Quantity": "2", "Total Taxable Value": "100.00 INR", "Total Tax": "5.00 INR", "Total Amount": "105.00 INR",
    }  # the 'Important Notification' legal text is not PO data and is not extracted
    assert parsed["lines"][0]["columns"] == {
        "S. no.": "1.0", "HSN/SA Code": "04059020", "FSN/ISBN13": "ITEM1", "Quantity": "2.0", "UOM": "pcs",
        "Brand": "Himalayan Natives", "Taxable Value": "100.00 INR", "Total Amount": "105.00",
    }


# --- the whole fetch for one email, with the mailbox, DB, S3 and SAP replaced by fakes -------------


def _email_with(attachment: bytes | None, filename: str = "po.xlsx") -> bytes:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = "New PO"
    msg.set_content("PO attached")
    if attachment:
        msg.add_attachment(attachment, maintype="application", subtype="octet-stream", filename=filename)
    return msg.as_bytes()


class FakeMailbox:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.moves = []  # IMAP commands used to move an email

    def uid(self, command, uid, *args):
        if command == "fetch":
            return "OK", [(b"1 (RFC822)", self.raw), b")"]
        self.moves.append((command, uid, *args))
        return "OK", [b""]

    def create(self, name):
        self.moves.append(("create", name))

    def expunge(self):
        self.moves.append(("expunge",))


class RecordingConn:
    def commit(self): pass
    def rollback(self): pass


def _world(monkeypatch, *, sku_map, sap_items):
    """Replace everything outside flipkart.py. Returns a dict that records what was written."""
    seen = {"upserts": [], "s3": [], "processed": [], "notifications": [], "existing": None, "archived": [], "audits": []}
    address = "Flipkart India Pvt. Ltd., Survey No 518, Bengaluru 560103"
    monkeypatch.setattr(flipkart.store, "list_ship_to_aliases", lambda conn, ch: [{"address_id": "BLR-DC", "address_entry": address}])
    monkeypatch.setattr(flipkart.store, "sku_map_dict", lambda conn, ch: sku_map)
    monkeypatch.setattr(flipkart.store, "get_po", lambda conn, po, channel: seen["existing"])
    monkeypatch.setattr(
        flipkart.store, "upsert_purchase_order",
        lambda conn, header, lines, force_update=False: seen["upserts"].append((header, lines, force_update)) or {"pipeline_status": header["pipeline_status"]},
    )
    monkeypatch.setattr(flipkart.store, "archive_po_for_amend", lambda conn, detail, email_subject, email_uid: seen["archived"].append(email_subject) or "Amended/1")
    monkeypatch.setattr(flipkart.store, "add_audit", lambda conn, **kw: seen["audits"].append(kw["action"]))
    monkeypatch.setattr(flipkart, "_warning_exists", lambda conn, po, title: seen.get("warned", False))
    monkeypatch.delenv("B2B_MOVE_PROCESSED_MAIL", raising=False)
    monkeypatch.delenv("B2B_ALERT_FROM_EMAIL", raising=False)
    monkeypatch.delenv("B2B_ALERT_ADMIN_EMAIL", raising=False)
    monkeypatch.setattr(flipkart.store, "delete_po", lambda conn, po, channel: None)
    monkeypatch.setattr(flipkart.store, "mark_email_processed", lambda conn, ch, uid, po: seen["processed"].append((ch, uid, po)))
    monkeypatch.setattr(flipkart.store, "add_notification", lambda conn, **kw: seen["notifications"].append(kw))
    monkeypatch.setattr(flipkart, "get_business_partner", lambda code: {"BPAddresses": [{"AddressName": "BLR-DC", "State": "KT", "GSTIN": "29AABCF8078M1ZX"}]})
    monkeypatch.setattr(flipkart, "get_items", lambda codes: sap_items)
    monkeypatch.setattr(flipkart, "_save_to_s3", lambda key, body: seen["s3"].append((key, body)))
    return seen


GOOD_ITEM = {"FGHN0065": {"ItemName": "HN Organic Bilona Ghee - 500ML", "U_TaxRate": 5, "U_GstTax": "GST@5", "U_IgstTax": "IGST@5"}}


def test_one_email_end_to_end_ready(monkeypatch):
    excel = _xlsx_bytes(REAL_LAYOUT_ROWS)
    seen = _world(monkeypatch, sku_map={"GHEFUS2NFYJRHDDF": "FGHN0065"}, sap_items=GOOD_ITEM)
    result = flipkart.process_email(RecordingConn(), FakeMailbox(_email_with(excel)), "1", "C00770")

    header, lines, _force = seen["upserts"][0]
    assert result == {"uid": "1", "po_number": "FAGWN08514362", "status": "ready", "last_error": "", "mail_moved": False}
    assert header["channel"] == "flipkart" and header["pipeline_status"] == "ready"
    assert (header["matched_address_name"], header["matched_state"], header["bill_to"]) == ("BLR-DC", "KT", "Local")
    assert header["customer_code"] == "C00770" and header["email_uid"] == "1" and header["email_subject"] == "New PO"
    assert header["po_expiry_date"] == "2026-10-19" and header["total_quantity"] == 72
    assert header["xlsx_s3_key"] == "flipkart/Flipkart_FAGWN08514362.xlsx"
    assert (lines[0]["sap_code"], lines[0]["taxcode"], lines[0]["basic_cost_price"]) == ("FGHN0065", "KACS5", 628.58)
    assert seen["s3"] == [("flipkart/Flipkart_FAGWN08514362.xlsx", excel)]  # original file stored untouched
    assert seen["processed"] == [("flipkart", "1", "FAGWN08514362")]
    assert seen["notifications"][0]["kind"] == "success" and seen["notifications"][0]["channel"] == "flipkart"


def test_one_email_with_an_unmapped_item_needs_review(monkeypatch):
    seen = _world(monkeypatch, sku_map={}, sap_items={})
    result = flipkart.process_email(RecordingConn(), FakeMailbox(_email_with(_xlsx_bytes(REAL_LAYOUT_ROWS))), "1", "C00770")
    assert result["status"] == "needs_review" and "No mapping found for item codes: GHEFUS2NFYJRHDDF" in result["last_error"]
    assert seen["notifications"][0]["kind"] == "error"
    assert seen["processed"]  # still marked processed: the user fixes the map and uses Recheck


def test_email_without_an_excel_is_kept_as_a_failed_row(monkeypatch):
    seen = _world(monkeypatch, sku_map={}, sap_items={})
    result = flipkart.process_email(RecordingConn(), FakeMailbox(_email_with(None)), "7", "C00770")
    header, lines, _force = seen["upserts"][0]
    assert result["status"] == "failed" and "no Excel" in result["error"]
    assert header["po_number"] == "UNPARSED-flipkart-7" and header["pipeline_status"] == "failed" and lines == []
    assert seen["notifications"][0]["title"] == "Could not extract purchase order"
    assert seen["s3"] == [] and seen["processed"] == []  # nothing stored, so the email is retried next fetch


def test_a_file_that_is_not_a_flipkart_po_is_kept_as_a_failed_row(monkeypatch):
    _world(monkeypatch, sku_map={}, sap_items={})
    result = flipkart.process_email(RecordingConn(), FakeMailbox(_email_with(_xlsx_bytes([["just", "some", "sheet"]]))), "8", "C00770")
    assert result["status"] == "failed" and "missing PO#" in result["error"]


def test_dashboard_fetch_and_recheck_use_the_flipkart_module(monkeypatch):
    import blinkit
    import handlers

    monkeypatch.setattr(flipkart, "run_fetch", lambda max_messages=None: {"who": "flipkart", "max": max_messages})
    monkeypatch.setattr(blinkit, "run_fetch", lambda channel, max_messages=None: {"who": "blinkit", "channel": channel})
    monkeypatch.setattr(blinkit, "send_alert", lambda *a, **k: None)
    assert handlers.fetch_handler({"channel": "flipkart", "max_messages": 2}, None) == {"who": "flipkart", "max": 2}
    assert handlers.fetch_handler({"channel": "zepto"}, None) == {"who": "blinkit", "channel": "zepto"}
    assert handlers.fetch_handler({}, None)["channel"] == "blinkit"  # the default is unchanged


# --- duplicate / amended POs ------------------------------------------------------------------

EXISTING_PO = {"header": {"po_number": "FAGWN08514362", "pipeline_status": "posted", "last_error": "", "xlsx_s3_key": "flipkart/old.xls"}, "lines": []}


def test_amend_words_in_the_subject():
    assert all(flipkart.is_amend_subject(s) for s in ("Amended PO", "PO REVISED", "amendment 2", "Revise"))
    assert not any(flipkart.is_amend_subject(s) for s in ("New PO", "", "Flipkart"))


def test_a_second_email_for_an_existing_po_without_amend_in_the_subject_is_skipped_with_a_warning(monkeypatch):
    seen = _world(monkeypatch, sku_map={"GHEFUS2NFYJRHDDF": "FGHN0065"}, sap_items=GOOD_ITEM)
    seen["existing"] = EXISTING_PO
    result = flipkart.process_email(RecordingConn(), FakeMailbox(_email_with(_xlsx_bytes(REAL_LAYOUT_ROWS))), "3", "C00770")
    assert result["status"] == "skipped" and result["reason"] == "duplicate_without_amend_subject"
    assert seen["upserts"] == [] and seen["s3"] == [] and seen["processed"] == []  # nothing changed, email stays in the label
    assert seen["notifications"][0]["kind"] == "warning" and seen["notifications"][0]["title"] == "Skipped duplicate PO FAGWN08514362"


def test_the_duplicate_warning_is_not_repeated_on_every_fetch(monkeypatch):
    seen = _world(monkeypatch, sku_map={}, sap_items={})
    seen["existing"], seen["warned"] = EXISTING_PO, True
    flipkart.process_email(RecordingConn(), FakeMailbox(_email_with(_xlsx_bytes(REAL_LAYOUT_ROWS))), "3", "C00770")
    assert seen["notifications"] == []


def test_an_amended_email_replaces_the_po_even_when_it_was_posted(monkeypatch):
    seen = _world(monkeypatch, sku_map={"GHEFUS2NFYJRHDDF": "FGHN0065"}, sap_items=GOOD_ITEM)
    seen["existing"] = EXISTING_PO
    mail = FakeMailbox(_email_with(_xlsx_bytes(REAL_LAYOUT_ROWS)).replace(b"Subject: New PO", b"Subject: Amended PO"))
    result = flipkart.process_email(RecordingConn(), mail, "3", "C00770")
    header, lines, force_update = seen["upserts"][0]
    assert seen["archived"] == ["Amended PO"] and seen["audits"] == ["amend_archive"]  # old version is kept in the amend history
    assert force_update is True and header["duplicate_records_status"] == "Amended/1"
    assert result["status"] == "ready" and lines[0]["sap_code"] == "FGHN0065"
    assert seen["notifications"][0]["title"] == "Amended PO FAGWN08514362" and seen["notifications"][0]["kind"] == "warning"


# --- moving the email to <label>-Processed -----------------------------------------------------


def test_a_ready_po_moves_its_email_when_moving_is_on(monkeypatch):
    _world(monkeypatch, sku_map={"GHEFUS2NFYJRHDDF": "FGHN0065"}, sap_items=GOOD_ITEM)
    monkeypatch.setenv("B2B_MOVE_PROCESSED_MAIL", "true")
    mail = FakeMailbox(_email_with(_xlsx_bytes(REAL_LAYOUT_ROWS)))
    result = flipkart.process_email(RecordingConn(), mail, "1", "C00770", "Flipkart")
    assert result["mail_moved"] is True
    assert mail.moves == [("create", '"Flipkart-Processed"'), ("copy", "1", '"Flipkart-Processed"'), ("store", "1", "+FLAGS", r"(\Deleted)"), ("expunge",)]


def test_the_email_stays_when_moving_is_off_or_the_po_needs_review(monkeypatch):
    _world(monkeypatch, sku_map={"GHEFUS2NFYJRHDDF": "FGHN0065"}, sap_items=GOOD_ITEM)
    mail = FakeMailbox(_email_with(_xlsx_bytes(REAL_LAYOUT_ROWS)))
    assert flipkart.process_email(RecordingConn(), mail, "1", "C00770", "Flipkart")["mail_moved"] is False  # switch is off
    assert mail.moves == []

    _world(monkeypatch, sku_map={}, sap_items={})  # unmapped item -> needs_review
    monkeypatch.setenv("B2B_MOVE_PROCESSED_MAIL", "true")
    assert flipkart.process_email(RecordingConn(), mail, "1", "C00770", "Flipkart")["mail_moved"] is False
    assert mail.moves == []


def test_mail_is_ready_only_for_a_ready_po_without_errors():
    assert flipkart.mail_is_ready("ready", "")
    assert not flipkart.mail_is_ready("ready", "No mapping found")
    assert not flipkart.mail_is_ready("needs_review", "")


# --- the team email ----------------------------------------------------------------------------


def test_team_email_is_sent_through_ses_when_configured(monkeypatch):
    import boto3

    sent = []

    class FakeSes:
        def send_email(self, **kw): sent.append(kw)

    monkeypatch.setattr(boto3, "client", lambda name, **kw: FakeSes())
    assert flipkart.send_po_email("P1", "needs a map") is False and sent == []  # not configured: nothing is sent
    monkeypatch.setenv("B2B_ALERT_FROM_EMAIL", "from@example.com")
    monkeypatch.setenv("B2B_ALERT_ADMIN_EMAIL", "a@example.com, b@example.com")
    assert flipkart.send_po_email("P1", "needs a map", "https://x/y.xls") is True
    mail = sent[0]
    assert mail["Source"] == "from@example.com" and mail["Destination"] == {"ToAddresses": ["a@example.com", "b@example.com"]}
    assert mail["Message"]["Subject"]["Data"] == "Notification from B2B Flipkart Channel - PO Number: P1"
    assert "needs a map" in mail["Message"]["Body"]["Html"]["Data"] and "Flipkart" in mail["Message"]["Body"]["Html"]["Data"]


def test_the_email_is_logged_and_sent_with_the_inbox_row(monkeypatch):
    seen = _world(monkeypatch, sku_map={}, sap_items={})
    logs = []
    monkeypatch.setenv("B2B_ALERT_FROM_EMAIL", "from@example.com")
    monkeypatch.setenv("B2B_ALERT_ADMIN_EMAIL", "a@example.com")
    monkeypatch.setattr(flipkart, "send_po_email", lambda po, message, url="": True)
    monkeypatch.setattr(flipkart, "_presigned_url", lambda key: "https://signed")
    monkeypatch.setattr(flipkart.store, "add_email_log", lambda conn, **kw: logs.append(kw))
    flipkart.process_email(RecordingConn(), FakeMailbox(_email_with(_xlsx_bytes(REAL_LAYOUT_ROWS))), "1", "C00770")
    assert seen["notifications"] and logs[0]["sent_ok"] is True and logs[0]["recipients"] == "a@example.com" and logs[0]["channel"] == "flipkart"
