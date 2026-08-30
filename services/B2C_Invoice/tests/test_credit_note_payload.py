"""Tests for Walmart credit-note payload gates and builder."""

from __future__ import annotations

from walmart.credit_note_payload import (
    CreditNoteBuildError,
    build_credit_note_payload,
    can_post_credit_note,
    line_has_discount,
    postable_return_lines,
    parse_return_order_lines,
)


def _raw(**overrides):
    base = {
        "returnOrderId": "RMA-1",
        "customerOrderId": "CUST-1",
        "returnOrderDate": "2026-08-10T12:00:00.000Z",
        "returnOrderLines": [
            {
                "returnOrderLineNumber": 1,
                "purchaseOrderId": "PO-1",
                "returnReason": "NO_LONGER_WANTED",
                "item": {"sku": "SKU-A", "productName": "Chew"},
                "quantity": {"measurementValue": 1.0},
                "refundedQty": 1.0,
                "statusTime": "2026-08-18T15:22:30.000Z",
                "currentRefundStatus": "REFUND_COMPLETED",
                "charges": [
                    {
                        "chargeCategory": "PRODUCT",
                        "chargeName": "ItemPrice",
                        "isDiscount": False,
                        "chargePerUnit": {"currencyAmount": 13.65, "currencyUnit": "USD"},
                    }
                ],
            }
        ],
    }
    base.update(overrides)
    return base


def _invoice():
    return {
        "DocEntry": 45821,
        "DocNum": 1001,
        "NumAtCard": "CUST-1",
        "CancelStatus": "csNo",
        "PayToCode": "Walmart-B2C",
        "DocumentLines": [
            {
                "LineNum": 0,
                "ItemCode": "FG0001",
                "Quantity": 2.0,
                "UnitPrice": 13.65,
                "WarehouseCode": "WM-FBA",
                "TaxCode": "etax0",
                "BatchNumbers": [{"BatchNumber": "BATCH-XXX", "Quantity": 2.0}],
            },
            {
                "LineNum": 1,
                "ItemCode": "EXP000003",
                "Quantity": 1.0,
                "UnitPrice": 3.60,
                "WarehouseCode": "WM-FBA",
                "TaxCode": "etax0",
                "AccountCode": "_SYS00000000059",
            },
        ],
    }


def _return_row(raw=None, **overrides):
    row = {
        "return_order_id": "RMA-1",
        "customer_order_id": "CUST-1",
        "return_order_date": "2026-08-10T12:00:00+00:00",
        "purchase_order_ids": "PO-1",
        "return_reasons": "NO_LONGER_WANTED",
        "current_refund_status": "REFUND_COMPLETED",
        "credit_note_status": "Not Posted",
        "raw_json": raw if raw is not None else _raw(),
    }
    row.update(overrides)
    return row


def test_gate_requires_refund_completed():
    ok, reason = can_post_credit_note(
        current_refund_status="REFUND_INITIATED",
        credit_note_status="Not Posted",
        raw_json=_raw(),
    )
    assert ok is False
    assert "REFUND_COMPLETED" in reason


def test_gate_blocks_posted_and_not_applicable():
    for status in ("Posted", "Posted (Manual)", "Not Applicable"):
        ok, _ = can_post_credit_note(
            current_refund_status="REFUND_COMPLETED",
            credit_note_status=status,
            raw_json=_raw(),
        )
        assert ok is False


def test_excludes_return_to_sender_only():
    raw = _raw(
        returnOrderLines=[
            {
                "returnReason": "RETURN_TO_SENDER",
                "item": {"sku": "SKU-A"},
                "refundedQty": 1,
                "quantity": {"measurementValue": 1},
            }
        ]
    )
    assert postable_return_lines(parse_return_order_lines(raw)) == []
    ok, reason = can_post_credit_note(
        current_refund_status="REFUND_COMPLETED",
        credit_note_status="Not Posted",
        raw_json=raw,
    )
    assert ok is False
    assert "no postable" in reason


def test_discount_is_hard_fail():
    line = {
        "charges": [{"chargeName": "PromoDiscount", "isDiscount": True}],
    }
    assert line_has_discount(line) is True


def test_build_payload_base_link_and_no_inventory_movement():
    payload = build_credit_note_payload(
        return_row=_return_row(),
        invoice=_invoice(),
        resolve_item_code=lambda sku: "FG0001",
    )
    assert payload["Series"] == 104
    assert payload["NumAtCard"] == "RMA-1"
    assert payload["PayToCode"] == "Walmart-B2C"
    assert payload["DocDate"] == "2026-08-18"  # statusTime Pacific (UTC-7/8)
    assert payload["DocDueDate"] == payload["DocDate"]
    assert payload["TaxDate"] == payload["DocDate"]
    product = payload["DocumentLines"][0]
    assert product["WithoutInventoryMovement"] == "tYES"
    assert product["BaseType"] == 13
    assert product["BaseEntry"] == 45821
    assert product["BaseLine"] == 0
    assert product["Quantity"] == 1.0
    assert product["UnitPrice"] == 13.65
    assert product["BatchNumbers"][0]["BatchNumber"] == "BATCH-XXX"
    assert product["BatchNumbers"][0]["Quantity"] == 1.0

    exp = payload["DocumentLines"][1]
    assert exp["ItemCode"] == "EXP000003"
    # half of invoice product qty returned → half of 3.60 tax
    assert exp["UnitPrice"] == 1.8
    assert exp["BaseEntry"] == 45821
    assert "WithoutInventoryMovement" not in exp


def test_missing_mapping_fails_whole_return():
    try:
        build_credit_note_payload(
            return_row=_return_row(),
            invoice=_invoice(),
            resolve_item_code=lambda sku: (_ for _ in ()).throw(ValueError("no map")),
        )
        assert False, "expected CreditNoteBuildError"
    except CreditNoteBuildError as e:
        assert "mapping" in str(e).lower() or "SKU" in str(e)


def test_missing_invoice_line_fails_whole_return():
    try:
        build_credit_note_payload(
            return_row=_return_row(),
            invoice=_invoice(),
            resolve_item_code=lambda sku: "OTHER-ITEM",
        )
        assert False, "expected CreditNoteBuildError"
    except CreditNoteBuildError as e:
        assert "not found on AR Invoice" in str(e)
