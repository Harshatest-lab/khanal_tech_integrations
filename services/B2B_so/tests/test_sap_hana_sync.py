from sap_hana_sync import document_status


def test_document_status_open():
    assert document_status("O", "N") == "bost_Open"


def test_document_status_closed():
    assert document_status("C", "N") == "bost_Close"


def test_document_status_cancelled():
    assert document_status("O", "Y") == "cancelled"
