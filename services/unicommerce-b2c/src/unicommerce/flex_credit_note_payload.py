"""Build the SAP B1 AR Credit Note payload for one Amazon Flex refund event.

Ported from amazon-us-b2c's credit_note_payload.py (same SAP install, same
proven WithoutInventoryMovement reversal pattern -- refunded FBA/Flex goods
never return to sellable stock, so the CN reverses revenue without a
physical stock receipt). Adapted for this service's own schema:

  - Every field that SAP requires to match the base AR Invoice exactly
    (ItemCode, TaxCode, WarehouseCode, CardCode) is read directly off the
    invoice's own stored payload_json DocumentLines/header, not
    re-derived -- same "trust what's actually on the base document"
    principle unicommerce.sap_payload.credit_note_payload already uses
    for CardCode (see its own comment on why re-deriving from
    settings.channel_customer_map is wrong once the mapping has drifted).
  - Quantity is INFERRED: the Finances API reports refunded money, not
    units. qty = abs(refunded principal) / unit price, where unit price
    comes from the ORIGINAL invoice's own order_json line
    (sellerSkuCode -> sellingPriceWithoutTaxesAndDiscount), not today's
    live pricing.
  - v1 scope: CFG (Sales BOM) bundle refunds are explicitly NOT handled --
    skipped with a clear reason rather than guessing a proportional
    reversal across bundle children without having verified it against
    real SAP TEST data. Flag for a v2 once real bundle-refund volume is
    observed.
  - v1 scope: nonzero shipping_amount/shipping_tax_amount on an
    adjustment is logged and skipped (not silently dropped) rather than
    posted through a GL account path this service has never proven --
    Flex delivery fees are baked into item price with no separate
    customer-facing shipping charge in every case observed so far, so
    this is expected to be rare/never, not a routine gap.
"""

from __future__ import annotations

import logging
from typing import Any

from unicommerce import amazon_flex_items as flex_items
from unicommerce.amazon_finances_api import RefundEvent

logger = logging.getLogger(__name__)

# Same precision floor as amazon-us-b2c's credit_note_payload.py: SAP
# stores Quantity at 3 decimals, our own math runs at 4-6 -- a remainder
# below SAP's own precision floor is rounding dust, not a real top-up
# amount, and posting it makes SAP reject the whole document with
# "Quantity cannot be zero".
_MIN_POSTABLE_QTY = 0.001


def _invoice_lines_by_item_code(invoice_doc: dict) -> dict[str, list[dict]]:
    by_item_code: dict[str, list[dict]] = {}
    for line in invoice_doc.get("DocumentLines") or []:
        code = str(line.get("ItemCode") or "").strip()
        if code:
            by_item_code.setdefault(code, []).append(line)
    return by_item_code


def _pop_matching_line(lines_by_item_code: dict[str, list[dict]], item_code: str) -> dict | None:
    lines = lines_by_item_code.get(item_code)
    return lines.pop(0) if lines else None


def _unit_price_by_seller_sku(order_json: dict) -> dict[str, float]:
    prices: dict[str, float] = {}
    for item in order_json.get("saleOrderItems") or []:
        sku = str(item.get("sellerSkuCode") or "").strip()
        if sku and sku not in prices:
            try:
                prices[sku] = float(item.get("sellingPriceWithoutTaxesAndDiscount") or 0)
            except (TypeError, ValueError):
                continue
    return prices


def build_flex_credit_note_payload(
    parent_order: dict,
    refund_event: RefundEvent,
    sku_map: dict[str, str],
    settings,
    already_posted: dict[str, Any] | None = None,
) -> tuple[dict | None, list[dict]]:
    """parent_order: one row from store.get_posted_flex_orders_by_amazon_order_id
    (uniware_id, order_json, payload_json, sap_ar_invoice_docentry) for the
    ONE shipment-invoice this event's adjustments actually belong to --
    caller has already disambiguated across a split order's multiple
    shipments before calling this.

    Returns (payload, skipped). payload is None when nothing is postable
    (every adjustment unmatched, BOM, or already fully covered).

    already_posted: {"qty_by_price": {unit_price: qty}} -- what a prior CN
    already reversed under this refund_id, keyed by UnitPrice (not
    item_code -- a SKU's mapping can drift after the base CN posted, same
    reasoning as amazon-us-b2c's post_credit_notes.py amendment path)."""
    already_posted = already_posted or {}
    already_qty_by_price: dict[float, float] = dict(already_posted.get("qty_by_price") or {})

    order_json = parent_order.get("order_json") or {}
    invoice_doc = parent_order.get("payload_json") or {}
    base_doc_entry = parent_order.get("sap_ar_invoice_docentry")
    uniware_id = parent_order.get("uniware_id")

    if not base_doc_entry:
        return None, [{"reason": f"Parent order {uniware_id} has no sap_ar_invoice_docentry"}]

    unit_price_by_sku = _unit_price_by_seller_sku(order_json)
    lines_by_item_code = _invoice_lines_by_item_code(invoice_doc)
    card_code = str(invoice_doc.get("CardCode") or "").strip()

    doc_lines: list[dict] = []
    skipped: list[dict] = []
    line_num = 0

    for adj in refund_event.item_adjustments:
        if adj.shipping_amount or adj.shipping_tax_amount:
            logger.warning(
                "flex_credit_note_payload(%s): seller_sku=%s carries shipping_amount=%s "
                "shipping_tax_amount=%s -- not reversed, no proven GL path for this yet",
                refund_event.refund_id, adj.seller_sku, adj.shipping_amount, adj.shipping_tax_amount,
            )

        try:
            item_code = flex_items.resolve_seller_sku(sku_map, settings, adj.seller_sku)
        except flex_items.SkuUnresolved:
            skipped.append({"seller_sku": adj.seller_sku, "reason": "Seller SKU not resolvable to a SAP item code"})
            continue

        if flex_items.is_cfg_parent(item_code):
            skipped.append(
                {
                    "seller_sku": adj.seller_sku, "item_code": item_code,
                    "reason": "CFG bundle refund reversal not implemented in v1 -- needs manual credit note",
                }
            )
            continue

        unit_price = unit_price_by_sku.get(adj.seller_sku)
        if not unit_price:
            skipped.append({"seller_sku": adj.seller_sku, "reason": "No unit price on parent order's own order_json"})
            continue

        qty = round(abs(adj.principal_amount) / unit_price, 4)
        price_key = round(unit_price, 4)
        if price_key in already_qty_by_price:
            # Draw down a pool, not a flat per-line subtraction -- an event
            # can carry more than one adjustment at the same unit price.
            already = already_qty_by_price[price_key]
            consume = min(qty, already)
            qty = round(qty - consume, 4)
            already_qty_by_price[price_key] = round(already - consume, 4)

        if qty < _MIN_POSTABLE_QTY:
            skipped.append(
                {
                    "seller_sku": adj.seller_sku,
                    "reason": f"Refund amount ({adj.principal_amount}) rounds to <{_MIN_POSTABLE_QTY} qty at unit price {unit_price}",
                }
            )
            continue

        invoice_line = _pop_matching_line(lines_by_item_code, item_code)
        if invoice_line is None:
            skipped.append({"seller_sku": adj.seller_sku, "item_code": item_code, "reason": "No matching invoice line found"})
            continue

        doc_lines.append(
            {
                "LineNum": line_num,
                "BaseType": 13,  # AR Invoice
                "BaseEntry": int(base_doc_entry),
                "BaseLine": invoice_line.get("LineNum"),
                "ItemCode": item_code,
                "Quantity": qty,
                "UnitPrice": unit_price,
                "WarehouseCode": invoice_line.get("WarehouseCode"),
                "TaxCode": invoice_line.get("TaxCode"),
                "TaxType": "tt_Yes",
                "TaxLiable": "tYES",
                "WithoutInventoryMovement": "tYES",
            }
        )
        line_num += 1

    if not doc_lines:
        return None, skipped

    payload = {
        "CardCode": card_code,
        "DocDate": refund_event.posted_date or None,
        "Series": int(settings.credit_note_series),
        "NumAtCard": refund_event.refund_id,
        "DocumentLines": doc_lines,
        "Comments": f"Amazon Flex refund for order {refund_event.amazon_order_id}",
    }
    return payload, skipped
