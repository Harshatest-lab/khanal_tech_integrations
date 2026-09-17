from sap_hana_sync import document_status


def test_document_status_open():
    assert document_status("O", "N") == "bost_Open"


def test_document_status_closed():
    assert document_status("C", "N") == "bost_Close"


def test_document_status_cancelled():
    assert document_status("O", "Y") == "cancelled"


def test_document_status_accepts_service_layer_tokens():
    assert document_status("bost_Close", "N") == "bost_Close"
    assert document_status("bost_Open", "N") == "bost_Open"
