"""Tests for Walmart returns dashboard helpers."""

from walmart.dashboard_api import _return_lines_from_raw, _returns_filter_clauses


def test_return_lines_from_raw_maps_walmart_fields() -> None:
    raw = {
        "returnOrderId": "RET-1",
        "returnOrderLines": [
            {
                "returnOrderLineNumber": 1,
                "purchaseOrderId": "PO-1",
                "returnReason": "NO_LONGER_WANTED",
                "item": {"sku": "SKU-A", "productName": "Chew"},
                "quantity": {"unitOfMeasure": "EA", "measurementValue": 1.0},
                "unitPrice": {"currencyAmount": 21.66, "currencyUnit": "USD"},
                "status": "INITIATED",
                "statusTime": "2026-08-18T15:22:30.000Z",
                "currentDeliveryStatus": "NOT_SHIPPED",
                "currentRefundStatus": "NOT_REFUNDED",
            }
        ],
    }

    lines = _return_lines_from_raw(raw)
    assert len(lines) == 1
    assert lines[0]["purchase_order_id"] == "PO-1"
    assert lines[0]["sku"] == "SKU-A"
    assert lines[0]["quantity"] == 1.0
    assert lines[0]["unit_price"] == 21.66
    assert lines[0]["current_refund_status"] == "NOT_REFUNDED"


def test_returns_filter_clauses_builds_expected_predicates() -> None:
    clauses, params = _returns_filter_clauses(
        return_order_id="RET",
        purchase_order_id="1291",
        status="INITIATED",
        credit_note_status="Not Posted",
        return_date_from="2026-08-01",
        return_date_to="2026-08-20",
    )
    assert any("return_order_id ILIKE" in c for c in clauses)
    assert any("purchase_order_ids ILIKE" in c for c in clauses)
    assert any("r.status = %s" in c for c in clauses)
    assert any("credit_note_status = %s" in c for c in clauses)
    assert "RET" in params[0]
    assert "1291" in params[1]
    assert "INITIATED" in params
    assert "Not Posted" in params
