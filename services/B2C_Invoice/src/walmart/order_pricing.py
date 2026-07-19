"""Parse Walmart order line charges (product, tax, shipping)."""

from __future__ import annotations

from typing import Any

SAP_INVOICEABLE_STATUSES = ("Shipped", "Delivered", "Dispensed")


def as_list(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def line_status_quantities(order_line: dict[str, Any]) -> dict[str, Any]:
    """Return total and current status-bucket quantities for an order line."""
    total = line_quantity(order_line)
    by_status: dict[str, float] = {}
    statuses = as_list(
        (order_line.get("orderLineStatuses") or {}).get("orderLineStatus")
    )
    for row in statuses:
        status = str((row or {}).get("status") or "Unknown").strip() or "Unknown"
        raw = ((row or {}).get("statusQuantity") or {}).get("amount")
        qty = _to_float(raw)
        if qty is None or qty < 0:
            continue
        by_status[status] = by_status.get(status, 0.0) + qty

    invoiceable = (
        sum(by_status.get(status, 0.0) for status in SAP_INVOICEABLE_STATUSES)
        if statuses
        else total
    )
    cancelled = by_status.get("Cancelled", 0.0) + by_status.get("Canceled", 0.0)
    return {
        "total": total,
        "invoiceable": min(invoiceable, total),
        "delivered": by_status.get("Delivered", 0.0),
        "cancelled": min(cancelled, total),
        "by_status": by_status,
        "is_partial": invoiceable > 0 and cancelled > 0,
    }


def line_invoiceable_quantity(order_line: dict[str, Any]) -> float:
    """Quantity eligible for SAP, based on statusQuantity rather than array order."""
    return float(line_status_quantities(order_line)["invoiceable"])


def line_display_status(order_line: dict[str, Any]) -> str:
    """Choose the meaningful current status independent of Walmart array order."""
    quantities = line_status_quantities(order_line)
    by_status = quantities["by_status"]
    for status in ("Delivered", "Dispensed", "Shipped", "Cancelled", "Canceled"):
        if by_status.get(status, 0) > 0:
            return status
    statuses = as_list(
        (order_line.get("orderLineStatuses") or {}).get("orderLineStatus")
    )
    return str((statuses[-1] if statuses else {}).get("status") or "Unknown")


def _charge_tax(charge: dict[str, Any]) -> tuple[str | None, float | None]:
    tax_block = charge.get("tax") or {}
    tax_name = tax_block.get("taxName")
    tax_amount = _to_float((tax_block.get("taxAmount") or {}).get("amount"))

    fees = charge.get("taxAndOtherFees") or {}
    if tax_amount is None:
        tax_name = tax_name or fees.get("taxAndOtherFeesName")
        tax_amount = _to_float((fees.get("taxAndOtherFeesAmount") or {}).get("amount"))

    clean_name = str(tax_name).strip() if tax_name else None
    return clean_name, tax_amount


def _promotion_discount_amount(charge: dict[str, Any]) -> float | None:
    """Promotion / discount from a charge block (negative PRODUCT amount)."""
    charge_name = str(charge.get("chargeName") or "").strip()
    charge_type = str(charge.get("chargeType") or "").upper()
    amount = _to_float((charge.get("chargeAmount") or {}).get("amount"))
    if amount is None or amount >= 0:
        return None
    if charge_name == "Promotion" or charge_type == "PRODUCT":
        return round(abs(float(amount)), 2)
    return None


def line_promotion_discount(order_line: dict[str, Any]) -> float:
    """Total promotion discount on a Walmart order line (positive USD amount)."""
    total = 0.0

    refund = order_line.get("refund") or {}
    for refund_charge in as_list((refund.get("refundCharges") or {}).get("refundCharge")):
        charge = refund_charge.get("charge") or {}
        amount = _promotion_discount_amount(charge)
        if amount is not None:
            total += amount

    for charge in as_list(order_line.get("charges", {}).get("charge")):
        amount = _promotion_discount_amount(charge)
        if amount is not None:
            total += amount

    return round(total, 2) if total > 0 else 0.0


def _append_tax_line(
    tax_lines: list[dict[str, Any]],
    *,
    name: str,
    amount: float | None,
    category: str,
) -> None:
    if amount is None or amount <= 0:
        return
    tax_lines.append(
        {
            "name": name,
            "amount": round(float(amount), 2),
            "category": category,
        }
    )


def parse_line_charges(order_line: dict[str, Any]) -> dict[str, Any]:
    """Extract item price, all tax lines, shipping, and totals from a Walmart line."""
    charges = as_list(order_line.get("charges", {}).get("charge"))
    tax_lines: list[dict[str, Any]] = []
    discount = line_promotion_discount(order_line)
    result: dict[str, Any] = {
        "item_price": None,
        "discount": discount,
        "net_item_price": None,
        "product_tax": None,
        "shipping": None,
        "shipping_tax": None,
        "tax_lines": tax_lines,
        "total_tax": 0.0,
        "currency": "USD",
    }

    for charge in charges:
        charge_type = str(charge.get("chargeType") or "").upper()
        charge_name = str(charge.get("chargeName") or "")
        amount = _to_float((charge.get("chargeAmount") or {}).get("amount"))
        tax_name, tax_amount = _charge_tax(charge)
        currency = (charge.get("chargeAmount") or {}).get("currency") or "USD"

        if charge_type == "PRODUCT" or charge_name == "ItemPrice":
            result["item_price"] = amount
            result["product_tax"] = tax_amount
            result["currency"] = currency
            _append_tax_line(
                tax_lines,
                name="Product tax",
                amount=tax_amount,
                category="product",
            )
            continue

        if charge_type == "SHIPPING" or charge_name == "Shipping":
            result["shipping"] = amount
            result["shipping_tax"] = tax_amount
            _append_tax_line(
                tax_lines,
                name="Shipping tax",
                amount=tax_amount,
                category="shipping",
            )
            continue

        if tax_amount is not None and tax_amount > 0:
            label = tax_name or charge_name or "Other tax"
            _append_tax_line(
                tax_lines,
                name=label,
                amount=tax_amount,
                category="other",
            )

    result["total_tax"] = round(
        sum(float(row["amount"]) for row in tax_lines),
        2,
    )
    item_price = result.get("item_price")
    if item_price is not None:
        result["net_item_price"] = round(float(item_price) - discount, 2)
        qty = line_quantity(order_line)
        result["gross_unit_price"] = item_price_per_unit(float(item_price), qty)
        result["net_unit_price"] = net_unit_price(float(item_price), discount, qty)
    return result


def order_has_promotion_discount(order: dict[str, Any]) -> bool:
    """True if any line has a Walmart Promotion discount (negative PRODUCT charge)."""
    for line in as_list(order.get("orderLines", {}).get("orderLine")):
        if line_promotion_discount(line) > 0:
            return True
    return False


def order_has_county_surcharge_tax(order: dict[str, Any]) -> bool:
    """True if any line has County Surcharge Tax (or matching fee tax) with amount > 0."""
    target = "County Surcharge Tax"
    for line in as_list(order.get("orderLines", {}).get("orderLine")):
        for tax in parse_line_charges(line).get("tax_lines") or []:
            if str(tax.get("name") or "").strip() == target and float(tax.get("amount") or 0) > 0:
                return True
    return False


def item_price_per_unit(item_price: float, quantity: float) -> float:
    """Gross unit price = Walmart line item price divided by quantity."""
    qty = quantity if quantity > 0 else 1.0
    return round(item_price / qty, 2)


def net_unit_price(item_price: float, discount: float, quantity: float) -> float:
    """Net unit price after promotion discount (discount is line total, divided by qty)."""
    qty = quantity if quantity > 0 else 1.0
    return round((float(item_price) - float(discount)) / qty, 2)


def line_quantity(order_line: dict[str, Any]) -> float:
    raw = (order_line.get("orderLineQuantity") or {}).get("amount", "1")
    try:
        qty = float(raw)
    except (TypeError, ValueError):
        qty = 1.0
    return qty if qty > 0 else 1.0
