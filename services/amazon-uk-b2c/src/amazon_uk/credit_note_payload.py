"""Build the SAP B1 AR Credit Memo payload for one RELEASED refund transaction.

Every line is created via SAP's "Copy From" mechanism (BaseType/BaseEntry/
BaseLine pointing at the original AR Invoice), so the credit memo shows up
as a proper "Based On" reversal in SAP's document flow (A/R aging, the
invoice's "Open Amount" reduces correctly) instead of a floating unlinked
document. ItemCode/UnitPrice/VatGroup/AccountCode are ALSO set explicitly
on every line (copied straight from the matched invoice line) rather than
relying on SAP to infer them purely from BaseEntry/BaseLine — this data is
already sitting on the fetched invoice line; avoids trusting inheritance
behaviour that was never actually verified live for CreditNotes.

WarehouseCode and BatchNumbers ARE passed on the product line (mirroring
the same batch(es) the original invoice line shipped from — same
traceability as the sale itself), but that line also carries
"WithoutInventoryMovement": "tYES" (line-level, not header) so SAP does
NOT actually move stock — this is a financial-only reversal that still
shows batch/warehouse detail for reference. Not set on the shipping/promo
service lines below since those are non-inventory EXP items already
(never had BatchNumbers on the invoice either) — nothing there to prevent
moving.

A RELEASED refund transaction in the Finances API IS the completion
signal — full stop. This module never checks, and is not meant to check,
whether the item was ever physically returned (the FBA Customer Returns
report is a separate, unrelated concept). Money-back and item-back are
two independent things Amazon tracks; this pipeline only acts on the
first. A real physical return that should put stock back is a separate
manual stock-in, not something this module waits on or infers.

Shipping/promo also get their own copied-from line, same as
invoice_payload.py posts them (EXP0001/EXP0002) — each refund_items row's
raw `breakdowns` JSON (untouched by the charges_total/refunded_amount
rollup in store.py) is checked for a Shipping or PromoRebates entry, and
if present + the invoice actually has that EXP-line, a second doc_line
copies the FULL base line (its own recorded Quantity, whatever sign it
was posted with) rather than trying to prorate a partial shipping/promo
refund — there's no reliable amount-based partial-copy field confirmed
live yet. Gift wrap is NOT wired the same way: invoice_payload.py itself
has never seen one in real data, so there's no confirmed Finances-API
breakdownType name to key off — would be guessing a key that might not
match anything.
"""

from amazon_uk.invoice_payload import ITEM_CODE_GIFT_WRAP, ITEM_CODE_PROMO_DISCOUNT, ITEM_CODE_SHIPPING

CREDIT_MEMO_BASE_TYPE_INVOICES = 13  # standard SAP B1 oInvoices constant 

_SERVICE_ITEM_CODES = {ITEM_CODE_SHIPPING, ITEM_CODE_PROMO_DISCOUNT, ITEM_CODE_GIFT_WRAP}


def _breakdown_amount(breakdowns: list[dict], breakdown_type: str) -> float:
    return sum((b.get("breakdownAmount") or {}).get("currencyAmount") or 0
               for b in (breakdowns or []) if b.get("breakdownType") == breakdown_type)


def _nested_breakdown_amount(breakdowns: list[dict], parent_type: str, child_type: str) -> float:
    """PromoRebates isn't flat — it nests into typed children (confirmed
    live: ShippingDiscount, OurPriceDiscount). Collapsing it to one number
    conflates a shipping discount (never gets its own invoice line —
    invoice_payload.py always nets it straight into EXP0001's amount) with
    a genuine item discount (which DOES get its own EXP0002 line). Needed
    to tell "nothing to credit, already netted at invoice time" apart from
    "real money with no line to post it to"."""
    return sum(
        (c.get("breakdownAmount") or {}).get("currencyAmount") or 0
        for b in (breakdowns or []) if b.get("breakdownType") == parent_type
        for c in (b.get("breakdowns") or []) if c.get("breakdownType") == child_type
    )


def _index_invoice_lines(invoice: dict) -> tuple[dict[str, dict], dict[str, dict[str, dict]]]:
    """(product_lines, service_lines_by_product).

    product_lines: {ItemCode: DocumentLines entry} — real product SKUs are
    genuinely unique per invoice, so first-match-is-only-match holds here.

    service_lines_by_product: {product_ItemCode: {EXP-ItemCode: line}} —
    EXP0001/EXP0002/EXP0003 item codes are SHARED across every order row
    that has one (invoice_payload.py can emit a separate EXP0001 for EACH
    item on a multi-item order), so a flat {ItemCode: line} lookup for
    those collapses multiple real lines down to whichever came first —
    silently matching a refund's shipping/promo component against the
    WRONG product's line. This instead relies on invoice_payload.py's own
    guaranteed build order — build_invoice_payload appends one row's
    [product, promo?, shipping?, giftwrap?] lines fully before starting the
    next row — to tie each service line to whichever product line
    immediately precedes it in DocumentLines.
    """
    product_lines: dict[str, dict] = {}
    service_lines_by_product: dict[str, dict[str, dict]] = {}
    current_product_code = None
    for line in invoice.get("DocumentLines") or []:
        code = line.get("ItemCode")
        if not code:
            continue
        if code in _SERVICE_ITEM_CODES:
            if current_product_code is not None:
                service_lines_by_product.setdefault(current_product_code, {})[code] = line
        else:
            current_product_code = code
            if code not in product_lines:
                product_lines[code] = line
    return product_lines, service_lines_by_product


def _matching_batches(original_batches: list[dict], qty_needed: float) -> tuple[list[dict], str | None]:
    """Consume the original invoice line's BatchNumbers (same FEFO order
    they were allocated in) up to qty_needed, capping each batch's
    contribution at what was actually allocated to it on the invoice —
    crediting back exactly the batch(es) that shipment came from. Returns
    (batches, warning) — warning is set (non-fatal) if qty_needed exceeds
    what the invoice line's batches account for, which shouldn't happen
    since a refund can't exceed what was originally invoiced."""
    consumed = []
    remaining = qty_needed
    for b in original_batches or []:
        if remaining <= 0:
            break
        take = min(remaining, float(b.get("Quantity") or 0))
        if take <= 0:
            continue
        consumed.append({"BatchNumber": b["BatchNumber"], "Quantity": take})
        remaining -= take

    warning = None
    if round(remaining, 3) > 0:
        warning = (f"refund quantity {qty_needed} exceeds the {qty_needed - remaining} covered by "
                   f"the original invoice line's batches — check for a data mismatch")
    return consumed, warning


def build_credit_note_payload(invoice: dict, refund_line_rows: list[dict], amazon_order_id: str,
                               transaction_id: str, posted_date: str, series: int,
                               sku_map: dict) -> tuple[dict | None, list[dict]]:
    """invoice: full SAP Invoice object (sap_client.get_invoice(doc_entry))
    for this order — supplies CardCode and the BaseLine numbers to copy
    from.
    refund_line_rows: this transaction's amazonuk.refund_items rows.
    sku_map: {seller_sku: sap_item_code}, from invoice_payload.get_sku_mapping().

    Returns (payload, skipped). payload is None if no refund line matched a
    line on the original invoice (nothing postable). skipped entries explain
    why a given refund line was dropped — same hard-visibility rule as
    invoice_payload's unmapped-SKU skip list, but non-fatal here: a refund
    for a promo/shipping adjustment (which never gets its own product line
    on the invoice) is expected to skip, not a hard stop.
    """
    product_lines, service_lines_by_product = _index_invoice_lines(invoice)

    doc_lines = []
    skipped = []
    copied_service_lines = set()  # BaseLine numbers already copied — guards against
                                   # double-copying the same EXP-line if it's somehow
                                   # reached twice (shouldn't happen now each product's
                                   # service lines are looked up under its own ItemCode,
                                   # but kept as a safety net)
    for row in refund_line_rows:
        sku = (row.get("seller_sku") or "").strip()
        qty = row.get("quantity_refunded")
        if not sku or not qty:
            skipped.append({"sku": sku, "reason": "no seller_sku or zero/null quantity_refunded"})
            continue

        sap_item_code = sku_map.get(sku)
        invoice_line = product_lines.get(sap_item_code) if sap_item_code else None
        own_service_lines = service_lines_by_product.get(sap_item_code, {}) if sap_item_code else {}

        if not sap_item_code:
            skipped.append({"sku": sku, "reason": "no SAP mapping in amazonuk.sku_item_map"})
        elif invoice_line is None:
            skipped.append({"sku": sku, "reason":
                             f"SAP item {sap_item_code} not found on original invoice "
                             f"DocEntry {invoice.get('DocEntry')} — data mismatch, check manually"})
        else:
            doc_line = {
                "LineNum": len(doc_lines),
                "BaseType": CREDIT_MEMO_BASE_TYPE_INVOICES,
                "BaseEntry": invoice["DocEntry"],
                "BaseLine": invoice_line["LineNum"],
                "ItemCode": sap_item_code,
                "Quantity": qty,
                "UnitPrice": invoice_line.get("UnitPrice"),
                "WarehouseCode": invoice_line.get("WarehouseCode"),
                "VatGroup": invoice_line.get("VatGroup"),
                "AccountCode": invoice_line.get("AccountCode"),
                "WithoutInventoryMovement": "tYES",
            }
            original_batches = invoice_line.get("BatchNumbers")
            if original_batches:
                batches, warning = _matching_batches(original_batches, qty)
                doc_line["BatchNumbers"] = batches
                if warning:
                    skipped.append({"sku": sku, "reason": warning, "posted_anyway": True})
            doc_lines.append(doc_line)

        # Shipping/promo are properties of this same refund line, not a
        # separate row — checked regardless of whether the product line
        # above matched, same as invoice_payload.py builds them alongside
        # (not instead of) the product line.
        #
        # invoice_payload.py NEVER gives a shipping discount its own line —
        # it's always netted straight into EXP0001's amount (shipping_price
        # - shipping_discount). So a shipping discount must be folded into
        # the Shipping amount here too, not compared against EXP0002 (which
        # only ever holds genuine ITEM discounts) — see
        # _nested_breakdown_amount. Get this wrong and a shipping charge
        # partially offset by its own discount either double-checks a real
        # EXP0001 amount against the wrong line, or (when the two happen to
        # cancel exactly) throws a false "no line to copy from" warning for
        # money that was never invoiced in the first place.
        breakdowns = row.get("breakdowns")
        shipping_net = (_breakdown_amount(breakdowns, "Shipping")
                         + _nested_breakdown_amount(breakdowns, "PromoRebates", "ShippingDiscount"))
        item_discount = _nested_breakdown_amount(breakdowns, "PromoRebates", "OurPriceDiscount")

        for label, amount, service_item_code in (
            ("Shipping", shipping_net, ITEM_CODE_SHIPPING),
            ("PromoRebates", item_discount, ITEM_CODE_PROMO_DISCOUNT),
        ):
            if abs(amount) < 0.005:
                continue
            service_line = own_service_lines.get(service_item_code)
            if service_line is None:
                skipped.append({"sku": sku, "reason":
                                 f"refund includes a {label} component ({amount}) but this SKU's own "
                                 f"invoice line (DocEntry {invoice.get('DocEntry')}) has no {service_item_code} "
                                 f"line right after it to copy from"})
                continue
            if service_line["LineNum"] in copied_service_lines:
                continue  # safety net — see comment above copied_service_lines
            copied_service_lines.add(service_line["LineNum"])
            doc_lines.append({
                "LineNum": len(doc_lines),
                "BaseType": CREDIT_MEMO_BASE_TYPE_INVOICES,
                "BaseEntry": invoice["DocEntry"],
                "BaseLine": service_line["LineNum"],
                "ItemCode": service_item_code,
                "Quantity": service_line.get("Quantity", 1),
                "UnitPrice": service_line.get("UnitPrice"),
                "WarehouseCode": service_line.get("WarehouseCode"),
                "VatGroup": service_line.get("VatGroup"),
                "AccountCode": service_line.get("AccountCode"),
            })

    if not doc_lines:
        return None, skipped

    payload = {
        "CardCode": invoice["CardCode"],
        "DocDate": str(posted_date)[:10],
        "DocDueDate": str(posted_date)[:10],
        "TaxDate": str(posted_date)[:10],
        "NumAtCard": amazon_order_id,
        "PayToCode": invoice.get("PayToCode"),
        "Address": invoice.get("Address"),
        "Series": series,
        "Comments": f"{amazon_order_id} - Amazon API",
        "DocumentLines": doc_lines,
    }
    return payload, skipped
