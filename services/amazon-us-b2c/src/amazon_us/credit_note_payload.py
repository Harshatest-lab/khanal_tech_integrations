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
from amazon_us.invoice_payload import build_line_udfs

AcctResolver = Callable[[str], str]


def build_credit_note_payload(
    order: dict[str, Any],
    refund_event: RefundEvent,
    item_mapping: dict[str, str],
    base_doc_entry: int,
    invoice_lines: list[dict],
    resolve_acct: AcctResolver,
) -> tuple[dict | None, list[dict]]:
    """Returns (payload, skipped). payload is None when nothing is
    postable (every adjustment unmatched)."""
    unit_price_by_sku = {
        (line.get("seller_sku") or "").strip(): float(line.get("unit_price") or 0)
        for line in order.get("lines", [])
    }
    lines_by_item_code: dict[str, list[dict]] = {}
    for line in invoice_lines:
        lines_by_item_code.setdefault(line.get("ItemCode"), []).append(line)

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

        invoice_line = _pop_matching_invoice_line(lines_by_item_code, item_code)
        if invoice_line is None:
            skipped.append({"seller_sku": adj.seller_sku, "reason": "No matching invoice line found"})
            continue

        qty = round(abs(adj.principal_amount) / unit_price, 6)
        if qty <= 0:
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
                "WarehouseCode": constants.SAP_WAREHOUSE,
                "TaxCode": constants.SAP_TAX_CODE,
                "WithoutInventoryMovement": "tYES",
                **line_udfs,
            }
        )
        line_num += 1

    if not doc_lines:
        return None, skipped

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

    payload = {
        "CardCode": constants.SAP_CARD_CODE,
        "DocDate": refund_event.posted_date or None,
        "Series": constants.SAP_CN_SERIES,
        "NumAtCard": refund_event.refund_id,
        "PayToCode": constants.SAP_PAY_TO_CODE,
        "DocumentLines": doc_lines,
    }
    return payload, skipped


def _pop_matching_invoice_line(lines_by_item_code: dict[str, list[dict]], item_code: str) -> dict | None:
    lines = lines_by_item_code.get(item_code)
    return lines.pop(0) if lines else None


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
