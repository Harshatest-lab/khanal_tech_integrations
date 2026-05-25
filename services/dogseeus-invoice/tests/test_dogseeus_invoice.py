import json

from dogseeus_invoice.handler import invoice_handler


def test_invoice() -> None:
    resp = invoice_handler({"body": '{"order_id": "o1"}'}, object())
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body.get("service") == "Dogseeus_Invoice"
