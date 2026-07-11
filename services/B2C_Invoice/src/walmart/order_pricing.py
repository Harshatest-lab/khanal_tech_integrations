"""Parse Walmart order line charges (product, tax, shipping)."""

from __future__ import annotations

from typing import Any


def as_list(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


def parse_line_charges(order_line: dict[str, Any]) -> dict[str, Any]:
    """Extract unit price, product tax, shipping, and shipping tax from a line."""
    charges = as_list(order_line.get("charges", {}).get("charge"))
    result: dict[str, Any] = {
        "unit_price": None,
        "product_tax": None,
        "shipping": None,
        "shipping_tax": None,
        "currency": "USD",
    }

    for charge in charges:
        charge_type = str(charge.get("chargeType") or "").upper()
        charge_name = str(charge.get("chargeName") or "")
        amount = (charge.get("chargeAmount") or {}).get("amount")
        tax = (charge.get("tax") or {}).get("taxAmount", {}).get("amount")
        if tax is None:
            tax = (charge.get("taxAndOtherFees") or {}).get("taxAndOtherFeesAmount", {}).get(
                "amount"
            )

        if charge_type == "PRODUCT" or charge_name == "ItemPrice":
            result["unit_price"] = _to_float(amount)
            result["product_tax"] = _to_float(tax)
            result["currency"] = (charge.get("chargeAmount") or {}).get("currency") or "USD"
        elif charge_type == "SHIPPING" or charge_name == "Shipping":
            result["shipping"] = _to_float(amount)
            result["shipping_tax"] = _to_float(tax)

    return result


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def line_quantity(order_line: dict[str, Any]) -> float:
    raw = (order_line.get("orderLineQuantity") or {}).get("amount", "1")
    try:
        qty = float(raw)
    except (TypeError, ValueError):
        qty = 1.0
    return qty if qty > 0 else 1.0
