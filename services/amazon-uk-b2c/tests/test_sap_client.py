from amazon_uk import sap_client


def test_extract_sap_error_nests_value():
    body = {"error": {"message": {"value": "Cannot add row without complete selection"}}}
    assert sap_client.extract_sap_error(body) == "Cannot add row without complete selection"


def test_extract_sap_error_plain_string():
    assert sap_client.extract_sap_error({"error": {"message": "plain"}}) == "plain"


def test_extract_sap_error_unknown():
    assert sap_client.extract_sap_error({}) == "Unknown SAP error"


def test_find_invoice_path_escapes_and_filters_cancel_status(monkeypatch):
    captured = {}

    def fake_request(method, path, body=None, timeout=60):
        captured["path"] = path
        return 200, {"value": []}

    monkeypatch.setattr(sap_client, "_request", fake_request)
    assert sap_client.find_invoice_by_order_id("402-123'4567") is None
    # quote() encodes spaces as %20 (same as the sibling service's prod code);
    # the escaped quote inside NumAtCard must survive.
    assert "NumAtCard%20eq%20'402-123''4567'" in captured["path"]
    assert "CancelStatus%20eq%20'csNo'" in captured["path"]


def test_find_invoice_returns_doc_fields(monkeypatch):
    monkeypatch.setattr(
        sap_client, "_request",
        lambda *a, **k: (200, {"value": [{"DocEntry": 123, "DocNum": 456}]}),
    )
    assert sap_client.find_invoice_by_order_id("402-1") == {"doc_entry": "123", "doc_num": "456"}


def test_find_invoice_loud_failure(monkeypatch):
    monkeypatch.setattr(sap_client, "_request", lambda *a, **k: (500, {}))
    try:
        sap_client.find_invoice_by_order_id("402-1")
    except sap_client.SapLookupError:
        pass
    else:
        raise AssertionError("lookup failure must be loud, never 'not found'")


def test_post_invoice_udf_patch_flow(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, timeout=60):
        calls.append((method, path, body))
        if method == "POST":
            return 201, {"DocEntry": 7, "DocNum": 9001}
        if method == "PATCH":
            return 204, {}
        if method == "GET" and path.startswith("Invoices(7)"):
            return 200, {"DocEntry": 7, "DocNum": 9001, "U_TaxClctnMthd": "1"}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(sap_client, "_request", fake_request)
    result = sap_client.post_invoice({"CardCode": "C0003", "U_TaxClctnMthd": "1"})

    create = calls[0]
    assert create[0] == "POST" and "U_TaxClctnMthd" not in (create[2] or {})
    assert calls[1][0] == "PATCH" and calls[1][2] == {"U_TaxClctnMthd": "1"}
    assert result["doc_entry"] == "7" and result["doc_num"] == "9001"
    assert result["response"]["U_TaxClctnMthd"] == "1"


def test_post_invoice_error_carries_response_body(monkeypatch):
    monkeypatch.setattr(
        sap_client, "_request",
        lambda *a, **k: (400, {"error": {"message": {"value": "bad"}}}),
    )
    try:
        sap_client.post_invoice({"CardCode": "C0003"})
    except sap_client.SapPostError as e:
        assert e.response_body == {"error": {"message": {"value": "bad"}}}
    else:
        raise AssertionError("expected SapPostError")


def test_find_credit_notes_path_escapes_and_filters_cancel_status(monkeypatch):
    captured = {}

    def fake_request(method, path, body=None, timeout=60):
        captured["path"] = path
        return 200, {"value": []}

    monkeypatch.setattr(sap_client, "_request", fake_request)
    assert sap_client.find_credit_notes_by_order("402-123'4567") == []
    assert "NumAtCard%20eq%20'402-123''4567'" in captured["path"]
    assert "CancelStatus%20eq%20'csNo'" in captured["path"]


def test_find_credit_notes_returns_every_candidate(monkeypatch):
    monkeypatch.setattr(
        sap_client, "_request",
        lambda *a, **k: (200, {"value": [{"DocEntry": 1, "DocNum": 10}, {"DocEntry": 2, "DocNum": 20}]}),
    )
    assert sap_client.find_credit_notes_by_order("402-1") == [
        {"doc_entry": "1", "doc_num": "10"}, {"doc_entry": "2", "doc_num": "20"},
    ]


def test_find_credit_notes_loud_failure(monkeypatch):
    monkeypatch.setattr(sap_client, "_request", lambda *a, **k: (500, {}))
    try:
        sap_client.find_credit_notes_by_order("402-1")
    except sap_client.SapLookupError:
        pass
    else:
        raise AssertionError("lookup failure must be loud, never 'not found'")


def test_post_credit_note_udf_patch_flow(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, timeout=60):
        calls.append((method, path, body))
        if method == "POST":
            return 201, {"DocEntry": 7, "DocNum": 9001}
        if method == "PATCH":
            return 204, {}
        if method == "GET" and path.startswith("CreditNotes(7)"):
            return 200, {"DocEntry": 7, "DocNum": 9001, "U_CreatedBy": "Amazon API"}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(sap_client, "_request", fake_request)
    result = sap_client.post_credit_note({"CardCode": "C0003", "U_CreatedBy": "Amazon API"})

    create = calls[0]
    assert create[0] == "POST" and "U_CreatedBy" not in (create[2] or {})
    assert calls[1][0] == "PATCH" and calls[1][2] == {"U_CreatedBy": "Amazon API"}
    assert result["doc_entry"] == "7" and result["doc_num"] == "9001"


def test_post_credit_note_error_carries_response_body(monkeypatch):
    monkeypatch.setattr(
        sap_client, "_request",
        lambda *a, **k: (400, {"error": {"message": {"value": "bad"}}}),
    )
    try:
        sap_client.post_credit_note({"CardCode": "C0003"})
    except sap_client.SapPostError as e:
        assert e.response_body == {"error": {"message": {"value": "bad"}}}
    else:
        raise AssertionError("expected SapPostError")
