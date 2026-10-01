"""Build SAP B1 A/R Invoice payload for Walmart B2C shipped orders."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from walmart.dates import PACIFIC, ms_to_pacific_date_str
from walmart.order_pricing import (
    line_invoiceable_quantity,
    line_quantity,
    net_unit_price,
    parse_line_charges,
)
from walmart.sap_hana import DEFAULT_WAREHOUSE, fetch_batches_for_item
from walmart.sap_item_lookup import resolve_item_code
from walmart.walmart_store import (
    _as_list,
    order_ship_date_time_ms,
    order_shipping_state,
    valid_order_date_ms,
)

# SAP B1 constants (Walmart B2C)
CARD_CODE = "C00002"
SERIES = 120
PAY_TO_CODE = "Walmart-B2C"
WAREHOUSE_CODE = DEFAULT_WAREHOUSE
TAX_CODE = "etax0"
U_CHANNEL = "Walmart_US"
U_CREATED_BY = "Posted By Walmart API"
TAX_ITEM_CODE = "EXP000003"
TAX_ACCOUNT_CODE = "_SYS00000000059"
SHIPPING_ITEM_CODE = "EXP000004"
SHIPPING_ACCOUNT_CODE = "_SYS00000000084"


def _order_date_pacific_str(order_date_ms: int | None) -> str:
    """SAP TaxDate = Walmart orderDate in US Pacific."""
    if order_date_ms is None:
        return datetime.now(PACIFIC).strftime("%Y-%m-%d")
    return ms_to_pacific_date_str(int(order_date_ms))


def _ship_date_pacific_str(order: dict[str, Any], order_date_ms: int | None) -> str:
    """SAP DocDate = shipDateTime in US Pacific (falls back to order date Pacific)."""
    ship_ms = order_ship_date_time_ms(order)
    if ship_ms is not None:
        return ms_to_pacific_date_str(ship_ms)
    return _order_date_pacific_str(order_date_ms)


def _allocate_batches(
    item_code: str,
    quantity: float,
    *,
    require_batches: bool = True,
) -> list[dict[str, float]]:
    batches = fetch_batches_for_item(item_code, warehouse_code=WAREHOUSE_CODE)
    remaining = quantity
    allocations: list[dict[str, float]] = []

    for batch in batches:
        if remaining <= 0:
            break
        take = min(remaining, float(batch["avail_qty"]))
        if take <= 0:
            continue
        allocations.append(
            {
                "BatchNumber": str(batch["batch_number"]),
                "Quantity": round(take, 6),
            }
        )
        remaining -= take

    if remaining > 0:
        if not require_batches:
            return [
                {
                    "BatchNumber": "PREVIEW_NO_BATCH_STOCK",
                    "Quantity": round(quantity, 6),
                }
            ]
        raise ValueError(
            f"Insufficient batch stock for {item_code} in {WAREHOUSE_CODE}: "
            f"need {quantity}, short by {remaining}"
        )
    return allocations


def _product_document_line(
    *,
    item_code: str,
    quantity: float,
    unit_price: float,
    customer_order_id: str,
    purchase_order_id: str,
    require_batches: bool = True,
) -> dict[str, Any]:
    line: dict[str, Any] = {
        "ItemCode": item_code,
        "Quantity": quantity,
        "UnitPrice": round(unit_price, 2),
        "WarehouseCode": WAREHOUSE_CODE,
        "TaxCode": TAX_CODE,
        "BatchNumbers": _allocate_batches(
            item_code, quantity, require_batches=require_batches
        ),
        "U_Channel": U_CHANNEL,
        "U_OrderID": customer_order_id,
        "U_PurchaseOrderID": purchase_order_id,
    }
    return line


def _expense_line(
    *,
    item_code: str,
    unit_price: float,
    account_code: str,
    customer_order_id: str,
    purchase_order_id: str,
    quantity: float = 1,
    unit_price_precision: int = 2,
) -> dict[str, Any]:
    return {
        "ItemCode": item_code,
        "Quantity": quantity,
        "UnitPrice": round(unit_price, unit_price_precision),
        "WarehouseCode": WAREHOUSE_CODE,
        "TaxCode": TAX_CODE,
        "AccountCode": account_code,
        "U_Channel": U_CHANNEL,
        "U_OrderID": customer_order_id,
        "U_PurchaseOrderID": purchase_order_id,
    }


def build_sap_invoice_payload(
    conn: psycopg.Connection,
    order: dict[str, Any],
    *,
    require_batches: bool = True,
) -> dict[str, Any]:
    """Build SAP Service Layer Invoices payload from a Walmart order JSON."""
    purchase_order_id = str(order.get("purchaseOrderId") or "").strip()
    customer_order_id = str(order.get("customerOrderId") or "").strip()
    if not purchase_order_id or not customer_order_id:
        raise ValueError("Order missing purchaseOrderId or customerOrderId")

    order_date_ms_int = valid_order_date_ms(order.get("orderDate"))
    if order_date_ms_int is None:
        raise ValueError(
            f"Invalid Walmart orderDate {order.get('orderDate')!r} for PO {purchase_order_id}; "
            "use 'Refetch from Walmart' on the order, then post again"
        )
    doc_date = _ship_date_pacific_str(order, order_date_ms_int)
    tax_date = _order_date_pacific_str(order_date_ms_int)
    ship_node_type = (order.get("shipNode") or {}).get("type")

    document_lines: list[dict[str, Any]] = []
    total_all_tax = 0.0

    order_lines = _as_list(order.get("orderLines", {}).get("orderLine"))
    for order_line in order_lines:
        item = order_line.get("item") or {}
        wm_sku = str(item.get("sku") or "").strip()
        if not wm_sku:
            raise ValueError(f"Order {purchase_order_id} has a line without SKU")

        charges = parse_line_charges(order_line)
        # Walmart sometimes returns empty charges[]; treat missing ItemPrice as 0.
        item_price = charges.get("item_price")
        if item_price is None:
            item_price = 0.0

        qty = line_quantity(order_line)
        invoiceable_qty = line_invoiceable_quantity(order_line)
        if invoiceable_qty <= 0:
            continue
        fulfilled_ratio = min(invoiceable_qty / qty, 1.0)
        discount = float(charges.get("discount") or 0.0)
        unit_price = net_unit_price(float(item_price), discount, qty)
        item_code = resolve_item_code(conn, wm_sku)

        shipping = float(charges.get("shipping") or 0.0)
        total_all_tax += float(charges.get("total_tax") or 0.0) * fulfilled_ratio

        document_lines.append(
            _product_document_line(
                item_code=item_code,
                quantity=invoiceable_qty,
                unit_price=unit_price,
                customer_order_id=customer_order_id,
                purchase_order_id=purchase_order_id,
                require_batches=require_batches,
            )
        )

        if shipping > 0:
            document_lines.append(
                _expense_line(
                    item_code=SHIPPING_ITEM_CODE,
                    quantity=1,
                    unit_price=shipping * fulfilled_ratio,
                    unit_price_precision=6,
                    account_code=SHIPPING_ACCOUNT_CODE,
                    customer_order_id=customer_order_id,
                    purchase_order_id=purchase_order_id,
                )
            )

    # Single tax expense line: product tax + fee taxes (e.g. County Surcharge) + shipping tax.
    total_tax = round(total_all_tax, 2)
    if total_tax > 0:
        document_lines.append(
            _expense_line(
                item_code=TAX_ITEM_CODE,
                unit_price=total_tax,
                account_code=TAX_ACCOUNT_CODE,
                customer_order_id=customer_order_id,
                purchase_order_id=purchase_order_id,
            )
        )

    comments = (
        f"Walmart B2C | Order {purchase_order_id} | CustomerOrderId {customer_order_id}"
    )
    if ship_node_type:
        comments += f" | ShipNode {ship_node_type}"

    ship_to_code = order_shipping_state(order) or PAY_TO_CODE

    return {
        "CardCode": CARD_CODE,
        "DocDate": doc_date,
        "TaxDate": tax_date,
        "DocDueDate": doc_date,
        "Series": SERIES,
        "NumAtCard": customer_order_id,
        "PayToCode": ship_to_code,
        "ShipToCode": ship_to_code,
        "U_CreatedBy": U_CREATED_BY,
        "Comments": comments,
        "DocumentLines": document_lines,
    }
