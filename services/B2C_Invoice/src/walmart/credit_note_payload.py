"""Build SAP B1 AR Credit Note payload for one Walmart return.

Rules (Finance):
  - Gate: current_refund_status == REFUND_COMPLETED
  - Gate: credit_note_status not already Posted / Posted (Manual) / Not Applicable
  - Exclude RETURN_TO_SENDER lines; if none remain → not postable
  - Every remaining SKU must map to SAP and exist on the original AR Invoice
  - No discount charges on return lines (hard fail — no partial CNs)
  - No skipped SKUs (any miss → fail entire return)
  - Product lines: WithoutInventoryMovement=tYES, BaseType=13, batches from invoice
  - EXP/tax lines: prorated by returned_qty / invoice_product_qty
  - DocDate / DocDueDate / TaxDate = refund completed (latest statusTime) Pacific
  - Invoice lookup key: NumAtCard == customerOrderId
  - CN NumAtCard = return_order_id (RMA idempotency; one customer order may have many returns)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from walmart.dates import PACIFIC
from walmart.sap_invoice_payload import (
    CARD_CODE,
    TAX_CODE,
    TAX_ITEM_CODE,
    U_CHANNEL,
    U_CREATED_BY,
    WAREHOUSE_CODE,
)

RETURN_TO_SENDER_REASON = "RETURN_TO_SENDER"
REFUND_COMPLETED = "REFUND_COMPLETED"
CN_SERIES = 104

BLOCKED_CN_STATUSES = frozenset(
    {"Posted", "Posted (Manual)", "Not Applicable"}
)

ItemResolver = Callable[[str], str]


class CreditNoteBuildError(Exception):
    """Hard failure while building a credit note (no partial post)."""


def _iso_to_pacific_date_str(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(PACIFIC).date().isoformat()
    except ValueError:
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            return raw[:10]
        return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def parse_return_order_lines(raw_json: Any) -> list[dict[str, Any]]:
    if not raw_json:
        return []
    if isinstance(raw_json, str):
        import json

        try:
            raw_json = json.loads(raw_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if not isinstance(raw_json, dict):
        return []
    return [ln for ln in _as_list(raw_json.get("returnOrderLines")) if isinstance(ln, dict)]


def line_has_discount(line: dict[str, Any]) -> bool:
    for charge in _as_list(line.get("charges")):
        if not isinstance(charge, dict):
            continue
        if charge.get("isDiscount") is True:
            return True
        name = str(charge.get("chargeName") or "").lower()
        category = str(charge.get("chargeCategory") or "").lower()
        if "discount" in name or "discount" in category:
            return True
    return False


def line_refunded_qty(line: dict[str, Any]) -> float:
    refunded = line.get("refundedQty")
    try:
        if refunded is not None and float(refunded) > 0:
            return float(refunded)
    except (TypeError, ValueError):
        pass
    qty = line.get("quantity") or {}
    if isinstance(qty, dict):
        try:
            return float(qty.get("measurementValue") or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(qty or 0)
    except (TypeError, ValueError):
        return 0.0


def line_sku(line: dict[str, Any]) -> str:
    item = line.get("item") or {}
    return str(item.get("sku") or "").strip()


def postable_return_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude RETURN_TO_SENDER; keep lines with qty > 0."""
    out: list[dict[str, Any]] = []
    for line in lines:
        reason = str(line.get("returnReason") or "").strip()
        if reason == RETURN_TO_SENDER_REASON:
            continue
        if line_refunded_qty(line) <= 0:
            continue
        out.append(line)
    return out


def can_post_credit_note(
    *,
    current_refund_status: str | None,
    credit_note_status: str | None,
    raw_json: Any,
) -> tuple[bool, str]:
    """Return (ok, reason). reason empty when ok."""
    if str(current_refund_status or "").strip() != REFUND_COMPLETED:
        return False, f"refund status is {current_refund_status!r}, need {REFUND_COMPLETED}"

    status = str(credit_note_status or "").strip()
    if status in BLOCKED_CN_STATUSES:
        return False, f"credit_note_status is {status!r}"

    lines = postable_return_lines(parse_return_order_lines(raw_json))
    if not lines:
        return False, "no postable return lines (all RETURN_TO_SENDER or zero qty)"

    for line in lines:
        if line_has_discount(line):
            return False, "discount charges on return lines are unsupported"
        if not line_sku(line):
            return False, "return line missing SKU"

    return True, ""


def _refund_completed_date_pacific(raw_json: Any, lines: list[dict[str, Any]]) -> str | None:
    """Latest statusTime among return lines (refund completed), Pacific calendar date."""
    times: list[str] = []
    for line in lines:
        st = line.get("statusTime")
        if st:
            times.append(str(st))
    if isinstance(raw_json, dict):
        # fallback: any top-level timestamp if present
        for key in ("returnOrderDate",):
            pass
    if not times:
        return None
    times.sort()
    return _iso_to_pacific_date_str(times[-1])


def _return_created_date_pacific(raw_json: Any, fallback_iso: str | None) -> str | None:
    if isinstance(raw_json, dict) and raw_json.get("returnOrderDate"):
        return _iso_to_pacific_date_str(str(raw_json["returnOrderDate"]))
    return _iso_to_pacific_date_str(fallback_iso)


def _is_product_invoice_line(line: dict[str, Any]) -> bool:
    code = str(line.get("ItemCode") or "")
    if not code:
        return False
    if code.startswith("EXP"):
        return False
    return True


def _is_exp_invoice_line(line: dict[str, Any]) -> bool:
    code = str(line.get("ItemCode") or "")
    return code.startswith("EXP")


def _scale_batches(
    batches: list[dict[str, Any]] | None, quantity: float
) -> list[dict[str, Any]] | None:
    if not batches:
        return None
    total = 0.0
    for b in batches:
        try:
            total += float(b.get("Quantity") or 0)
        except (TypeError, ValueError):
            continue
    if total <= 0:
        # Fall back to first batch with full returned qty
        first = batches[0]
        return [
            {
                "BatchNumber": str(first.get("BatchNumber") or first.get("batch_number") or ""),
                "Quantity": round(quantity, 6),
            }
        ]

    scaled: list[dict[str, Any]] = []
    remaining = quantity
    for i, b in enumerate(batches):
        bn = str(b.get("BatchNumber") or b.get("batch_number") or "").strip()
        if not bn:
            continue
        try:
            orig_qty = float(b.get("Quantity") or 0)
        except (TypeError, ValueError):
            continue
        if i == len(batches) - 1:
            take = remaining
        else:
            take = round(quantity * (orig_qty / total), 6)
            take = min(take, remaining)
        if take <= 0:
            continue
        scaled.append({"BatchNumber": bn, "Quantity": round(take, 6)})
        remaining = round(remaining - take, 6)
        if remaining <= 0:
            break
    if remaining > 0 and scaled:
        scaled[-1]["Quantity"] = round(float(scaled[-1]["Quantity"]) + remaining, 6)
    return scaled or None


def _pop_matching_invoice_line(
    lines_by_item: dict[str, list[dict[str, Any]]], item_code: str
) -> dict[str, Any] | None:
    bucket = lines_by_item.get(item_code)
    if not bucket:
        return None
    return bucket.pop(0)


def build_credit_note_payload(
    *,
    return_row: dict[str, Any],
    invoice: dict[str, Any],
    resolve_item_code: ItemResolver,
) -> dict[str, Any]:
    """Build a fresh CreditNotes payload from return raw_json + live SAP invoice.

    Raises CreditNoteBuildError on any hard failure (no partial CNs).
    """
    return_order_id = str(return_row.get("return_order_id") or "").strip()
    customer_order_id = str(return_row.get("customer_order_id") or "").strip()
    if not return_order_id or not customer_order_id:
        raise CreditNoteBuildError("return missing return_order_id or customer_order_id")

    ok, reason = can_post_credit_note(
        current_refund_status=return_row.get("current_refund_status"),
        credit_note_status=return_row.get("credit_note_status"),
        raw_json=return_row.get("raw_json"),
    )
    if not ok:
        raise CreditNoteBuildError(reason)

    raw = return_row.get("raw_json")
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    lines = postable_return_lines(parse_return_order_lines(raw))
    invoice_lines = [ln for ln in _as_list(invoice.get("DocumentLines")) if isinstance(ln, dict)]
    product_lines_by_item: dict[str, list[dict[str, Any]]] = {}
    invoice_product_qty_by_item: dict[str, float] = {}
    for ln in invoice_lines:
        if not _is_product_invoice_line(ln):
            continue
        code = str(ln.get("ItemCode") or "").strip()
        product_lines_by_item.setdefault(code, []).append(ln)
        try:
            invoice_product_qty_by_item[code] = invoice_product_qty_by_item.get(code, 0.0) + float(
                ln.get("Quantity") or 0
            )
        except (TypeError, ValueError):
            pass

    base_doc_entry = int(invoice.get("DocEntry"))
    pay_to = str(invoice.get("PayToCode") or "").strip() or None

    doc_lines: list[dict[str, Any]] = []
    returned_qty_by_item: dict[str, float] = {}
    reasons: list[str] = []
    po_ids: list[str] = []

    for line in lines:
        if line_has_discount(line):
            raise CreditNoteBuildError("discount charges on return lines are unsupported")

        sku = line_sku(line)
        qty = line_refunded_qty(line)
        try:
            item_code = resolve_item_code(sku)
        except Exception as e:
            raise CreditNoteBuildError(f"no SAP mapping for SKU {sku!r}: {e}") from e

        inv_line = _pop_matching_invoice_line(product_lines_by_item, item_code)
        if inv_line is None:
            raise CreditNoteBuildError(
                f"SKU {sku!r} (ItemCode {item_code}) not found on AR Invoice DocEntry={base_doc_entry}"
            )

        try:
            unit_price = float(inv_line.get("UnitPrice") or 0)
        except (TypeError, ValueError):
            unit_price = 0.0

        warehouse = str(inv_line.get("WarehouseCode") or WAREHOUSE_CODE).strip() or WAREHOUSE_CODE
        tax_code = str(inv_line.get("TaxCode") or TAX_CODE).strip() or TAX_CODE
        base_line = inv_line.get("LineNum")
        if base_line is None:
            raise CreditNoteBuildError(f"invoice line for {item_code} missing LineNum")

        po = str(line.get("purchaseOrderId") or "").strip()
        if po and po not in po_ids:
            po_ids.append(po)
        reason = str(line.get("returnReason") or "").strip()
        if reason and reason not in reasons:
            reasons.append(reason)

        returned_qty_by_item[item_code] = returned_qty_by_item.get(item_code, 0.0) + qty

        product_line: dict[str, Any] = {
            "ItemCode": item_code,
            "Quantity": qty,
            "UnitPrice": round(unit_price, 2),
            "WarehouseCode": warehouse,
            "TaxCode": tax_code,
            "BaseType": 13,
            "BaseEntry": base_doc_entry,
            "BaseLine": int(base_line),
            "WithoutInventoryMovement": "tYES",
            "U_Channel": U_CHANNEL,
            "U_OrderID": customer_order_id,
            "U_PurchaseOrderID": po or str(return_row.get("purchase_order_ids") or ""),
        }
        batches = _scale_batches(inv_line.get("BatchNumbers"), qty)
        if batches:
            product_line["BatchNumbers"] = batches
        doc_lines.append(product_line)

    if not doc_lines:
        raise CreditNoteBuildError("no postable product lines after mapping")

    # Prorate EXP lines from the original invoice.
    total_invoice_product_qty = sum(invoice_product_qty_by_item.values()) or 0.0
    total_returned_product_qty = sum(returned_qty_by_item.values()) or 0.0
    if total_invoice_product_qty > 0 and total_returned_product_qty > 0:
        ratio = min(total_returned_product_qty / total_invoice_product_qty, 1.0)
    else:
        ratio = 0.0

    for exp in invoice_lines:
        if not _is_exp_invoice_line(exp):
            continue
        try:
            exp_price = float(exp.get("UnitPrice") or 0) * float(exp.get("Quantity") or 1)
        except (TypeError, ValueError):
            continue
        prorated = round(exp_price * ratio, 2)
        if prorated <= 0:
            continue
        base_line = exp.get("LineNum")
        exp_line: dict[str, Any] = {
            "ItemCode": str(exp.get("ItemCode")),
            "Quantity": 1.0,
            "UnitPrice": prorated,
            "WarehouseCode": str(exp.get("WarehouseCode") or WAREHOUSE_CODE),
            "TaxCode": str(exp.get("TaxCode") or TAX_CODE),
            "BaseType": 13,
            "BaseEntry": base_doc_entry,
            "U_Channel": U_CHANNEL,
            "U_OrderID": customer_order_id,
        }
        if base_line is not None:
            exp_line["BaseLine"] = int(base_line)
        if exp.get("AccountCode"):
            exp_line["AccountCode"] = exp["AccountCode"]
        # Prefer TAX item code when invoice used a different naming in examples
        if not exp_line["ItemCode"]:
            exp_line["ItemCode"] = TAX_ITEM_CODE
        doc_lines.append(exp_line)

    po_part = ",".join(po_ids) if po_ids else str(return_row.get("purchase_order_ids") or "")
    reason_part = ",".join(reasons) if reasons else str(return_row.get("return_reasons") or "")

    doc_date = _refund_completed_date_pacific(raw, lines)
    if not doc_date:
        # Fallback only if no line statusTime: return created date.
        doc_date = _return_created_date_pacific(
            raw,
            str(return_row.get("return_order_date") or "") or None,
        )
    if not doc_date:
        raise CreditNoteBuildError("could not resolve DocDate / TaxDate from return")

    payload: dict[str, Any] = {
        "CardCode": CARD_CODE,
        "DocDate": doc_date,
        "DocDueDate": doc_date,
        "TaxDate": doc_date,
        "Series": CN_SERIES,
        # Idempotency key = RMA / return order id (unique per return).
        "NumAtCard": return_order_id,
        "Comments": (
            f"Walmart Return | RMA {return_order_id} | PO {po_part} | Reason: {reason_part}"
        ),
        "U_CreatedBy": U_CREATED_BY,
        "DocumentLines": doc_lines,
    }
    if pay_to:
        payload["PayToCode"] = pay_to
    return payload
