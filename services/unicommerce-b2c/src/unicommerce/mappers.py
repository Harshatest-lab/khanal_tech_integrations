"""UC saleOrderDTO/return JSON -> staging-row field extraction.

Ported from khanal_tech_integrations/unicommerce/workers/fetch.py's
_extract_* / _build_line_items / _compute_uc_financials -- pure functions,
no Frappe dependency, so these carried over almost verbatim.
"""

from __future__ import annotations

from typing import Any

from unicommerce.dates import extract_order_date, format_cursor, parse_source_datetime

_DATA_MAX = 140


def normalize_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def safe_str(value: Any, max_len: int = _DATA_MAX) -> str:
    # Do NOT use `value or ""` -- that turns 0 / 0.0 into "" (wrong for numeric fields).
    if value is None:
        return ""
    return str(value).strip()[:max_len]


def safe_int(value: Any) -> int:
    try:
        return int(str(value or "").strip() or "0")
    except Exception:
        return 0


def extract_uc_order_id(order: dict) -> str | None:
    return order.get("saleOrderCode") or order.get("code") or order.get("displayOrderCode") or None


def extract_channel(order: dict) -> str:
    return order.get("channel") or order.get("channelName") or (order.get("saleOrderDTO") or {}).get("channel") or ""


def eligible_line_items(order: dict) -> list[dict]:
    active_items = []
    for item in order.get("saleOrderItems") or []:
        cancellation_reason = str(item.get("cancellationReason") or "").strip()
        status_code = str(item.get("statusCode") or "").strip().upper()
        on_hold = bool(item.get("onHold"))
        # KNOWN BUG, inherited faithfully from the Frappe source (same `or`
        # chain there): an explicit quantity of 0 is falsy in Python, so
        # this silently falls through to the `or 1` default instead of
        # being caught by the `quantity <= 0` check below. Kept as-is on
        # purpose for now so this Lambda's behavior is provably identical
        # to Frappe's during the dual-run comparison (docs/migration/
        # 03-unicommerce-b2c.md) -- fix once parity is confirmed, not before.
        try:
            quantity = float(
                item.get("quantity")
                or item.get("totalQuantity")
                or item.get("completedQuantity")
                or item.get("allocatedQuantity")
                or item.get("inProcessQuantity")
                or item.get("unallocatedQuantity")
                or 1
            )
        except Exception:
            quantity = 1.0
        if cancellation_reason or on_hold or quantity <= 0:
            continue
        if status_code in {"CANCELLED", "INVALID", "UNFULFILLABLE"}:
            continue
        active_items.append(item)
    return active_items


def order_ready_for_invoice(order: dict) -> tuple[bool, str]:
    order_status = str(order.get("status") or "").strip().upper()
    if order_status not in {"COMPLETE", "COMPLETED"}:
        return False, f"Order status is {order_status or 'unknown'} (requires COMPLETE)"
    if not eligible_line_items(order):
        return False, "Order has no invoice-eligible line items"
    return True, ""


def extract_billing_address(order: dict) -> dict:
    addr: dict = order.get("billingAddress") or order.get("shippingAddress") or {}
    if not addr:
        addr = ((order.get("addresses") or [{}])[0]) or {}
    return {
        "customer_name": safe_str(addr.get("name") or addr.get("customerName")),
        "city": safe_str(addr.get("city")),
        "state": safe_str(addr.get("state")),
        "district": safe_str(addr.get("district")),
        "pin_code": safe_int(addr.get("pincode") or addr.get("pinCode") or addr.get("zip")),
    }


def extract_shipping_package_fields(order: dict) -> dict:
    packages = order.get("shippingPackages") or []
    if not packages:
        return {}
    pkg = packages[0] or {}
    dispatched = pkg.get("dispatched")
    shipment_date = None
    if dispatched:
        dt = parse_source_datetime(dispatched)
        shipment_date = format_cursor(dt) if dt else None
    return {
        "shippingpackages": pkg,
        "shipment_status": safe_str(pkg.get("status")),
        "shipment_date": shipment_date,
    }


def extract_return_fields(order: dict) -> dict:
    returns = order.get("returns") or []
    if not returns:
        return {}
    ret = returns[0] or {}
    return {
        "return_code": safe_str(ret.get("code")),
        "statuscode": safe_str(ret.get("statusCode")),
        "shippingprovider": safe_str(ret.get("shippingProvider")),
        "trackingnumber": safe_str(ret.get("trackingNumber")),
        "trackingstatus": safe_str(ret.get("trackingStatus")),
        "providerstatus": safe_str(ret.get("providerStatus")),
        "putawaycode": safe_str(ret.get("putawayCode")),
        "returninvoicecode": safe_str(ret.get("returnInvoiceCode")),
        "returninvoicedisplaycode": safe_str(ret.get("returnInvoiceDisplayCode")),
        "actioncode": safe_str(ret.get("actionCode")),
        "type": safe_str(ret.get("type")),
        "returncompleteddate": safe_str(ret.get("returnCompletedDate")),
        "inventoryreceiveddate": safe_str(ret.get("inventoryReceivedDate")),
        "returnfacilitycode": safe_str(ret.get("returnFacilityCode")),
        "returnitems": ret if ret else None,
    }


def compute_uc_financials(order: dict) -> dict:
    """Raw UC financial totals from saleOrderItems at fetch time -- gives
    reconciliation a 3-way triangle: UC source -> expected -> SAP actual."""
    taxable = cgst = sgst = igst = shipping = 0.0
    for item in order.get("saleOrderItems") or []:
        try:
            qty = float(item.get("quantity") or item.get("totalQuantity") or 1)
        except Exception:
            qty = 1.0
        try:
            selling_wo_tax = float(item.get("sellingPriceWithoutTaxesAndDiscount") or 0)
        except Exception:
            selling_wo_tax = 0.0
        try:
            discount = float(item.get("discount") or 0)
        except Exception:
            discount = 0.0
        per_unit_disc = (discount / qty) if qty > 0 else 0.0
        unit_price = max(0.0, selling_wo_tax - per_unit_disc)
        line_taxable = unit_price * qty
        taxable += line_taxable

        try:
            igst_pct = float(item.get("integratedGstPercentage") or 0)
        except Exception:
            igst_pct = 0.0
        try:
            cgst_pct = float(
                item["centralGstPercentage"] if "centralGstPercentage" in item else item.get("centralGstTaxPercentage") or 0
            )
        except Exception:
            cgst_pct = 0.0
        try:
            sgst_pct = float(
                item["stateGstPercentage"] if "stateGstPercentage" in item else item.get("stateGstTaxPercentage") or 0
            )
        except Exception:
            sgst_pct = 0.0

        if igst_pct > 0:
            igst += line_taxable * igst_pct / 100.0
        else:
            cgst += line_taxable * cgst_pct / 100.0
            sgst += line_taxable * sgst_pct / 100.0

        try:
            item_shipping = (
                float(item.get("shippingCharges") or 0)
                + float(item.get("shippingMethodCharges") or 0)
                + float(item.get("cashOnDeliveryCharges") or 0)
            )
        except Exception:
            item_shipping = 0.0
        shipping += item_shipping

    total = taxable + cgst + sgst + igst + shipping
    return {
        "uc_taxable_value": round(taxable, 2),
        "uc_cgst": round(cgst, 2),
        "uc_sgst": round(sgst, 2),
        "uc_igst": round(igst, 2),
        "uc_shipping": round(shipping, 2),
        "uc_total": round(total, 2),
    }


def extract_order_scalar_fields(order: dict) -> dict:
    """Map all flat UC saleOrderDTO fields to unicommerce_orders columns."""
    created_dt = parse_source_datetime(order.get("created"))
    updated_dt = parse_source_datetime(order.get("updated"))
    display_dt = parse_source_datetime(order.get("displayOrderDateTime"))

    cod_raw = order.get("cod")
    cod_value = str(cod_raw) if cod_raw is not None else ""
    channel_id = safe_str(order.get("displayOrderCode") or order.get("channelOrderId") or "")

    return {
        "channel_id": channel_id,
        "status": safe_str(order.get("status")),
        "created": created_dt,
        "updated": updated_dt,
        "displayorderdatetime": display_dt,
        "cod": cod_value,
        **extract_billing_address(order),
        **extract_shipping_package_fields(order),
        **extract_return_fields(order),
        **compute_uc_financials(order),
    }


def extract_return_item_map(order: dict) -> dict[str, dict]:
    """saleOrderItemCode -> return item payload, from order returns[0].returnItems."""
    returns = order.get("returns") or []
    if not returns:
        return {}
    ret = returns[0] or {}
    mapped: dict[str, dict] = {}
    for item in ret.get("returnItems") or []:
        code = safe_str(item.get("saleOrderItemCode"))
        if code:
            mapped[code] = item
    return mapped


def build_line_items(order: dict) -> list[dict]:
    """UC saleOrderItems -> unicommerce_order_lines rows."""
    rows = []
    return_item_map = extract_return_item_map(order)
    return_status = safe_str(((order.get("returns") or [{}])[0] or {}).get("statusCode"))
    for item in order.get("saleOrderItems") or []:
        batch_dto = item.get("batchDTO") or {}
        batch_fields_dto = batch_dto.get("batchFieldsDTO") or {}
        batch_code = safe_str(batch_dto.get("batchCode") or item.get("batchCode"))
        vendor_batch = safe_str(batch_fields_dto.get("vendorBatchNumber") or item.get("vendorBatchNumber"))
        created_dt = parse_source_datetime(item.get("created"))
        updated_dt = parse_source_datetime(item.get("updated"))
        code = safe_str(item.get("code"))

        rows.append(
            {
                "item_id": safe_str(item.get("id")),
                "itemsku": safe_str(item.get("itemSku")),
                "sellerskucode": safe_str(item.get("sellerSkuCode")),
                "channelproductid": safe_str(item.get("channelProductId")),
                "quantity": safe_str(item.get("quantity") or item.get("totalQuantity") or "1"),
                "total_price": safe_str(item.get("totalPrice")),
                "selling_price": safe_str(item.get("sellingPrice")),
                "created": format_cursor(created_dt) if created_dt else safe_str(item.get("created")),
                "updated": format_cursor(updated_dt) if updated_dt else safe_str(item.get("updated")),
                "totalintegratedgst": safe_str(item.get("totalIntegratedGst")),
                "integratedgstpercentage": safe_str(item.get("integratedGstPercentage")),
                "sellingpricewithouttaxesanddiscount": safe_str(item.get("sellingPriceWithoutTaxesAndDiscount")),
                "shippingcharges": safe_str(item.get("shippingCharges")),
                "shippingmethodcharges": safe_str(item.get("shippingMethodCharges")),
                "cashondeliverycharges": safe_str(item.get("cashOnDeliveryCharges")),
                "cancellationreason": safe_str(item.get("cancellationReason")),
                # Legacy field naming (stateGstPercentage / centralGstPercentage, NOT
                # the *TaxPercentage variants) -- must NOT use `or` since 0.0 is falsy.
                "state_gst_tax_percentage": safe_str(
                    item["stateGstPercentage"] if "stateGstPercentage" in item else item.get("stateGstTaxPercentage")
                ),
                "central_gst_tax_percentage": safe_str(
                    item["centralGstPercentage"] if "centralGstPercentage" in item else item.get("centralGstTaxPercentage")
                ),
                "statuscode": safe_str(item.get("statusCode")),
                "discount": safe_str(item.get("discount")),
                "shippingchargetaxpercentage": safe_str(item.get("shippingChargeTaxPercentage")),
                "bundleskucode": safe_str(item.get("bundleSkuCode")),
                "code": code,
                "batchcode": batch_code,
                "vendorbatchnumber": vendor_batch,
                "uniware_return_status": return_status if code in return_item_map else "",
                "uniware_return_items": return_item_map.get(code) or None,
            }
        )
    return rows
