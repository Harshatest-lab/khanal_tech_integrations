"""Build the SAP B1 AR Invoice payload for one staged order. Pure —
stock allocation and GL resolution arrive as injected callables.

Business rules (Finance-mandated, validated by the June 2026 close):
  - PURE GROSS pricing: UnitPrice = item_price / qty; freight line =
    gross shipping. Promotions (item AND shipping) are NOT netted
    anywhere — Finance books promo spend separately, and the invoice
    total is expected to exceed the buyer's payment.
  - DocDate = actual_ship_date. The MTR indexes by shipment/settlement
    date, so ship date is what makes SAP and MTR land in the same month.
    NO ship date -> REFUSE to post (ship_date_missing) — never fall back
    to purchase date; that fallback put invoices in the wrong month.
  - Line UDFs are the verified INC INV1/RIN1 set (U_Order, U_OrderedOn,
    U_BuyerName, U_City, U_State, U_Country, U_PINCode). The Service
    Layer SILENTLY DROPS unknown properties — U_Channel/U_OrderID do not
    exist and must never be sent. U_State carries the USPS 2-letter code.
  - Tax rides as one EXP000003 line, shipping as EXP000004, both with
    HANA-resolved GL AcctCodes and the 0% 'etax0' tax code (every line
    needs an explicit TaxCode or SAP raises 'Row without tax was found').
"""

from __future__ import annotations

from typing import Any, Callable

from amazon_us import constants
from amazon_us.state_codes import US_TERRITORY_COUNTRY_CODES, normalize_us_state_code

# (item_code, qty, warehouse) -> list of SAP BatchNumbers dicts
BatchAllocator = Callable[[str, float, str], list[dict]]
# GL format code -> internal AcctCode
AcctResolver = Callable[[str], str]
# item_code -> [(child_item_code, qty_per_parent_unit), ...], [] if not a BOM
BomLookup = Callable[[str], list[tuple[str, float]]]


def build_line_udfs(order: dict[str, Any]) -> dict[str, str]:
    """Line-level UDFs for AR Invoice / Credit Note lines. Values are
    truncated to each UDF's EditSize or SAP rejects the document."""
    fields = [
        ("U_Order", order.get("amazon_order_id"), 50),
        ("U_BuyerName", order.get("buyer_name"), 120),
        ("U_City", order.get("ship_city"), 20),
        ("U_State", normalize_us_state_code(order.get("ship_state")), 20),
        ("U_PINCode", order.get("ship_postal_code"), 10),
        ("U_Country", order.get("ship_country"), 20),
    ]
    udfs = {name: str(value).strip()[:size] for name, value, size in fields if value}
    if order.get("purchase_date"):
        udfs["U_OrderedOn"] = str(order["purchase_date"])[:10]
    return udfs


def build_invoice_payload(
    order: dict[str, Any],
    item_mapping: dict[str, str],
    allocate_batches: BatchAllocator,
    resolve_acct: AcctResolver,
    get_bom_children: BomLookup,
) -> tuple[dict | None, list[dict]]:
    """Returns (payload, skipped). payload is None when the order cannot
    be posted; skipped explains why per line (or carries a single
    order-level reason such as ship_date_missing).

    HanaUnavailableError / InsufficientStockError propagate from
    allocate_batches — the caller decides staging consequences.
    """
    ship_date = order.get("actual_ship_date")
    if not ship_date:
        return None, [{"reason": "ship_date_missing"}]
    doc_date = str(ship_date)[:10]

    line_udfs = build_line_udfs(order)

    doc_lines: list[dict] = []
    skipped: list[dict] = []
    total_tax = 0.0
    total_shipping = 0.0

    for row in order.get("lines", []):
        sku = (row.get("seller_sku") or "").strip()
        qty = float(row.get("quantity") or 0)
        if not sku or qty <= 0:
            continue

        total_tax += float(row.get("item_tax_amount") or 0) + float(row.get("shipping_tax_amount") or 0)
        total_shipping += float(row.get("shipping_price_amount") or 0)

        item_code = item_mapping.get(sku)
        if not item_code:
            skipped.append({"seller_sku": sku, "reason": "No SAP mapping"})
            continue

        children = get_bom_children(item_code)
        if children:
            # Sales BOM (combo/bundle) item: SAP explodes the parent into
            # its components at document-add time and rejects the whole
            # document if a batch-managed component has no batch selected
            # ("Cannot add row without complete selection of batch/serial
            # numbers") — merging children's batches onto the parent's own
            # line does NOT satisfy this; SAP needs a real line per
            # component. Ported from unicommerce-b2c's bundle handling:
            # zero-price/zero-batch "iSalesTree" parent line, then one
            # "iIngredient" line per component carrying the real price
            # share, quantity, and its own FEFO batch allocation.
            doc_lines.append(
                {
                    "ItemCode": item_code,
                    "Quantity": qty,
                    "UnitPrice": 0.0,
                    "WarehouseCode": constants.SAP_WAREHOUSE,
                    "TaxCode": constants.SAP_TAX_CODE,
                    "TaxType": "tt_Yes",
                    "TaxLiable": "tYES",
                    "TaxTotal": 0.0,
                    "TreeType": "iSalesTree",
                    **line_udfs,
                }
            )
            item_price = float(row.get("unit_price") or 0) * qty
            n = len(children)
            allocated_price = 0.0
            for i, (child_code, qty_per_unit) in enumerate(children):
                child_qty = qty_per_unit * qty
                if child_qty <= 0:
                    continue
                # Amazon gives one price for the whole bundle, not a
                # per-component breakdown — split evenly across components,
                # last one absorbing the rounding remainder so the sum
                # matches item_price exactly (MTR reconciliation needs the
                # invoice total to match, not the per-component split).
                share = round(item_price / n, 2) if i < n - 1 else round(item_price - allocated_price, 2)
                allocated_price += share
                child_batches = allocate_batches(child_code, child_qty, constants.SAP_WAREHOUSE)
                child_line = {
                    "ItemCode": child_code,
                    "Quantity": child_qty,
                    "UnitPrice": round(share / child_qty, 4),
                    "WarehouseCode": constants.SAP_WAREHOUSE,
                    "TaxCode": constants.SAP_TAX_CODE,
                    "TaxType": "tt_Yes",
                    "TaxLiable": "tYES",
                    "TreeType": "iIngredient",
                    **line_udfs,
                }
                if child_batches:
                    child_line["BatchNumbers"] = child_batches
                doc_lines.append(child_line)
            continue

        batches = allocate_batches(item_code, qty, constants.SAP_WAREHOUSE)

        line = {
            "ItemCode": item_code,
            "Quantity": qty,
            "UnitPrice": float(row.get("unit_price") or 0),  # GROSS
            "WarehouseCode": constants.SAP_WAREHOUSE,
            "TaxCode": constants.SAP_TAX_CODE,
            **line_udfs,
        }
        if batches:
            line["BatchNumbers"] = batches
        doc_lines.append(line)

    if not doc_lines or skipped:
        # Partial posting is forbidden: an invoice missing one line can
        # never reconcile against the MTR. All lines or nothing.
        return None, (skipped or [{"reason": "No postable lines"}])

    if total_tax > 0:
        doc_lines.append(
            _gl_line(constants.SAP_TAX_ITEM_CODE, total_tax, resolve_acct(constants.SAP_TAX_GL_FORMAT), line_udfs)
        )
    if total_shipping > 0:
        doc_lines.append(
            _gl_line(
                constants.SAP_SHIPPING_ITEM_CODE,
                total_shipping,
                resolve_acct(constants.SAP_SHIPPING_GL_FORMAT),
                line_udfs,
            )
        )

    # Per-state Ship-To/Pay-To address code (same idea as the Walmart
    # integration's order_shipping_state, different naming convention on
    # this customer master — see SAP_SHIP_TO_STATE_PREFIX). Falls back to
    # the generic SAP_PAY_TO_CODE default when the order has no state OR
    # ships outside the US — a 2-letter ISO country (e.g. "CA" Canada,
    # "CO" Colombia) collides with real USPS state codes (California,
    # Colorado), so it must never be smuggled into the state-code prefix.
    # The real destination country instead rides AddressExtension below,
    # which SAP applies as a per-document override independent of
    # whatever the referenced address code's own country is.
    #
    # US territories (Puerto Rico, Guam, etc.) are the one exception: Amazon
    # reports them with their own ship_country ("PR", not "US"), but they
    # have real, dedicated, non-colliding SAP codes (confirmed live:
    # "Amazon USA-PR") — treat them as domestic instead of routing to the
    # generic fallback, which has no matching SAP address at all.
    country = (order.get("ship_country") or "").strip().upper()
    is_domestic = country in ("", "US", "USA") or country in US_TERRITORY_COUNTRY_CODES
    if is_domestic:
        state_code = normalize_us_state_code(order.get("ship_state"))
        ship_to_code = f"{constants.SAP_SHIP_TO_STATE_PREFIX}{state_code}" if state_code else constants.SAP_PAY_TO_CODE
    else:
        ship_to_code = constants.SAP_PAY_TO_CODE

    payload = {
        "CardCode": constants.SAP_CARD_CODE,
        "DocDate": doc_date,
        "DocDueDate": doc_date,
        "Series": constants.SAP_INVOICE_SERIES,
        "NumAtCard": order["amazon_order_id"],
        "PayToCode": ship_to_code,
        "ShipToCode": ship_to_code,
        "U_CreatedBy": constants.SAP_CREATED_BY,
        "DocumentLines": doc_lines,
    }
    if not is_domestic:
        payload["AddressExtension"] = {"ShipToCountry": country, "BillToCountry": country}
    return payload, []


def _gl_line(item_code: str, amount: float, acct_code: str, udfs: dict) -> dict:
    return {
        "ItemCode": item_code,
        "Quantity": 1,
        "UnitPrice": round(amount, 2),
        "WarehouseCode": constants.SAP_WAREHOUSE,
        "TaxCode": constants.SAP_TAX_CODE,
        "AccountCode": acct_code,
        **udfs,
    }
