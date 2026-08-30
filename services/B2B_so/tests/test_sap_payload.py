from blinkit import BLINKIT_CARD_CODE
from blinkit_sap import build_order_payload


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


def test_post_persists_ssm_error(monkeypatch):
    import blinkit_sap as sap

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
    import blinkit_sap as sap

    assert sap.is_open_document_status("bost_Open")
    assert sap.is_open_document_status("Open")
    assert not sap.is_open_document_status("bost_Close")
    assert not sap.is_open_document_status("cancelled")


def test_update_posted_order_patches_open_doc(monkeypatch):
    import blinkit_sap as sap

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
    import blinkit_sap as sap

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
