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
from amazon_us.state_codes import normalize_us_state_code

# (item_code, qty, warehouse) -> list of SAP BatchNumbers dicts
BatchAllocator = Callable[[str, float, str], list[dict]]
# GL format code -> internal AcctCode
AcctResolver = Callable[[str], str]


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

    payload = {
        "CardCode": constants.SAP_CARD_CODE,
        "DocDate": doc_date,
        "DocDueDate": doc_date,
        "Series": constants.SAP_INVOICE_SERIES,
        "NumAtCard": order["amazon_order_id"],
        "PayToCode": constants.SAP_PAY_TO_CODE,
        "U_CreatedBy": constants.SAP_CREATED_BY,
        "DocumentLines": doc_lines,
    }
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
