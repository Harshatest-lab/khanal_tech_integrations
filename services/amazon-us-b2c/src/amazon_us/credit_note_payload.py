"""Build the SAP B1 AR Credit Note payload for one Amazon refund event.

Rules (Finance-mandated, live on production since 2026-07-17):
  - Every item line carries WithoutInventoryMovement='tYES' and NO
    BatchNumbers: refunded FBA goods never return to sellable stock, so
    the CN must reverse revenue without receiving inventory.
  - Item lines are BaseType/BaseEntry/BaseLine-linked to the original AR
    Invoice so the CN actually reverses that invoice. The caller MUST
    have verified the invoice's NumAtCard equals the order id before
    handing base_doc_entry in (stale-pointer guard).
  - Quantity is INFERRED — the Finances API reports money, not units:
    qty = abs(refunded principal) / original unit price.
  - The buyer's refunded shipping comes back as an EXP000004 line, and
    shipping tax folds into the EXP000003 tax line, mirroring the
    invoice's own structure (CNs that skipped freight were a real
    production bug).
  - DocDate = refund PostedDate. NumAtCard = refund_id
    ("{order_id}-{PostedDate}") — the idempotency key on the SAP side.
"""

from __future__ import annotations

from typing import Any, Callable

from amazon_us import constants
from amazon_us.finances_api import RefundEvent
from amazon_us.invoice_payload import BomLookup, build_line_udfs
from amazon_us.state_codes import US_TERRITORY_COUNTRY_CODES, normalize_us_state_code

AcctResolver = Callable[[str], str]

# SAP stores Quantity at 3 decimals; our own draw-down math runs at 6. A
# remainder below SAP's own precision floor is rounding dust, not a real
# top-up amount -- posting it makes SAP reject the whole document with
# "Quantity cannot be zero" (confirmed for real 2026-08-14, order
# 113-3926758-2793850: computed 0.999267 against a stored 0.999, leaving
# a dust remainder of 0.000267).
_MIN_POSTABLE_QTY = 0.001


def build_credit_note_payload(
    order: dict[str, Any],
    refund_event: RefundEvent,
    item_mapping: dict[str, str],
    base_doc_entry: int,
    invoice_lines: list[dict],
    resolve_acct: AcctResolver,
    get_bom_children: BomLookup,
    already_posted: dict[str, Any] | None = None,
) -> tuple[dict | None, list[dict]]:
    """Returns (payload, skipped). payload is None when nothing is
    postable (every adjustment unmatched).

    already_posted: {"tax": float, "shipping": float, "qty_by_price": {unit_price: qty}}
    — what a PRIOR CN already posted for this same refund_id, when Amazon
    has grown the event's item_adjustments since (see
    sap_client.find_credit_notes_by_ref_prefix). Every quantity/amount
    below is reduced by it, so only the newly-added remainder gets built.
    Keyed by UnitPrice, not item_code -- confirmed for real 2026-08-14: a
    seller_sku's SAP item_code mapping can drift after the base CN posted
    (item_mapping reflects TODAY's mapping, but already_posted reflects
    whatever item_code was actually on SAP at post time), so item_code
    keying can silently never match and treat a fully-covered order as
    unclaimed. UnitPrice comes from the order's own historical data and
    doesn't drift the way a live SKU mapping does."""
    already_posted = already_posted or {}
    already_qty_by_price: dict[float, float] = already_posted.get("qty_by_price") or {}
    already_tax = float(already_posted.get("tax") or 0.0)
    already_shipping = float(already_posted.get("shipping") or 0.0)

    unit_price_by_sku = {
        (line.get("seller_sku") or "").strip(): float(line.get("unit_price") or 0)
        for line in order.get("lines", [])
    }
    lines_by_item_code: dict[str, list[dict]] = {}
    for line in invoice_lines:
        lines_by_item_code.setdefault(line.get("ItemCode"), []).append(line)

    # Invoiced quantity still available per invoice line, for this document
    # only (earlier CNs' consumption isn't tracked here). None = the line
    # carries no Quantity, so it can't be capped.
    remaining_qty: dict[int, float | None] = {
        id(line): (float(line["Quantity"]) if line.get("Quantity") not in (None, "") else None)
        for line in invoice_lines
    }

    merged_lines: dict[tuple, dict] = {}

    line_udfs = build_line_udfs(order)

    doc_lines: list[dict] = []
    skipped: list[dict] = []
    total_tax = 0.0
    total_shipping = 0.0
    line_num = 0

    for adj in refund_event.item_adjustments:
        # Shipping and its tax reverse in full regardless of line matching —
        # they ride on service lines, not the item line.
        total_tax += adj.tax_amount + adj.shipping_tax_amount
        total_shipping += adj.shipping_amount

        unit_price = unit_price_by_sku.get(adj.seller_sku)
        item_code = item_mapping.get(adj.seller_sku)

        if not unit_price or not item_code:
            skipped.append(
                {"seller_sku": adj.seller_sku, "reason": "No unit price or SAP mapping on staged order"}
            )
            continue

        qty = round(abs(adj.principal_amount) / unit_price, 6)
        price_key = round(unit_price, 2)
        if price_key in already_qty_by_price:
            # A pool to DRAW DOWN, not a flat per-line subtraction -- an
            # event can carry multiple item_adjustments for the same
            # item_code (2 separate refunded units posted as 2 lines, not
            # one qty-2 line; confirmed for real 2026-08-12). Subtracting
            # the same already-posted amount from every adjustment for
            # that code would zero out ALL of them instead of just the
            # ones it actually covers.
            already = already_qty_by_price[price_key]
            consume = min(qty, already)
            qty = round(qty - consume, 6)
            already_qty_by_price[price_key] = round(already - consume, 6)
        if qty < _MIN_POSTABLE_QTY:
            # Refund amount for this line rounds to zero units at the
            # order's unit price (e.g. a $0.00 principal adjustment, or one
            # far smaller than a single unit) — nothing to reverse on the
            # item line itself, though shipping/tax above already
            # accumulated regardless. Log it instead of silently vanishing
            # (this is exactly what looked like an unexplained "no
            # postable lines: []" skip to Finance on 2026-08-11).
            #
            # The already_posted draw-down above can also leave pure
            # rounding dust here rather than a true zero: SAP stores
            # Quantity at 3 decimals, but qty is computed fresh at 6, so a
            # remainder like 0.000267 (confirmed for real 2026-08-14, order
            # 113-3926758-2793850) is the precision gap between "0.999267
            # computed now" and "0.999 as SAP actually stored it", not a
            # genuine top-up. SAP itself then rejects that as a zero
            # quantity, so treat anything under SAP's own precision floor
            # the same as an exact zero rather than let it reach SAP.
            skipped.append(
                {
                    "seller_sku": adj.seller_sku,
                    "reason": f"Refund amount ({adj.principal_amount}) rounds to <{_MIN_POSTABLE_QTY} qty at unit price {unit_price}",
                }
            )
            continue

        # Pop only once we know this adjustment will actually consume a line —
        # popping earlier would discard the line for a later same-item_code
        # adjustment even though this one turned out to be a no-op.
        children = get_bom_children(item_code)
        if children:
            # Sales BOM (combo/bundle): the base invoice isn't one line for
            # item_code, it's a zero-price "iSalesTree" parent line plus one
            # real "iIngredient" line per component (see
            # invoice_payload.build_invoice_payload). A CN must reverse the
            # SAME rows it's linked from — BaseLine has to point at the
            # actual invoiced row, and reversing the full refund amount
            # against the zero-price parent would both fail SAP's
            # remaining-open-amount check on that base line and leave the
            # real revenue (booked on the child lines) unreversed.
            parent_line = _pop_matching_invoice_line(lines_by_item_code, item_code)
            if parent_line is None:
                skipped.append({"seller_sku": adj.seller_sku, "reason": "No matching invoice line found"})
                continue
            doc_lines.append(
                {
                    "LineNum": line_num,
                    "BaseType": 13,
                    "BaseEntry": int(base_doc_entry),
                    "BaseLine": parent_line.get("LineNum"),
                    "ItemCode": item_code,
                    "Quantity": qty,
                    "UnitPrice": 0.0,
                    "WarehouseCode": constants.SAP_WAREHOUSE,
                    "TaxCode": constants.SAP_TAX_CODE,
                    "TaxType": "tt_Yes",
                    "TaxLiable": "tYES",
                    "TaxTotal": 0.0,
                    # NOT WithoutInventoryMovement here: SAP rejects it on a
                    # Sales-BOM tree row ("row does not support the 'Without
                    # Qty Posting' feature") — the parent carries no real
                    # inventory quantity itself (that's on the children), so
                    # the flag is meaningless on this row, not just unwanted.
                    "TreeType": "iSalesTree",
                    **line_udfs,
                }
            )
            line_num += 1

            # Refunded qty vs. what was actually invoiced on the parent row —
            # scales down cleanly for a partial refund (e.g. 1 of 2 bundles).
            original_parent_qty = float(parent_line.get("Quantity") or 0) or qty
            scale = qty / original_parent_qty if original_parent_qty else 1.0
            for child_code, _qty_per_unit in children:
                child_invoice_line = _pop_matching_invoice_line(lines_by_item_code, child_code)
                if child_invoice_line is None:
                    skipped.append(
                        {"seller_sku": adj.seller_sku, "reason": f"No matching invoice line for BOM component {child_code}"}
                    )
                    continue
                child_qty = round(float(child_invoice_line.get("Quantity") or 0) * scale, 6)
                if child_qty < _MIN_POSTABLE_QTY:
                    continue
                doc_lines.append(
                    {
                        "LineNum": line_num,
                        "BaseType": 13,
                        "BaseEntry": int(base_doc_entry),
                        "BaseLine": child_invoice_line.get("LineNum"),
                        "ItemCode": child_code,
                        "Quantity": child_qty,
                        # Reverse at the price actually invoiced on this row,
                        # not a re-derived split — keeps the CN consistent
                        # with what SAP has on file for that base line.
                        "UnitPrice": float(child_invoice_line.get("UnitPrice") or 0),
                        "WarehouseCode": constants.SAP_WAREHOUSE,
                        "TaxCode": constants.SAP_TAX_CODE,
                        "TaxType": "tt_Yes",
                        "TaxLiable": "tYES",
                        "WithoutInventoryMovement": "tYES",
                        "TreeType": "iIngredient",
                        **line_udfs,
                    }
                )
                line_num += 1
            continue

        # Fill same-item invoice lines up to each one's invoiced Quantity,
        # splitting one adjustment across lines when it has to. Popping a
        # single line per adjustment sent a 4-unit refund to a 1-unit base
        # line (order 112-7159795-8197858: invoice lines FGDC0824 x1 and
        # x29, 12-unit refund rejected -10 "Quantity cannot exceed the
        # quantity in the base document", 2026-09-25).
        parts = _allocate_invoice_lines(lines_by_item_code.get(item_code) or [], remaining_qty, qty)
        invoice_line = parts[0][0] if parts else None
        actual_item_code = item_code
        if invoice_line is None:
            # The SKU->item mapping can drift after an invoice posts (SAP
            # master data corrected later) — confirmed for real 2026-08-11:
            # an order invoiced against FGDC00009 whose current mapping had
            # since moved to FGDC00005. The CN must reverse whatever's
            # ACTUALLY on the invoice, not today's mapping, so fall back to
            # matching by UnitPrice (unique per line on a typical order)
            # and use that line's real ItemCode instead of item_code.
            fallback = _pop_line_by_price_fallback(lines_by_item_code, unit_price)
            if fallback:
                actual_item_code, invoice_line = fallback
                parts = [(invoice_line, qty)]
        if invoice_line is None:
            skipped.append({"seller_sku": adj.seller_sku, "reason": "No matching invoice line found"})
            continue

        for part_line, part_qty in parts:
            # One document line per base invoice row: SAP rejects a second
            # line pointing at the same BaseLine ("Invalid value
            # [DocumentLines.BaseLine]", order 112-7159795-8197858 on
            # 2026-09-30), so quantities aimed at the same row are summed.
            merge_key = (part_line.get("LineNum"), actual_item_code, unit_price)
            existing = merged_lines.get(merge_key)
            if existing is not None:
                existing["Quantity"] = round(existing["Quantity"] + part_qty, 6)
                continue
            doc_line = {
                "LineNum": line_num,
                "BaseType": 13,  # AR Invoice
                "BaseEntry": int(base_doc_entry),
                "BaseLine": part_line.get("LineNum"),
                "ItemCode": actual_item_code,
                "Quantity": part_qty,
                "UnitPrice": unit_price,
                "WarehouseCode": constants.SAP_WAREHOUSE,
                "TaxCode": constants.SAP_TAX_CODE,
                "WithoutInventoryMovement": "tYES",
                **line_udfs,
            }
            merged_lines[merge_key] = doc_line
            doc_lines.append(doc_line)
            line_num += 1

    if not doc_lines:
        return None, skipped

    # Net out whatever a prior CN under this refund_id already reversed —
    # only the newly-added remainder should hit the service lines below.
    total_tax = round(total_tax - (already_tax if total_tax >= 0 else -already_tax), 2)
    total_shipping = round(total_shipping - (already_shipping if total_shipping >= 0 else -already_shipping), 2)
    if abs(total_tax) < 0.01:
        total_tax = 0.0
    if abs(total_shipping) < 0.01:
        total_shipping = 0.0

    if total_tax:
        doc_lines.append(
            _service_line(
                line_num,
                constants.SAP_TAX_ITEM_CODE,
                abs(total_tax),
                resolve_acct(constants.SAP_TAX_GL_FORMAT),
                line_udfs,
            )
        )
        line_num += 1
    if total_shipping:
        doc_lines.append(
            _service_line(
                line_num,
                constants.SAP_SHIPPING_ITEM_CODE,
                abs(total_shipping),
                resolve_acct(constants.SAP_SHIPPING_GL_FORMAT),
                line_udfs,
            )
        )

    # Per-state Ship-To/Pay-To address code — same fix and fallback as
    # invoice_payload.build_invoice_payload, kept consistent between
    # invoices and their credit notes. US territories (Puerto Rico, Guam,
    # etc.) count as domestic here too -- see US_TERRITORY_COUNTRY_CODES.
    country = (order.get("ship_country") or "").strip().upper()
    is_domestic = country in ("", "US", "USA") or country in US_TERRITORY_COUNTRY_CODES
    if is_domestic:
        state_code = normalize_us_state_code(order.get("ship_state"))
        ship_to_code = f"{constants.SAP_SHIP_TO_STATE_PREFIX}{state_code}" if state_code else constants.SAP_PAY_TO_CODE
    else:
        ship_to_code = constants.SAP_PAY_TO_CODE

    payload = {
        "CardCode": constants.SAP_CARD_CODE,
        "DocDate": refund_event.posted_date or None,
        "Series": constants.SAP_CN_SERIES,
        "NumAtCard": refund_event.refund_id,
        "PayToCode": ship_to_code,
        "ShipToCode": ship_to_code,
        "U_CreatedBy": constants.SAP_CREATED_BY,
        "DocumentLines": doc_lines,
    }
    if not is_domestic:
        payload["AddressExtension"] = {"ShipToCountry": country, "BillToCountry": country}
    return payload, skipped


def _pop_matching_invoice_line(lines_by_item_code: dict[str, list[dict]], item_code: str) -> dict | None:
    lines = lines_by_item_code.get(item_code)
    return lines.pop(0) if lines else None


def _allocate_invoice_lines(
    lines: list[dict], remaining_qty: dict[int, float | None], qty: float
) -> list[tuple[dict, float]]:
    """Split `qty` over the same-item invoice `lines`, filling each up to its
    invoiced Quantity. Returns [(invoice_line, part_qty), ...]; empty when
    there is no line for the item. Exhausted lines are dropped from `lines`
    so the next adjustment moves on to the next one.

    A line with no known Quantity takes the whole amount and is consumed
    (the old one-line-per-adjustment behaviour). If `qty` exceeds everything
    the invoice has, the excess goes on the last line so SAP rejects the
    document loudly (-10) instead of a silent partial credit."""
    parts: list[tuple[dict, float]] = []
    left = qty
    for line in list(lines):
        cap = remaining_qty.get(id(line))
        if cap is None:
            parts.append((line, round(left, 6)))
            lines.remove(line)
            left = 0.0
            break
        take = round(min(left, cap), 6)
        if take >= _MIN_POSTABLE_QTY:
            parts.append((line, take))
            remaining_qty[id(line)] = round(cap - take, 6)
            left = round(left - take, 6)
        if remaining_qty[id(line)] < _MIN_POSTABLE_QTY:
            lines.remove(line)
        if left < _MIN_POSTABLE_QTY:
            left = 0.0
            break
    if parts and left >= _MIN_POSTABLE_QTY:
        last_line, last_qty = parts[-1]
        parts[-1] = (last_line, round(last_qty + left, 6))
    return parts


def _pop_line_by_price_fallback(
    lines_by_item_code: dict[str, list[dict]], target_price: float
) -> tuple[str, dict] | None:
    """Fallback when no line matches the current SKU mapping's item code —
    match by UnitPrice instead (unique per line on a typical order) and
    return the line's REAL ItemCode, so the CN reverses what's actually on
    the invoice rather than today's (possibly since-changed) mapping."""
    for code, lines in lines_by_item_code.items():
        for i, line in enumerate(lines):
            if abs(float(line.get("UnitPrice") or 0) - target_price) < 0.01:
                return code, lines.pop(i)
    return None


def _service_line(line_num: int, item_code: str, amount: float, acct_code: str, udfs: dict) -> dict:
    return {
        "LineNum": line_num,
        "ItemCode": item_code,
        "Quantity": 1,
        "UnitPrice": round(amount, 2),
        "WarehouseCode": constants.SAP_WAREHOUSE,
        "TaxCode": constants.SAP_TAX_CODE,
        "AccountCode": acct_code,
        **udfs,
    }
