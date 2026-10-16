import pytest

import blinkit
from blinkit import BLINKIT_CARD_CODE
from b2b_sappost import _sap_post_precheck, build_order_payload, order_series, po_link_error


@pytest.fixture(autouse=True)
def imap_config(monkeypatch):
    """Never call SSM in these tests; /b2b/imap only sets a series for Flipkart."""
    monkeypatch.setattr(blinkit, "_imap_config_cache", {"series": {"flipkart": 336}})


def test_card_code_is_hardcoded():
    assert BLINKIT_CARD_CODE == "C03570"



def test_order_payload_matches_frappe_shape():
    header = {
        "customer_code": "C03570",
        "company_name": "HANDS ON TRADES PRIVATE LIMITED",
        "po_number": "2173110010781",
        "po_expiry_date": "2026-03-06",
        "billing_from": "KT",
        "bill_to": "Local",
        "salesperson_code": "103",
        "matched_address_name": "BLR-WF",
        "pdf_s3_key": "blinkit/Blinkit_2173110010781.pdf",
    }
    lines = [{"sap_code": "FG-001", "quantity": 10, "basic_cost_price": 12.5, "taxcode": "KACS5"}]
    payload = build_order_payload(header, lines, "103")
    assert payload["SalesPersonCode"] == "103"
    assert payload["CardCode"] == "C03570"
    assert payload["Series"] == 514
    assert payload["NumAtCard"] == "2173110010781"
    assert "DocDate" not in payload
    assert payload["DocumentLines"][0]["ItemCode"] == "FG-001"
    assert payload["DocumentLines"][0]["TaxCode"] == "KACS5"


def test_po_link_is_public_and_not_presigned(monkeypatch):
    monkeypatch.setenv("B2B_DOCS_BUCKET", "khanal-b2b-so-dev")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    header = {
        "customer_code": "C03570",
        "company_name": "HANDS ON TRADES PRIVATE LIMITED",
        "po_number": "8525210021628",
        "po_expiry_date": "2026-03-06",
        "billing_from": "KT",
        "bill_to": "Local",
        "matched_address_name": "BLR-WF",
        "pdf_s3_key": "blinkit/Blinkit_8525210021628.pdf",
        "po_url": "https://khanal-b2b-so-dev.s3.amazonaws.com/blinkit/Blinkit_8525210021628.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc",
    }
    lines = [{"sap_code": "FG-001", "quantity": 1, "basic_cost_price": 1, "taxcode": "KACS5"}]
    payload = build_order_payload(header, lines, "103")
    assert payload["U_PO_LINK"] == "https://khanal-b2b-so-dev.s3.ap-south-1.amazonaws.com/blinkit/Blinkit_8525210021628.pdf"
    assert "X-Amz-" not in payload["U_PO_LINK"]


def test_order_payload_fills_tax_code_from_tax_rate_when_missing(monkeypatch):
    import blinkit

    monkeypatch.setattr(
        blinkit,
        "enrich_lines_from_item_master",
        lambda lines, matched_state="": lines,
    )
    header = {
        "customer_code": "C03566",
        "company_name": "SCOOTSY LOGISTICS PRIVATE LIMITED",
        "po_number": "NARPO22475",
        "po_expiry_date": "2026-08-22",
        "billing_from": "KT",
        "bill_to": "Central",
        "matched_address_name": "Scootsy Nagpur warehouse",
        "matched_state": "MH",
        "pdf_s3_key": "instamart/x.pdf",
    }
    lines = [{"sap_code": "FGHN0065", "quantity": 24, "basic_cost_price": 469.16, "tax_rate": 5}]
    payload = build_order_payload(header, lines, "111")
    assert payload["DocumentLines"][0]["TaxCode"] == "KAIG5"


def test_order_payload_fills_tax_code_via_item_master(monkeypatch):
    import blinkit

    def fake_enrich(lines, matched_state=""):
        return [{**lines[0], "taxcode": "KAIG5", "tax_rate": 5}]

    monkeypatch.setattr(blinkit, "enrich_lines_from_item_master", fake_enrich)
    header = {
        "customer_code": "C03570",
        "company_name": "TEST",
        "po_number": "PO1",
        "po_expiry_date": "2026-08-22",
        "billing_from": "KT",
        "bill_to": "Central",
        "matched_state": "MH",
        "matched_address_name": "WH",
        "pdf_s3_key": "x.pdf",
    }
    lines = [{"sap_code": "FGHN0065", "quantity": 1, "basic_cost_price": 10}]
    payload = build_order_payload(header, lines, "103")
    assert payload["DocumentLines"][0]["TaxCode"] == "KAIG5"


def test_order_payload_accepts_db_column_names():
    header = {
        "customer_code": "C03570",
        "company_name": "HANDS ON TRADES PRIVATE LIMITED",
        "po_number": "2173110010781",
        "po_expiry_date": "2026-03-06",
        "billing_from": "KT",
        "bill_to": "Central",
        "matched_address_name": "BLR-WF",
        "pdf_s3_key": "blinkit/Blinkit_2173110010781.pdf",
    }
    lines = [
        {"sap_code": "FGHN0054", "quantity": 77, "basic_cost_price": 64.3, "taxcode": "KAIG5"},
        {"sap_code": "", "quantity": 1, "basic_cost_price": 1, "taxcode": "KAIG5"},
    ]
    payload = build_order_payload(header, lines, "103")
    assert payload["CardCode"] == "C03570"
    assert payload["CardName"] == "HANDS ON TRADES PRIVATE LIMITED"
    assert payload["DocDueDate"] == "2026-03-06"
    assert "DocDate" not in payload
    assert payload["U_BillingFrom"] == "KT"
    assert payload["ShipToCode"] == "BLR-WF"
    assert len(payload["DocumentLines"]) == 1
    assert payload["DocumentLines"][0]["ItemCode"] == "FGHN0054"
    assert payload["DocumentLines"][0]["Price"] == 64.3
    assert payload["DocumentLines"][0]["TaxCode"] == "KAIG5"


def _document_line_for_channel(channel: str) -> dict:
    header = {
        "channel": channel,
        "customer_code": "C03570",
        "company_name": "TEST",
        "po_number": "PO-PRICE",
        "po_expiry_date": "2026-03-06",
        "billing_from": "KT",
        "bill_to": "Local",
        "matched_address_name": "WH",
        "pdf_s3_key": "x.pdf",
    }
    lines = [{"sap_code": "FG-001", "quantity": 2, "basic_cost_price": 50.0, "taxcode": "KACS5"}]
    return build_order_payload(header, lines, "103")["DocumentLines"][0]


def test_blinkit_sends_price_and_unit_price():
    line = _document_line_for_channel("blinkit")
    assert line["Price"] == 50.0
    assert line["UnitPrice"] == 50.0


def test_bigbasket_sends_price_and_unit_price():
    line = _document_line_for_channel("bigbasket")
    assert line["Price"] == 50.0
    assert line["UnitPrice"] == 50.0


def test_bigbasket_card_name_is_legal_entity():
    from bigbasket import BIGBASKET_CARD_NAME

    header = {
        "channel": "bigbasket",
        "customer_code": "C03569",
        "company_name": "Bigbasket",  # legacy stored value
        "po_number": "IRA44122563",
        "po_expiry_date": "2026-09-15",
        "billing_from": "KT",
        "bill_to": "Local",
        "matched_address_name": "IRCPL - Bangalore - Dasanapura",
        "pdf_s3_key": "x.pdf",
    }
    lines = [{"sap_code": "FGDC0063", "quantity": 12, "basic_cost_price": 126.62, "taxcode": "KACS18"}]
    payload = build_order_payload(header, lines, "111", skip_enrich=True)
    assert payload["CardName"] == BIGBASKET_CARD_NAME
    assert payload["CardName"] == "Innovative Retail Concepts Private Limited"
    assert payload["CardCode"] == "C03569"


def test_bigbasket_u_po_link_from_excel_when_no_pdf(monkeypatch):
    monkeypatch.setenv("B2B_DOCS_BUCKET", "khanal-b2b-docs")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    header = {
        "channel": "bigbasket",
        "customer_code": "C03569",
        "company_name": "Innovative Retail Concepts Private Limited",
        "po_number": "IRA44122563",
        "po_expiry_date": "2026-09-15",
        "billing_from": "KT",
        "bill_to": "Local",
        "matched_address_name": "IRCPL - Bangalore - Dasanapura",
        "pdf_s3_key": "",
        "xlsx_s3_key": "bigbasket/Bigbasket_IRA44122563.xls",
    }
    lines = [{"sap_code": "FGDC0063", "quantity": 12, "basic_cost_price": 126.62, "taxcode": "KACS18"}]
    payload = build_order_payload(header, lines, "111", skip_enrich=True)
    assert payload["U_PO_LINK"] == (
        "https://khanal-b2b-docs.s3.ap-south-1.amazonaws.com/bigbasket/Bigbasket_IRA44122563.xls"
    )
    assert _sap_post_precheck(header, payload) == ""


def test_bigbasket_blocks_post_when_no_document_link():
    header = {
        "channel": "bigbasket",
        "customer_code": "C03569",
        "po_number": "IRA44122563",
        "po_expiry_date": "2026-09-15",
        "matched_address_name": "WH",
        "pdf_s3_key": "",
        "xlsx_s3_key": "",
    }
    lines = [{"sap_code": "FGDC0063", "quantity": 12, "basic_cost_price": 126.62, "taxcode": "KACS18"}]
    payload = build_order_payload(header, lines, "111", skip_enrich=True)
    assert payload["U_PO_LINK"] == ""
    err = _sap_post_precheck(header, payload)
    assert "U_PO_LINK" in err
    assert "Excel file" in err


def test_zepto_sends_unit_price_only():
    line = _document_line_for_channel("zepto")
    assert "Price" not in line
    assert line["UnitPrice"] == 50.0


def test_instamart_omits_price_fields():
    line = _document_line_for_channel("instamart")
    assert "Price" not in line
    assert "UnitPrice" not in line


def test_instamart_prices_enabled_via_env(monkeypatch):
    monkeypatch.setenv("B2B_INSTAMART_SEND_PRICES", "1")
    line = _document_line_for_channel("instamart")
    assert line["Price"] == 50.0
    assert line["UnitPrice"] == 50.0


def test_instamart_payload_tax_from_oitm(monkeypatch):
    import blinkit

    header = {
        "channel": "instamart",
        "customer_code": "C03566",
        "company_name": "SCOOTSY LOGISTICS PRIVATE LIMITED",
        "po_number": "NARPO22475",
        "po_expiry_date": "2026-08-22",
        "billing_from": "KT",
        "bill_to": "Central",
        "matched_address_name": "Scootsy Nagpur warehouse",
        "matched_state": "MH",
        "pdf_s3_key": "instamart/Instamart_NARPO22475.pdf",
    }
    lines = [
        {"sap_code": "FGHN0065", "quantity": 24, "basic_cost_price": 469.16},
        {"sap_code": "FGHN0008", "quantity": 72, "basic_cost_price": 100},
    ]
    items = {
        "FGHN0065": {"ItemCode": "FGHN0065", "U_TaxRate": 5},
        "FGHN0008": {"ItemCode": "FGHN0008", "U_TaxRate": 5},
    }
    monkeypatch.setattr(blinkit, "get_items", lambda codes: {c: items[c] for c in codes if c in items})
    payload = build_order_payload(header, lines, "111")
    assert payload["DocumentLines"][0]["TaxCode"] == "KAIG5"
    assert payload["DocumentLines"][1]["TaxCode"] == "KAIG5"
    assert "Price" not in payload["DocumentLines"][0]


def test_post_blocks_when_tax_code_missing(monkeypatch):
    import b2b_sappost as sap

    header = {
        "channel": "instamart",
        "po_number": "NARPO22475",
        "pipeline_status": "approved",
        "customer_code": "C03566",
        "matched_state": "MH",
        "bill_to": "Central",
        "pdf_s3_key": "instamart/x.pdf",
        "matched_address_name": "Scootsy Nagpur warehouse",
        "po_expiry_date": "2026-08-22",
        "billing_from": "KT",
    }
    lines = [{"sap_code": "FGHN0065", "quantity": 24, "basic_cost_price": 469.16}]
    monkeypatch.setattr(sap.store, "get_po", lambda *a, **k: {"header": header, "lines": lines})
    saved = {}

    def set_status(conn, po, status, **kw):
        saved.update(kw)
        saved["pipeline_status"] = status

    monkeypatch.setattr(sap.store, "set_po_status", set_status)
    monkeypatch.setattr(sap.store, "add_audit", lambda *a, **k: None)

    class Conn:
        def commit(self):
            pass

    out = sap.post_purchase_order(Conn(), "NARPO22475", channel="instamart", salesperson_code="111")
    assert out["status"] == "error"
    assert "FGHN0065" in out["message"]
    assert "Refetch" in out["message"]
    assert saved.get("sap_status") == "POST FAILED"


def test_post_purchase_order_uses_channel_for_lookup(monkeypatch):
    import b2b_sappost as sap

    lookup = []

    def fake_get_po(conn, po_number, channel=sap.store.CHANNEL_BLINKIT):
        lookup.append((po_number, channel))
        if channel == "instamart":
            return {
                "header": {
                    "channel": "instamart",
                    "po_number": po_number,
                    "pipeline_status": "approved",
                    "po_expiry_date": "2026-03-06",
                    "matched_address_name": "BLR-WF",
                },
                "lines": [{"sap_code": "FG-001", "quantity": 1, "basic_cost_price": 1, "taxcode": "KAIG5"}],
            }
        return None

    monkeypatch.setattr(sap.store, "get_po", fake_get_po)
    monkeypatch.setattr(sap.store, "set_po_status", lambda *a, **k: None)
    monkeypatch.setattr(sap.store, "add_audit", lambda *a, **k: None)
    monkeypatch.setattr(
        sap,
        "find_order_by_num_at_card",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("skip sap")),
    )

    class Conn:
        def commit(self):
            pass

    out = sap.post_purchase_order(Conn(), "NARPO1", channel="instamart", salesperson_code="103")
    assert lookup == [("NARPO1", "instamart")]
    assert out["status"] == "error"
    assert "skip sap" in out["message"]


def test_post_persists_ssm_error(monkeypatch):
    import b2b_sappost as sap

    header = {
        "po_number": "8525210021628",
        "pipeline_status": "approved",
        "company_name": "HANDS ON TRADES PRIVATE LIMITED",
        "customer_code": "C03570",
        "pdf_s3_key": "blinkit/Blinkit_8525210021628.pdf",
        "matched_address_name": "BLR-WF",
        "po_expiry_date": "2026-03-06",
        "billing_from": "KT",
        "bill_to": "Local",
    }
    lines = [{"sap_code": "FG-001", "quantity": 1, "basic_cost_price": 1, "taxcode": "KACS5"}]
    monkeypatch.setattr(sap.store, "get_po", lambda *a, **k: {"header": header, "lines": lines})
    saved = {}

    def set_status(conn, po, status, **kw):
        saved.update(kw)
        saved["pipeline_status"] = status
        saved["po"] = po

    monkeypatch.setattr(sap.store, "set_po_status", set_status)
    monkeypatch.setattr(sap.store, "add_audit", lambda *a, **k: None)
    monkeypatch.setattr(
        sap,
        "find_order_by_num_at_card",
        lambda *_a, **_k: (_ for _ in ()).throw(
            ValueError("SSM sl_config and credentials must include sap_b1_url, companydb, username, password")
        ),
    )

    class Conn:
        def commit(self):
            pass

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def execute(self, *_a, **_k):
            pass

    out = sap.post_purchase_order(Conn(), "8525210021628", actor="a@x.com", salesperson_code="103")
    assert out["status"] == "error"
    assert "SSM" in out["message"]
    assert saved["pipeline_status"] == "approved"
    assert saved["sap_status"] == "POST FAILED"
    assert "SSM" in saved["last_error"]


def test_is_open_document_status():
    import b2b_sappost as sap

    assert sap.is_open_document_status("bost_Open")
    assert sap.is_open_document_status("Open")
    assert not sap.is_open_document_status("bost_Close")
    assert not sap.is_open_document_status("cancelled")


def test_update_posted_order_patches_open_doc(monkeypatch):
    import b2b_sappost as sap

    header = {
        "po_number": "51925610000607",
        "pipeline_status": "posted",
        "sap_docentry": 44753,
        "sap_docnum": 31065,
        "sap_document_status": "bost_Open",
        "company_name": "BLINK",
        "customer_code": "C03570",
        "po_expiry_date": "2026-03-06",
        "billing_from": "KT",
        "bill_to": "Local",
        "matched_address_name": "BLR-WF",
        "pdf_s3_key": "blinkit/x.pdf",
    }
    lines = [{"sap_code": "FG-001", "quantity": 3, "basic_cost_price": 100, "taxcode": "KACS5"}]
    monkeypatch.setattr(sap.store, "get_po", lambda *a, **k: {"header": header, "lines": lines})
    monkeypatch.setattr(sap, "get_order", lambda doc: {"DocEntry": doc, "DocNum": 31065, "DocumentStatus": "bost_Open", "DocTotal": 223286})
    patched = {}

    def fake_update(doc_entry, payload):
        patched["doc_entry"] = doc_entry
        patched["payload"] = payload

    monkeypatch.setattr(sap, "update_order", fake_update)
    monkeypatch.setattr(sap.store, "set_po_status", lambda *a, **k: None)
    monkeypatch.setattr(sap.store, "add_audit", lambda *a, **k: None)

    class Conn:
        def commit(self):
            pass

    out = sap.update_posted_order(Conn(), "51925610000607", actor="a@x.com", salesperson_code="103")
    assert out["status"] == "success"
    assert patched["doc_entry"] == 44753
    assert "DocumentLines" in patched["payload"]


def test_cancel_posted_order_calls_service_layer(monkeypatch):
    import b2b_sappost as sap

    header = {
        "po_number": "51925610000607",
        "pipeline_status": "posted",
        "sap_docentry": 44753,
        "sap_document_status": "bost_Open",
    }
    monkeypatch.setattr(sap.store, "get_po", lambda *a, **k: {"header": header, "lines": []})
    monkeypatch.setattr(sap, "get_order", lambda doc: {"DocEntry": doc, "DocumentStatus": "bost_Open", "Cancelled": "tNO"})
    cancelled = {}

    def fake_cancel(doc_entry):
        cancelled["doc_entry"] = doc_entry
        return {"ok": True}

    monkeypatch.setattr(sap, "cancel_order", fake_cancel)
    saved = {}

    def set_status(conn, po, status, **kw):
        saved["status"] = status
        saved.update(kw)

    monkeypatch.setattr(sap.store, "set_po_status", set_status)
    monkeypatch.setattr(sap.store, "add_audit", lambda *a, **k: None)

    class Conn:
        def commit(self):
            pass

    out = sap.cancel_posted_order(Conn(), "51925610000607", actor="a@x.com")
    assert out["status"] == "success"
    assert cancelled["doc_entry"] == 44753
    assert saved["status"] == "cancelled"



def test_audit_rows_are_written_under_the_pos_own_channel(monkeypatch):
    """Regression: sap_create / sap_update / sap_link used to be logged as 'blinkit' for every channel."""
    import b2b_sappost

    class FakeCursor:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, *args): pass

    class FakeConn:
        def cursor(self): return FakeCursor()
        def commit(self): pass

    audits = []
    monkeypatch.setattr(b2b_sappost.store, "set_po_status", lambda *a, **k: None)
    monkeypatch.setattr(b2b_sappost.store, "mark_notifications_read", lambda *a, **k: None)
    monkeypatch.setattr(b2b_sappost.store, "add_audit", lambda conn, **k: audits.append(k))
    b2b_sappost._finish_post(
        FakeConn(), "PO1", actor="a", action="sap_create", salesperson_code="1", sap_docentry=5, sap_docnum=6,
        sap_document_status="open", sap_response={}, message="ok", channel="flipkart",
    )
    assert audits[0]["channel"] == "flipkart"


def test_po_link_falls_back_to_the_excel_when_there_is_no_pdf(monkeypatch):
    import b2b_sappost

    monkeypatch.setattr(blinkit, "public_url", lambda key: f"https://bucket/{key}")
    assert b2b_sappost._po_file_url({"pdf_s3_key": "a.pdf", "xlsx_s3_key": "a.xls"}) == "https://bucket/a.pdf"
    assert b2b_sappost._po_file_url({"pdf_s3_key": "", "xlsx_s3_key": "flipkart/Flipkart_P1.xls"}) == "https://bucket/flipkart/Flipkart_P1.xls"


def test_series_comes_from_ssm_per_channel_and_defaults_to_514():
    assert order_series("flipkart") == 336  # SSM value wins
    assert order_series("blinkit") == 514  # not in SSM: hardcoded default
    assert order_series("zepto") == 514
