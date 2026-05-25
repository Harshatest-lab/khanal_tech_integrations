import json

from src.handler import carrier_webhook_handler, create_shipment_handler


def test_create_shipment() -> None:
    resp = create_shipment_handler({"body": "{}"}, object())
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert "label_url" in body


def test_carrier_webhook() -> None:
    resp = carrier_webhook_handler({"pathParameters": {"carrier": "ups"}}, object())
    assert resp["statusCode"] == 200
