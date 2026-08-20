"""Build SAP B1 Service Layer AR Invoice / Credit Note payloads from staged
UC orders and returns.

Ported from khanal_tech_integrations/unicommerce/mappers/sap_payload.py.
frappe.get_single("Unicommerce Settings") becomes a required `settings`
argument (unicommerce.settings.UnicommerceSettings); frappe.throw becomes
raise ValueError; json.loads(settings.x or "{}") becomes settings.x
directly since our settings dataclass already holds parsed dicts.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from unicommerce.geo import classify_tax_bucket, extract_shipping_state, normalize_state_code, resolve_mapped_value, resolve_origin_state_code
from unicommerce.hana_access import bulk_batch_inventory_by_warehouse
from unicommerce.mappers import normalize_text
from unicommerce.settings import UnicommerceSettings
from unicommerce.tax import compute_tax_from_line, get_tax_code

ORIGIN_STATE = "KA"
SHIPPING_ITEM_CODE = "EXCM0027"

# Warehouse routing — single source of truth. Amazon FBA fulfilment-centre
# channels have their own dedicated warehouses; every other channel ships
# out of EC-FG. Keyed by UPPER-CASED channel name so case differences
# can't cause a mis-route.
CHANNEL_WAREHOUSE_MAP: dict[str, str] = {
    "AMAZON_FBA_IN_BOM5": "AMZ-BOM5",
    "AMAZON_FBA_IN_BOM7": "AMZ-BOM7",
    "AMAZON_FBA_IN_BLR7": "AMZ-BLR7",
    "AMAZON_FBA_IN_CJB1": "AMZ-CJB1",
    "AMAZON_FBA_IN_MAA4": "AMZ-MAA4",
    "AMAZON_FBA_IN_DEL4": "AMZ-DEL4",
    "AMAZON_FBA_IN_DEL5": "AMZ-DEL5",
    "AMAZON_FBA_IN_BLR5": "AMZ-BLR5",
    "AMAZON_FBA_IN_BLR8": "AMZ-BLR8",
    "AMAZON_FBA_IN_BLR4": "AMZ-BLR4",
}
DEFAULT_WAREHOUSE = "EC-FG"
RETURN_WAREHOUSE_CODE = "EC-QA"

# Must match BATCH_QTY_TOLERANCE in the (not yet ported) posting worker.
BATCH_QTY_TOLERANCE = 0.01


def resolve_customer_code(settings: UnicommerceSettings, channel: str) -> str:
    customer_code = resolve_mapped_value(settings.channel_customer_map, channel) or resolve_mapped_value(
        settings.channel_customer_map, "default"
    )
    if not customer_code:
        raise ValueError(f"No SAP customer mapped for channel: {channel}")
    return str(customer_code)


def _resolve_ar_invoice_series(settings: UnicommerceSettings, channel: str) -> int:
    channel_key = (channel or "").strip()
    series = settings.ar_invoice_series_map.get(channel_key)
    if series is None and channel_key:
        channel_upper = channel_key.upper()
        for key, value in settings.ar_invoice_series_map.items():
            if str(key).upper() == channel_upper:
                series = value
                break
    if series is None:
        series = settings.ar_invoice_series
    if not series:
        raise ValueError(
            f"AR invoice series not configured for channel: {channel}. "
            "Set unicommerce settings ar_invoice_series_map or fallback ar_invoice_series."
        )
    return int(series)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except Exception:
        return float(default)


def _resolve_order_address(uc_data: dict) -> dict:
    return (
        uc_data.get("billingAddress")
        or uc_data.get("shippingAddress")
        or ((uc_data.get("addresses") or [{}])[0] or {})
        or {}
    )


def _compose_sap_address(uc_data: dict) -> str:
    addr = _resolve_order_address(uc_data)
    parts = [
        str(addr.get("addressLine1") or "").strip(),
        str(addr.get("addressLine2") or "").strip(),
        str(addr.get("city") or "").strip(),
        str(addr.get("state") or "").strip(),
        str(addr.get("pincode") or "").strip(),
        str(addr.get("country") or "").strip(),
    ]
    return ", ".join([p for p in parts if p])[:254]


def _resolve_transportation_code(uc_data: dict) -> int | None:
    """Platform-fulfilled orders map to code 5."""
    third_party = uc_data.get("thirdPartyShipping")
    provider = str(uc_data.get("shippingProvider") or "").strip().lower()
    if third_party is True:
        return 5
    if provider and provider not in ("self", "self pickup"):
        return 5
    return None


def _resolve_gst_transaction_type(uc_data: dict, total_tax_expected: float) -> str:
    """Force regular GST Tax Invoice -- some SAP localizations infer export
    flow when the header transaction type is omitted."""
    return "gsttrantyp_GSTTaxInvoice"


def _raw_order_json_for_sap(order_json_str: str) -> str:
    max_len = 64000
    raw = order_json_str or "{}"
    if len(raw) > max_len:
        raw = raw[: max_len - 3] + "..."
    return raw


def _resolve_shipping_tax_percent(item: dict, default_tax_percent: float, channel: str, item_code: str) -> float:
    """Shipping GST % from UC shippingChargeTaxPercentage; falls back to
    item tax % if a shipping charge exists but its tax % is missing/zero."""
    freight_gross = _resolve_freight_gross(item)
    if freight_gross <= 0:
        return 0.0

    raw = item.get("shippingChargeTaxPercentage")
    if raw in (None, ""):
        raw = item.get("shippingchargetaxpercentage")
    percent = _to_float(raw)
    if percent > 0:
        return percent

    fallback = max(_to_float(default_tax_percent), 0.0)
    return fallback


def _resolve_freight_gross(item: dict) -> float:
    shipping = _to_float(item.get("shippingCharges") or item.get("shippingcharges"))
    shipping_method = _to_float(item.get("shippingMethodCharges") or item.get("shippingmethodcharges"))
    cod = _to_float(item.get("cashOnDeliveryCharges") or item.get("cashondeliverycharges"))
    return round(max(0.0, shipping + shipping_method + cod), 4)


def _resolve_sap_warehouse(settings: UnicommerceSettings, channel: str, uc_wh_code: str) -> str:
    channel_upper = (channel or "").strip().upper()
    if channel_upper in CHANNEL_WAREHOUSE_MAP:
        return CHANNEL_WAREHOUSE_MAP[channel_upper]

    resolved = resolve_mapped_value(settings.warehouse_map, uc_wh_code)
    if resolved:
        return str(resolved).strip()
    resolved = resolve_mapped_value(settings.channel_warehouse_map, channel)
    if resolved:
        return str(resolved).strip()

    return DEFAULT_WAREHOUSE


def _resolve_bill_ship_codes(settings: UnicommerceSettings, channel: str, tax_bucket: str) -> tuple[str, str]:
    bucket_key = "igst" if tax_bucket == "igst" else "cgst_sgst"
    default_bill = "B2C IGST ADD" if bucket_key == "igst" else "B2C SGST ADD"

    lookup_keys = [f"{channel}:{bucket_key}", channel, bucket_key, "default"]

    bill_to_code = default_bill
    ship_to_code = default_bill
    for key in lookup_keys:
        if key and resolve_mapped_value(settings.bill_to_code_map, key) is not None:
            bill_to_code = str(resolve_mapped_value(settings.bill_to_code_map, key)).strip()
            break
    for key in lookup_keys:
        if key and resolve_mapped_value(settings.ship_to_code_map, key) is not None:
            ship_to_code = str(resolve_mapped_value(settings.ship_to_code_map, key)).strip()
            break

    return bill_to_code, ship_to_code or bill_to_code


def _with_batch_item_code(batch_numbers: list[dict], item_code: str) -> list[dict]:
    normalized_item_code = normalize_text(item_code)
    rows: list[dict] = []
    for row in batch_numbers or []:
        if not isinstance(row, dict):
            continue
        updated = dict(row)
        if normalized_item_code:
            updated["ItemCode"] = normalized_item_code
        rows.append(updated)
    return rows


def _gl_mode_from_tax_bucket(tax_bucket: str) -> str | None:
    if tax_bucket == "cgst_sgst":
        return "intra"
    if tax_bucket == "igst":
        return "inter"
    return None


def _resolve_gl_channel_section(gl_map: dict, channel: str) -> dict | None:
    if not isinstance(gl_map, dict) or not gl_map:
        return None
    ch = (channel or "").strip()
    section = resolve_mapped_value(gl_map, ch)
    if section is None and ch:
        ch_upper = ch.upper()
        for key, value in gl_map.items():
            if str(key).strip().upper() == ch_upper:
                section = value
                break
    if section is None:
        section = resolve_mapped_value(gl_map, "default")
    return section if isinstance(section, dict) else None


def _resolve_gl_state_section(channel_section: dict, ship_state: str) -> dict | None:
    if not isinstance(channel_section, dict):
        return None
    st = normalize_state_code(ship_state) if ship_state else ""
    section = resolve_mapped_value(channel_section, st) if st else None
    if section is None:
        section = resolve_mapped_value(channel_section, "default")
    return section if isinstance(section, dict) else None


def _resolve_gl_account_code(
    settings: UnicommerceSettings, channel: str, ship_state: str, tax_bucket: str, line_kind: str
) -> str | None:
    """line_kind: "product" | "shipping"."""
    gl_map = settings.gl_account_map
    if not gl_map:
        return None
    mode = _gl_mode_from_tax_bucket(tax_bucket)
    if not mode:
        return None
    ch_sec = _resolve_gl_channel_section(gl_map, channel)
    if not ch_sec:
        return None
    st_sec = _resolve_gl_state_section(ch_sec, ship_state)
    if not st_sec:
        return None
    mode_sec = st_sec.get(mode)
    if isinstance(mode_sec, str):
        return mode_sec.strip() or None
    if not isinstance(mode_sec, dict):
        return None
    acct = mode_sec.get(line_kind)
    if acct in (None, "") and line_kind == "product":
        acct = mode_sec.get("default")
    return str(acct).strip() or None if acct not in (None, "") else None


def fefo_allocate_from_rows(rows: list[dict], qty_needed: float) -> list[dict]:
    """Allocate qty_needed across already-fetched FEFO rows (must already
    be sorted earliest-expiry-first). Returns SAP BatchNumbers rows, or []
    when available quantity is below qty_needed (minus BATCH_QTY_TOLERANCE)."""
    needed = round(float(qty_needed or 0), 3)
    if needed <= 0:
        return []

    by_batch: dict[str, float] = {}
    for row in rows or []:
        batch = normalize_text(row.get("batch_number") or row.get("distnumber"))
        if not batch:
            continue
        try:
            qty = float(row.get("quantity") or 0)
        except Exception:
            qty = 0.0
        if qty <= 0:
            continue
        by_batch[batch] = round(float(by_batch.get(batch, 0) + qty), 3)

    allocations: list[dict] = []
    remaining = needed
    for batch, qty in by_batch.items():
        if remaining <= 0:
            break
        take = min(qty, remaining)
        if take <= 0:
            continue
        allocations.append({"BatchNumber": batch, "Quantity": round(float(take), 3)})
        remaining = round(remaining - take, 3)

    if remaining > BATCH_QTY_TOLERANCE:
        return []
    return allocations


def parse_line_items(uc_data: dict) -> list[dict]:
    direct_items = uc_data.get("items", [])
    if direct_items:
        return direct_items

    so_items = uc_data.get("saleOrderItems", [])
    if not so_items:
        return []

    normalized = []
    for row in so_items:
        qty = (
            row.get("quantity")
            or row.get("totalQuantity")
            or row.get("completedQuantity")
            or row.get("allocatedQuantity")
            or row.get("inProcessQuantity")
            or row.get("unallocatedQuantity")
            or 1
        )
        tax_percent = (
            row.get("taxPercentage")
            or row.get("integratedGstPercentage")
            or ((row.get("stateGstPercentage") or 0) + (row.get("centralGstPercentage") or 0))
            or 0
        )
        unit_price = row.get("sellingPriceWithoutTaxesAndDiscount")
        if unit_price in (None, ""):
            unit_price = row.get("sellingPrice") or row.get("totalPrice") or 0

        batch_num = ((row.get("batchDTO") or {}).get("batchFieldsDTO") or {}).get("vendorBatchNumber") or ""
        normalized.append(
            {
                "itemSku": normalize_text(row.get("itemSku")),
                "quantity": qty,
                "unitPrice": unit_price,
                "discount": row.get("discount"),
                "taxPercent": tax_percent,
                "batchNumber": normalize_text(batch_num),
                "warehouseCode": normalize_text(row.get("facilityCode")),
                "saleOrderItemCode": normalize_text(row.get("code")),
                "bundleSkuCode": normalize_text(row.get("bundleSkuCode")),
                "shippingCharges": row.get("shippingCharges"),
                "shippingChargeTaxPercentage": row.get("shippingChargeTaxPercentage"),
                "shippingMethodCharges": row.get("shippingMethodCharges"),
                "cashOnDeliveryCharges": row.get("cashOnDeliveryCharges"),
                "shippingcharges": row.get("shippingcharges"),
                "shippingchargetaxpercentage": row.get("shippingchargetaxpercentage"),
                "shippingmethodcharges": row.get("shippingmethodcharges"),
                "cashondeliverycharges": row.get("cashondeliverycharges"),
            }
        )
    return normalized


def _append_sales_line(
    doc_lines: list[dict],
    item: dict,
    ship_state: str,
    origin_state_code: str,
    tax_inclusive: bool,
    settings: UnicommerceSettings,
    channel: str,
    tax_bucket: str,
    tree_type: str | None = None,
    freight_buckets: dict | None = None,
    line_udfs: dict | None = None,
    batch_managed_cache: dict | None = None,
    fefo_rows_cache: dict | None = None,
) -> float:
    item_code = normalize_text(item.get("itemSku"))
    qty = float(item.get("quantity", 0) or 0)
    unit_price = float(item.get("unitPrice", 0) or 0)
    discount_total = _to_float(item.get("discount"))
    if discount_total > 0 and qty > 0:
        unit_price = max(0.0, unit_price - (discount_total / qty))
    tax_percent = float(item.get("taxPercent", 0) or 0)
    uc_wh_code = normalize_text(item.get("warehouseCode"))
    sap_wh_code = _resolve_sap_warehouse(settings, channel, uc_wh_code)

    if not item_code:
        raise ValueError("Missing itemSku in staged order payload")
    if qty <= 0:
        raise ValueError(f"Invalid quantity for item {item_code}")

    # UC's own recorded batchNumber (if any, on item.get("batchNumber")) is
    # informational only, not a hard requirement -- the batch actually
    # invoiced is always whichever FEFO (first-expired-first-out) selects
    # from what's currently available, same as _check_batches validates.
    # A batch UC picked at fulfillment time can be fully consumed by later
    # orders before this invoice ever posts (confirmed live, 2026-08-19:
    # re-posting week-old orders hit "have 0.0" on UC's exact batch while
    # other batches of the same item were available) -- hard-locking to it
    # just fails instead of substituting, which FEFO exists to prevent.
    batch_numbers: list[dict] = []
    cache_entry = None
    if batch_managed_cache is not None:
        cache_entry = batch_managed_cache.get(item_code)
    if cache_entry is None and batch_managed_cache is not None and item_code in batch_managed_cache:
        batch_numbers = []
    else:
        rows = (fefo_rows_cache or {}).get((item_code, sap_wh_code), [])
        batch_numbers = fefo_allocate_from_rows(rows, qty)
    batch_numbers = _with_batch_item_code(batch_numbers, item_code)

    tax_code = get_tax_code(tax_percent, ship_state, origin_state_code, settings)
    line_tax = compute_tax_from_line(unit_price, qty, tax_percent, tax_inclusive)
    line = {
        "LineNum": len(doc_lines),
        "ItemCode": item_code,
        "Quantity": qty,
        "UnitPrice": unit_price if not tax_inclusive else round(unit_price / (1 + tax_percent / 100), 4),
        "WarehouseCode": sap_wh_code,
        "TaxCode": tax_code,
        "TaxType": "tt_Yes",
        "TaxLiable": "tYES",
        "TaxTotal": round(line_tax, 5),
        "BatchNumbers": batch_numbers,
        "U_Sale_order_itemcode": normalize_text(item.get("saleOrderItemCode")),
        "U_ActualItem": item_code,
    }
    if line_udfs:
        line.update(line_udfs)
    gl_acct = _resolve_gl_account_code(settings, channel, ship_state, tax_bucket, "product")
    if gl_acct:
        line["AccountCode"] = gl_acct
    if tree_type:
        line["TreeType"] = tree_type
    doc_lines.append(line)

    freight_gross = _resolve_freight_gross(item)
    freight_tax_percent = _resolve_shipping_tax_percent(item, tax_percent, channel, item_code)
    if freight_gross > 0:
        freight_tax_code = get_tax_code(freight_tax_percent, ship_state, origin_state_code, settings)
        line_tax += compute_tax_from_line(freight_gross, 1, freight_tax_percent, tax_inclusive=True)
        if freight_buckets is None:
            freight_buckets = {}
        bucket_key = (sap_wh_code, freight_tax_code, round(float(freight_tax_percent or 0), 4))
        if bucket_key not in freight_buckets:
            freight_buckets[bucket_key] = {
                "warehouse_code": sap_wh_code,
                "tax_code": freight_tax_code,
                "tax_percent": round(float(freight_tax_percent or 0), 4),
                "gross": 0.0,
            }
        freight_buckets[bucket_key]["gross"] = round(float(freight_buckets[bucket_key].get("gross") or 0) + freight_gross, 4)

    return line_tax


def ar_invoice_payload(
    uniware_id: str,
    channel: str,
    order_json: str,
    uc_data: dict,
    order_date: str,
    settings: UnicommerceSettings,
    batch_managed_cache: dict | None = None,
    fefo_rows_cache: dict | None = None,
) -> tuple[dict, float]:
    """Build the SAP AR Invoice payload from a staged UC order.

    batch_managed_cache is a tri-state dict (True/False/None) shared with
    the pre-post validator. When the SAP ManageBatchNumbers lookup for an
    item failed, the cache holds None and this builder skips batch
    allocation so both layers make the same decision.

    Returns (payload, total_tax_expected).
    """
    tax_inclusive_map = settings.tax_inclusive_map
    customer_code = resolve_customer_code(settings, channel)

    items = parse_line_items(uc_data)
    if not items:
        raise ValueError(f"No items found in order_json for order {uniware_id}")

    tax_inclusive = bool(resolve_mapped_value(tax_inclusive_map, channel, False))
    ship_state = extract_shipping_state(uc_data) or ORIGIN_STATE
    origin_state_code = resolve_origin_state_code(channel, settings)
    tax_bucket = classify_tax_bucket(ship_state, None, origin_state_code)
    bill_to_code, ship_to_code = _resolve_bill_ship_codes(settings, channel, tax_bucket)
    order_address = _resolve_order_address(uc_data)
    order_id = normalize_text(uc_data.get("code") or uniware_id)
    raw_order_json = _raw_order_json_for_sap(order_json)
    line_udfs = {
        "U_Order": order_id,
        "U_OrderedOn": order_date,
        "U_BuyerName": str(order_address.get("name") or "").strip(),
        "U_City": str(order_address.get("city") or "").strip(),
        "U_State": str(order_address.get("state") or "").strip(),
        "U_PINCode": str(order_address.get("pincode") or "").strip(),
        "U_Country": str(order_address.get("country") or "India").strip(),
        "U_Order_JSON": raw_order_json,
    }
    doc_lines: list[dict] = []
    total_tax_expected = 0.0
    processed_bundle_codes: set[str] = set()
    freight_buckets: dict[tuple, dict] = {}

    for item in items:
        bundle_code = normalize_text(item.get("bundleSkuCode"))
        if bundle_code:
            if bundle_code in processed_bundle_codes:
                continue
            bundle_items = [row for row in items if normalize_text(row.get("bundleSkuCode")) == bundle_code]
            if not bundle_items:
                continue
            processed_bundle_codes.add(bundle_code)
            # min(): minimum component quantity == bundle-unit count when
            # every component's per-bundle ratio is >= 1. max() over-counts
            # when ratios differ.
            bundle_qty = min(float(row.get("quantity", 0) or 0) for row in bundle_items)
            if not bundle_qty:
                continue
            parent_tax_percent = float(bundle_items[0].get("taxPercent", 0) or 0)
            parent_uc_wh = bundle_items[0].get("warehouseCode", "")
            parent_sap_wh = _resolve_sap_warehouse(settings, channel, parent_uc_wh)
            parent_tax_code = get_tax_code(parent_tax_percent, ship_state, origin_state_code, settings)
            doc_lines.append(
                {
                    "LineNum": len(doc_lines),
                    "ItemCode": bundle_code,
                    "Quantity": bundle_qty,
                    "UnitPrice": 0.0,
                    "WarehouseCode": parent_sap_wh,
                    "TaxCode": parent_tax_code,
                    "TaxType": "tt_Yes",
                    "TaxLiable": "tYES",
                    "TaxTotal": 0.0,
                    "BatchNumbers": [],
                    "TreeType": "iSalesTree",
                }
            )
            for bundle_item in bundle_items:
                total_tax_expected += _append_sales_line(
                    doc_lines=doc_lines,
                    item=bundle_item,
                    ship_state=ship_state,
                    origin_state_code=origin_state_code,
                    tax_inclusive=tax_inclusive,
                    settings=settings,
                    channel=channel,
                    tax_bucket=tax_bucket,
                    tree_type="iIngredient",
                    freight_buckets=freight_buckets,
                    line_udfs=line_udfs,
                    batch_managed_cache=batch_managed_cache,
                    fefo_rows_cache=fefo_rows_cache,
                )
            continue

        total_tax_expected += _append_sales_line(
            doc_lines=doc_lines,
            item=item,
            ship_state=ship_state,
            origin_state_code=origin_state_code,
            tax_inclusive=tax_inclusive,
            settings=settings,
            channel=channel,
            tax_bucket=tax_bucket,
            freight_buckets=freight_buckets,
            line_udfs=line_udfs,
            batch_managed_cache=batch_managed_cache,
            fefo_rows_cache=fefo_rows_cache,
        )

    for bucket in sorted(
        freight_buckets.values(),
        key=lambda row: (str(row.get("warehouse_code") or ""), str(row.get("tax_code") or ""), float(row.get("tax_percent") or 0)),
    ):
        freight_gross = _to_float(bucket.get("gross"))
        freight_tax_percent = _to_float(bucket.get("tax_percent"))
        sap_wh_code = str(bucket.get("warehouse_code") or "").strip()
        tax_code = str(bucket.get("tax_code") or "").strip()
        freight_unit_price_ex_tax = (
            freight_gross * (100 / (100 + freight_tax_percent)) if freight_tax_percent > 0 else freight_gross
        )
        freight_line = {
            "LineNum": len(doc_lines),
            "ItemCode": SHIPPING_ITEM_CODE,
            "Quantity": 1,
            "UnitPrice": round(freight_unit_price_ex_tax, 4),
            "WarehouseCode": sap_wh_code,
            "TaxCode": tax_code,
            "TaxType": "tt_Yes",
            "TaxLiable": "tYES",
            "TaxTotal": round(compute_tax_from_line(freight_gross, 1, freight_tax_percent, tax_inclusive=True), 5),
            "BatchNumbers": [],
        }
        if line_udfs:
            freight_line.update(line_udfs)
        ship_gl = _resolve_gl_account_code(settings, channel, ship_state, tax_bucket, "shipping")
        if ship_gl:
            freight_line["AccountCode"] = ship_gl
        doc_lines.append(freight_line)

    series = _resolve_ar_invoice_series(settings, channel)

    payload = {
        "CardCode": customer_code,
        "DocDate": order_date,
        "DocDueDate": order_date,
        "Series": series,
        "GSTTransactionType": _resolve_gst_transaction_type(uc_data, total_tax_expected),
        "U_UCOrderID": uniware_id,
        "U_FrappeGRNKey": "3",
        "PayToCode": bill_to_code,
        "ShipToCode": ship_to_code,
        "Address": _compose_sap_address(uc_data),
        "DocumentLines": doc_lines,
        "Comments": f"Unicommerce Order: {uniware_id} | Channel: {channel}",
    }
    # Set on every channel going forward (not just AMAZON_FLEX) so
    # NumAtCard-based cross-checking (sap_client.find_invoice_by_num_at_card,
    # the same pattern amazon-us-b2c already dedups on) works universally
    # without needing periodic re-backfills -- the one-time backfill run
    # 2026-08-20 covered every invoice that existed at that moment (1277/
    # 1278), but had no ongoing mechanism, so newly-posted invoices from
    # every other channel kept drifting back to NumAtCard-unset. This does
    # NOT change any existing channel's actual duplicate-guard LOOKUP
    # (still U_Order-UDF/HANA -- proven, unchanged); it only ensures the
    # field itself is populated going forward. AMAZON_FLEX still leaves
    # uc_data["code"] unset (see amazon_flex_items.py / flex_fetch.py) so
    # its own NumAtCard-based lookup can't collide across a split order's
    # multiple shipments.
    payload["NumAtCard"] = uniware_id
    transport_code = _resolve_transportation_code(uc_data)
    if transport_code is not None:
        payload["TransportationCode"] = int(transport_code)

    return payload, round(total_tax_expected, 2)


def _allocate_return_batches(source_batches: list[dict], return_qty: float) -> list[dict]:
    """Split return_qty across the parent invoice line's batches, in the
    order they were posted (not re-sorted by expiry -- a return draws
    down whatever the original sale actually shipped from)."""
    qty_needed = round(float(return_qty or 0), 3)
    if qty_needed <= 0:
        return []
    normalized = []
    for row in source_batches or []:
        batch_no = normalize_text((row or {}).get("BatchNumber"))
        if not batch_no:
            continue
        try:
            batch_qty = float((row or {}).get("Quantity") or 0)
        except Exception:
            batch_qty = 0.0
        if batch_qty <= 0:
            continue
        normalized.append({"BatchNumber": batch_no, "Quantity": round(batch_qty, 3)})
    if not normalized:
        return []

    allocations = []
    remaining = qty_needed
    for row in normalized:
        if remaining <= 0:
            break
        take = min(float(row.get("Quantity") or 0), remaining)
        if take <= 0:
            continue
        allocations.append({"BatchNumber": row["BatchNumber"], "Quantity": round(float(take), 3)})
        remaining = round(remaining - float(take), 3)

    if remaining > 0 and allocations:
        # Return qty slightly exceeds the parent line's recorded batch qty
        # (rounding drift) -- absorb it into the last allocation rather
        # than reject the whole credit note over a fraction of a unit.
        allocations[-1]["Quantity"] = round(float(allocations[-1]["Quantity"]) + remaining, 3)
    return allocations


def _safe_json_loads(raw_value: Any) -> dict:
    if isinstance(raw_value, dict):
        return raw_value
    if not raw_value:
        return {}
    try:
        return json.loads(raw_value)
    except Exception:
        return {}


def credit_note_payload(cn: dict, parent_order: dict, settings: UnicommerceSettings) -> dict:
    """Build the SAP B1 Credit Note payload for one staged return.

    cn: a row from unicommerce_cn_staging (uc_return_id, uc_order_id,
    parent_sap_doc_entry, return_date, items) -- items already resolved
    by unicommerce.returns.prepare_credit_note_items (each has
    sapBaseLine/sapItemCode/sapAccountCode/sapBatchNumbers set).
    parent_order: the row from unicommerce_orders (channel_name, state).

    Ported from khanal_tech_integrations/unicommerce/mappers/sap_payload.py's
    credit_note_payload; frappe.throw -> raise ValueError, and the
    Frappe doc's live parent-order refetch is replaced by the parent_order
    dict the caller already loaded.
    """
    if not cn.get("parent_sap_doc_entry"):
        raise ValueError(f"Missing parent_sap_doc_entry for return {cn.get('uc_return_id')}")
    if not settings.credit_note_series:
        raise ValueError("credit_note_series is not configured")

    items = cn.get("items") or []
    if not items:
        raise ValueError(f"No return items found for return {cn.get('uc_return_id')}")

    parent_channel = (parent_order or {}).get("channel_name") or ""
    parent_ship_state = normalize_state_code((parent_order or {}).get("state") or "")
    parent_origin_state = resolve_origin_state_code(parent_channel, settings)
    parent_tax_bucket = classify_tax_bucket(parent_ship_state, None, parent_origin_state)

    lines = []
    for item in items:
        # sapItemCode (resolved by returns.prepare_credit_note_items against
        # the base invoice's own DocumentLines) must win over itemSku (the
        # raw UC seller SKU) -- they can legitimately differ (bundle SKUs,
        # channel-specific SKU codes), and SAP requires the credit note's
        # ItemCode to match the base invoice line's ItemCode exactly or
        # rejects with "Target item number does not match base item number".
        item_code = normalize_text(item.get("sapItemCode") or item.get("itemSku"))
        qty = float(item.get("quantity", 0) or 0)
        source_batches = item.get("sapBatchNumbers") or []
        source_account_code = normalize_text(item.get("sapAccountCode"))
        if item.get("sapBaseLine") in (None, ""):
            raise ValueError(f"Missing SAP base line for return item {item_code or 'unknown'} in {cn.get('uc_return_id')}")
        if not item_code:
            raise ValueError(f"Missing itemSku in return {cn.get('uc_return_id')}")
        if qty <= 0:
            raise ValueError(f"Invalid quantity for return item {item_code} in {cn.get('uc_return_id')}")
        if not source_batches:
            raise ValueError(f"Missing parent invoice batch mapping for return item {item_code} in {cn.get('uc_return_id')}")

        # Warehouse for return: FBA channels use their originating AMZ-* warehouse;
        # all other channels use the QA quarantine warehouse (EC-QA).
        item_uc_wh = normalize_text(item.get("warehouseCode"))
        channel_upper = parent_channel.strip().upper()
        if channel_upper in CHANNEL_WAREHOUSE_MAP:
            return_wh = CHANNEL_WAREHOUSE_MAP[channel_upper]
        else:
            return_wh = _resolve_sap_warehouse(settings, parent_channel, item_uc_wh) or RETURN_WAREHOUSE_CODE

        line = {
            "ItemCode": item_code,
            "Quantity": qty,
            "WarehouseCode": return_wh,
            "BaseType": 13,  # AR Invoice
            "BaseEntry": int(cn["parent_sap_doc_entry"]),
            "BaseLine": int(item.get("sapBaseLine", 0) or 0),
            "BatchNumbers": _allocate_return_batches(source_batches, qty),
        }
        if not line.get("BatchNumbers"):
            raise ValueError(f"Could not allocate return qty from parent invoice batches for {item_code} in {cn.get('uc_return_id')}")

        account_code = source_account_code
        if not account_code and parent_channel:
            account_code = _resolve_gl_account_code(
                settings=settings, channel=parent_channel, ship_state=parent_ship_state,
                tax_bucket=parent_tax_bucket, line_kind="product",
            ) or ""
        if account_code:
            line["AccountCode"] = account_code

        lines.append(line)

    cn_doc_date = str(cn.get("return_date") or date.today())
    # CardCode must match the base AR Invoice's own CardCode, not whatever
    # settings.channel_customer_map currently maps this channel to -- the
    # mapping can change after the invoice posted (channel reassigned to a
    # different SAP customer), and SAP requires an exact match or rejects
    # with "Base document card and target document card do not match".
    # The invoice's payload_json is the same source-of-truth pattern
    # returns.prepare_credit_note_items already uses for item codes/lines.
    invoice_doc = _safe_json_loads((parent_order or {}).get("payload_json"))
    card_code = normalize_text(invoice_doc.get("CardCode")) or resolve_customer_code(settings, parent_channel)
    return {
        "CardCode": card_code,
        "DocDate": cn_doc_date,
        "Series": int(settings.credit_note_series),
        "U_UCReturnID": cn.get("uc_return_id"),
        "DocumentLines": lines,
        "Comments": f"Return for UC Order: {cn.get('uc_order_id')}",
    }


# ── Shared actual-totals helpers ──────────────────────────────────────────
# Used by both post_invoices.py (right after a new post) and recon.py (the
# periodic re-verification sweep) so "what we call actual" can never drift
# between the two -- same DocumentLines shape either way (from a fresh
# post's response, or a later GET by DocEntry).

def sum_unit_price_total(lines: list[dict]) -> float:
    total = 0.0
    for line in lines or []:
        try:
            total += float(line.get("Quantity", 0) or 0) * float(line.get("UnitPrice", 0) or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def sum_tax_total(lines: list[dict]) -> float:
    total = 0.0
    for line in lines or []:
        for key in ("TaxTotal", "TaxAmount", "LineTaxTotal", "LineTaxAmount"):
            if key in line:
                try:
                    total += float(line.get(key) or 0)
                except Exception:
                    pass
                break
    return round(total, 2)
