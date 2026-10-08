"""Build the SAP B1 AR Invoice payload for one shipped Amazon UK order.

Line structure — every line carries an explicit AccountCode per the GL
mapping table (Item Sale/Shipping/Promo/Gift Wrap), rather than relying on
SAP's own G/L determination. This previously failed (10000115 "choose
sales-type revenue account") because AMZ-FBA's Warehouses > Accounting tab
had no sales-type revenue account configured — SAP requires that baseline
before it accepts any line-level AccountCode override in a warehouse. That
warehouse setup has since been done in SAP.
    Item Sale             one line per product — AccountCode 400000.
    Shipping Charges       EXP0001, AccountCode 400010 — one combined
                           line, netted (shipping price minus shipping
                           discount), positive only; dropped entirely if
                           the discount fully offsets the charge.
    Promotional Discount   EXP0002, AccountCode 650106 — item discount
                           only, as its own positive-amount line, same
                           VatGroup as the item line it belongs to
                           (shipping discount goes to the Shipping Charges
                           line above instead, not here).
    Gift Wrap Credits      EXP0003, AccountCode 420000 — never seen in
                           real data (0 of 1125 items) but wired now that
                           the item exists; uses its own tax breakdown
                           from Amazon.
    Tax                    handled automatically via VatGroup: O1 (20%),
                           O2 (0%), O4 (23%, IOSS/Ireland) — all three
                           confirmed live in UK_TEST, posting to 207000
                           (O1/O2) or 207001 (O4).

Every amount comes straight from Amazon's proceeds.breakdowns on the raw
order item — NOT the already-flattened DB columns, which only keep
combined item+shipping totals. TAX and DISCOUNT breakdowns both split into
ITEM/SHIPPING detailedBreakdowns (confirmed against real data).

DocDate = shipment_date (ship date). DocDueDate/TaxDate = purchase_date
(order date). An order with no shipment_date yet is refused here rather
than guessed.
"""

from amazon_uk import store
from amazon_uk import hana_client
from amazon_uk.hana_client import allocate_batches_fefo

# Confirmed live against UK_TEST's VatGroups (2026-09-11): O1=20%, O2=0%,
# O4=23% (IE Std rate, IOSS/Ireland) — all three post to their configured
# TaxAccount (207000 for O1/O2, 207001 for O4).
TAX_CODE_BY_RATE = {0: "O2", 20: "O1", 23: "O4"}

# SAP requires a real ItemCode on every line, even non-inventory G/L-only
# ones — confirmed live (-5002 "Item number is missing" when a line had
# only AccountCode). All three exist in UK_TEST (confirmed 2026-09-11).
# NOTE: EXP00001 (extra zero) also exists, same name "Shipping Charges" —
# a duplicate item, not used; EXP0001 is the one to post to.
ITEM_CODE_SHIPPING = "EXP0001"
ITEM_CODE_PROMO_DISCOUNT = "EXP0002"
ITEM_CODE_GIFT_WRAP = "EXP0003"

# GL account mapping table — passed explicitly as AccountCode on every
# line now that AMZ-FBA's Warehouses > Accounting tab has a sales-type
# revenue account configured (previously required for any override).
ACCOUNT_ITEM_SALE = "400000"
ACCOUNT_SHIPPING = "400010"
ACCOUNT_PROMO_DISCOUNT = "650106"
ACCOUNT_GIFT_WRAP = "420000"


def get_sku_mapping() -> dict:
    """{seller_sku: sap_item_code} for active mappings."""
    conn = store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT seller_sku, sap_item_code FROM amazonuk.sku_item_map WHERE is_active = true")
            return dict(cur.fetchall())
    finally:
        conn.close()


def _cache_sku_mapping(seller_sku: str, sap_item_code: str, item_description: str) -> None:
    """Write a mapping discovered live via hana_client.find_item_by_amz_sku
    back into amazonuk.sku_item_map, so the next order for this SKU hits
    the RDS cache instead of HANA again. Best-effort — a write failure here
    must never fail the invoice actually being posted."""
    try:
        conn = store.connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO amazonuk.sku_item_map (seller_sku, sap_item_code, item_description, is_active)
                        VALUES (%s, %s, %s, true)
                        ON CONFLICT (seller_sku) DO UPDATE SET
                            sap_item_code = EXCLUDED.sap_item_code,
                            item_description = EXCLUDED.item_description,
                            is_active = true
                        """,
                        (seller_sku, sap_item_code, item_description),
                    )
        finally:
            conn.close()
    except Exception:
        pass


def _extract_breakdowns(raw_order_item: dict) -> dict:
    """Item/shipping/discount/gift-wrap amounts + their own tax, split by
    Amazon's detailedBreakdowns subtype (ITEM vs SHIPPING vs GIFT_WRAP)."""
    breakdowns = (raw_order_item.get("proceeds") or {}).get("breakdowns", [])
    result = {
        "item_price": 0.0, "item_tax": 0.0,
        "shipping_price": 0.0, "shipping_tax": 0.0,
        "item_discount": 0.0, "shipping_discount": 0.0,
        "gift_wrap_price": 0.0, "gift_wrap_tax": 0.0,
    }
    for b in breakdowns:
        amount = float(b.get("subtotal", {}).get("amount") or 0)
        btype = b.get("type")
        if btype == "ITEM":
            result["item_price"] = amount
        elif btype == "SHIPPING":
            result["shipping_price"] = amount
        elif btype == "GIFT_WRAP":
            result["gift_wrap_price"] = amount
        elif btype in ("TAX", "DISCOUNT"):
            detail = {
                d.get("subtype"): float(d.get("value", {}).get("amount") or 0)
                for d in b.get("detailedBreakdowns", [])
            }
            if btype == "TAX":
                result["item_tax"] = detail.get("ITEM", amount if not detail else 0.0)
                result["shipping_tax"] = detail.get("SHIPPING", 0.0)
                result["gift_wrap_tax"] = detail.get("GIFT_WRAP", 0.0)
            else:
                result["item_discount"] = detail.get("ITEM", amount if not detail else 0.0)
                result["shipping_discount"] = detail.get("SHIPPING", 0.0)
    return result


def _rate(tax_amount: float, base_amount: float) -> int:
    """base_amount must be the taxable base Amazon actually applied tax to
    (post-discount), not the pre-discount price — confirmed live: dividing
    by the pre-discount item_price on order 203-1436547-6306717 gave 17%
    instead of the true 20% (tax was computed on item_price minus
    item_discount). Rounded to the nearest whole percent: real amounts are
    penny-rounded, so even the correct base rarely divides out to a
    perfectly clean e.g. 20.00 (that same order: 1.98/9.91*100 = 19.98,
    not 20.00) — whole-percent rounding absorbs that penny-level noise.
    _vat_group below then requires an EXACT match on that whole percent;
    it no longer snaps to whichever configured rate happens to be nearest.
    """
    return round(tax_amount / base_amount * 100) if base_amount else 0


def _vat_group(rate: int) -> str:
    """Exact match only — no more snapping to the 'nearest' configured
    rate. Nearest-match could silently pick the wrong VatGroup (e.g. a
    heavy discount pushing the computed rate low enough to snap to 0%
    when the real rate was 20%) with no error to catch it. A rate that
    doesn't exactly match a configured VatGroup is a real data problem
    (wrong base, a genuinely new tax rate, bad breakdown data) that needs
    surfacing, not silently guessed at — same hard-stop-over-guess rule as
    an unmapped SKU.
    """
    if rate not in TAX_CODE_BY_RATE:
        raise ValueError(
            f"No VatGroup configured for a {rate}% tax rate — configured rates are "
            f"{sorted(TAX_CODE_BY_RATE)}. Check the underlying breakdown amounts."
        )
    return TAX_CODE_BY_RATE[rate]


def _service_line(item_code: str, amount: float, vat_group: str, warehouse: str, account_code: str,
                   quantity: int = 1) -> dict:
    """A non-inventory G/L line (shipping/discount/gift-wrap). SAP requires a
    real ItemCode even for these — confirmed live (-5002 'Item number is
    missing' when a line had only AccountCode, no ItemCode).

    WarehouseCode is required even here — confirmed live (1980000017
    "Warehouse 01 is not active"): without it, SAP falls back to its own
    system-default warehouse ("01"), which is inactive in this company.

    AccountCode is passed explicitly per the GL mapping table — requires
    AMZ-FBA's Warehouses > Accounting tab to have a sales-type revenue
    account configured (without it: 10000115 "choose sales-type revenue
    account").

    quantity=-1 (promo discount line only) makes LineTotal = Quantity *
    UnitPrice negative, actually reducing DocTotal — UnitPrice itself stays
    positive per explicit instruction (no negative values on the price).
    """
    return {
        "ItemCode": item_code,
        "Quantity": quantity,
        "UnitPrice": amount,
        "WarehouseCode": warehouse,
        "VatGroup": vat_group,
        "AccountCode": account_code,
    }


def _lines_for_row(row: dict, sap_item_code: str, warehouse: str, hana_cursor) -> list[dict]:
    b = _extract_breakdowns(row.get("raw_order_item") or {})
    qty = float(row.get("quantity_ordered") or 0)
    lines = []

    item_rate = _rate(b["item_tax"], b["item_price"] - b["item_discount"])
    item_vat_group = _vat_group(item_rate)
    unit_price = round(b["item_price"] / qty, 5) if qty else 0.0
    batches = allocate_batches_fefo(hana_cursor, sap_item_code, qty, warehouse)
    item_line = {
        "ItemCode": sap_item_code,
        "Quantity": qty,
        "UnitPrice": unit_price,
        "WarehouseCode": warehouse,
        "VatGroup": item_vat_group,
        "AccountCode": ACCOUNT_ITEM_SALE,
    }
    if batches:
        item_line["BatchNumbers"] = batches
    lines.append(item_line)

    if b["item_discount"] > 0:
        # Separate G/L line (Promotional Discount, 650106) rather than SAP's
        # DiscountPercent field — same VatGroup as the item line it belongs
        # to. UnitPrice stays positive (per explicit instruction); Quantity
        # -1 is what actually makes this line reduce DocTotal instead of
        # adding to it.
        lines.append(_service_line(ITEM_CODE_PROMO_DISCOUNT, b["item_discount"], item_vat_group, warehouse,
                                    ACCOUNT_PROMO_DISCOUNT, quantity=-1))

    shipping_net = round(b["shipping_price"] - b["shipping_discount"], 2)
    if shipping_net > 0:
        # One combined line, netted (not summed) — a shipping discount is a
        # reduction of the charge, not additional revenue. Summing the two
        # overstated the invoice by 2x the discount whenever it fully or
        # mostly offset the shipping charge (confirmed live: DocTotal 20.64
        # vs Amazon's real 18.99 on an order with an 0.83 charge / 0.83
        # discount that nets to zero). Line dropped entirely when the
        # discount fully offsets the charge (net <= 0) — nothing to invoice.
        #
        # VatGroup reuses the item line's rate (item_vat_group) rather than
        # independently deriving shipping's own rate via tax/base division.
        # UK VAT law taxes shipping as an ancillary supply at the same rate
        # as the goods it accompanies, so there was never a second real rate
        # to detect here — the amount itself (shipping_net) still comes
        # straight from Amazon, unchanged. Confirmed live 2026-09-17: two
        # real orders (205-3552490-1553139, 204-0496663-1950743) failed with
        # "No VatGroup configured" for 19%/21% purely because dividing two
        # penny-rounded amounts (shipping_tax/shipping_net, both under £1.50)
        # is numerically unstable — a single penny of rounding swings the
        # derived rate a full percentage point or more — even though both
        # orders' item lines correctly derived 20% from the same order.
        lines.append(_service_line(ITEM_CODE_SHIPPING, shipping_net, item_vat_group, warehouse,
                                    ACCOUNT_SHIPPING))

    if b["gift_wrap_price"] > 0:
        # Same reasoning as shipping above — gift wrap is an ancillary
        # charge on the same sale, taxed at the item's rate, not derived
        # independently. Never seen in real data (0 of 1125+ items).
        lines.append(_service_line(ITEM_CODE_GIFT_WRAP, b["gift_wrap_price"], item_vat_group, warehouse,
                                    ACCOUNT_GIFT_WRAP))
    return lines


def _tax_collection_method(order_rows: list[dict]) -> str:
    """'1' = Marketplace Facilitator (Amazon collected the VAT),
    '2' = Seller. Decided from the items' raw tax.taxCollections[].model:
    if ANY line shows MARKETPLACE_FACILITATOR the whole invoice is '1';
    absent block or any other model -> '2'. Confirmed against August data:
    every item that carries the block is MARKETPLACE_FACILITATOR, so all
    currently postable orders resolve to '1'. String values per SAP's UDF
    definition (confirmed 2026-09-16, not numeric).
    """
    for row in order_rows:
        item = row.get("raw_order_item") or {}
        for tc in ((item.get("tax") or {}).get("taxCollections") or []):
            if tc.get("model") == "MARKETPLACE_FACILITATOR":
                return "1"
    return "2"


def _full_ship_address(order_rows: list[dict]) -> str:
    """Street-level address only exists in the shipment report (Orders API's
    ShippingAddress has no street lines) — pull it from whichever item's
    raw_report_row has it first."""
    for row in order_rows:
        report = row.get("raw_report_row")
        if not report:
            continue
        parts = [
            report.get("ship-address-1"), report.get("ship-address-2"), report.get("ship-address-3"),
            report.get("ship-city"), report.get("ship-state"),
            report.get("ship-postal-code"), report.get("ship-country"),
        ]
        joined = ", ".join(p for p in parts if p and p != "--")
        if joined:
            return joined
    return ""


def build_invoice_payload(order_rows: list[dict], hana_cursor, warehouse: str, card_code: str, series: int,
                           sku_map: dict):
    """order_rows: every amazonuk.order_items row for one amazon_order_id.
    sku_map: {seller_sku: sap_item_code}, from get_sku_mapping() — fetched
    once per batch by the caller, not per order (it doesn't change mid-run).

    Returns (payload, skipped). payload is None if there's nothing postable.
    Raises HanaUnavailableError — caller must NOT mark the order failed.
    Raises ValueError on insufficient FEFO stock or any unmapped SKU (hard
    stop, same rule as the Amazon US pipeline after a real incident there:
    a skipped/unmapped line must never silently fall through to a partial
    invoice).
    """
    first = order_rows[0]
    ship_date = first.get("shipment_date")
    if not ship_date:
        return None, [{"reason": "No shipment_date yet — order not confirmed shipped"}]
    order_date = str(first.get("purchase_date"))[:10]
    doc_date = str(ship_date)[:10]

    doc_lines = []
    skipped = []
    for row in order_rows:
        sku = (row.get("seller_sku") or "").strip()
        qty = float(row.get("quantity_ordered") or 0)
        if not sku or qty <= 0:
            continue

        sap_item_code = sku_map.get(sku)
        if not sap_item_code:
            # Not in the RDS cache — fall back to a live lookup against
            # SAP's item master (OITM."U_Amz_SKU") before giving up. A hit
            # is written back into sku_item_map AND sku_map (the in-memory
            # dict, shared across every order in this batch) so later
            # orders in the same run — and every future run — hit the RDS
            # cache instead of HANA again.
            found = hana_client.find_item_by_amz_sku(hana_cursor, sku)
            if found:
                sap_item_code = found["sap_item_code"]
                sku_map[sku] = sap_item_code
                _cache_sku_mapping(sku, sap_item_code, found["item_description"])
            else:
                skipped.append(sku)
                continue

        doc_lines.extend(_lines_for_row(row, sap_item_code, warehouse, hana_cursor))

    if skipped:
        raise ValueError(
            f"No SAP mapping for SKU(s): {', '.join(skipped)} — "
            "not in amazonuk.sku_item_map or SAP's item master (OITM.U_Amz_SKU); "
            "add to amazonuk.sku_item_map by hand and retry"
        )
    if not doc_lines:
        return None, skipped

    payload = {
        "CardCode": card_code,
        "DocDate": doc_date,
        "DocDueDate": order_date,
        "TaxDate": order_date,
        "Series": series,
        "NumAtCard": first["amazon_order_id"],
        "PayToCode": first.get("ship_country") or "",
        "U_TaxClctnMthd": _tax_collection_method(order_rows),
        "Address": _full_ship_address(order_rows),
        "Comments": f'{first["amazon_order_id"]} - Amazon API',
        "DocumentLines": doc_lines,
    }
    return payload, skipped
