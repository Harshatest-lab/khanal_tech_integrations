"""Return-item -> SAP-invoice-line resolution for credit note posting.

Ported from khanal_tech_integrations/unicommerce/returns.py's
prepare_credit_note_items (+ its private _invoice_line_maps/_pick_line
helpers). frappe.get_doc's `payload_json` field becomes the
`payload_json` key of the parent order dict returned by
unicommerce.store.get_order(); SAPClient becomes unicommerce.sap_client's
module-level functions.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any

from unicommerce.mappers import normalize_text


def _pick_text(*values: Any) -> str | None:
    for value in values:
        txt = str(value or "").strip()
        if txt:
            return txt
    return None


def extract_return_ids(ret: dict) -> tuple[str | None, str | None, list[str]]:
    """(uc_return_id, uc_order_id_or_None, fallback_item_codes).

    Ported from khanal_tech_integrations/unicommerce/workers/fetch.py's
    _extract_return_ids. When the parent order id can't be read directly
    off the return payload, fallback_item_codes lets the caller derive it
    from staged order-line codes (store.find_order_ref_by_line_codes) --
    the Frappe source does the same against Order Line Items."""
    rsov = ret.get("returnSaleOrderValue") or {}
    uc_return_id = _pick_text(
        rsov.get("reversePickupCode"), rsov.get("shipmentCode"), rsov.get("code"), rsov.get("returnCode"),
        ret.get("reversePickupCode"), ret.get("shipmentCode"), ret.get("code"), ret.get("returnCode"),
    )
    uc_order_id = _pick_text(
        rsov.get("saleOrderCode"), rsov.get("saleordercode"), rsov.get("orderCode"), rsov.get("ucOrderCode"),
        ret.get("saleOrderCode"), ret.get("saleordercode"), ret.get("orderCode"), ret.get("ucOrderCode"),
    )
    if uc_order_id:
        return uc_return_id, uc_order_id, []

    item_codes: list[str] = []
    for row in ret.get("returnSaleOrderItems") or []:
        code = _pick_text(
            (row or {}).get("saleOrderItemCode"), (row or {}).get("code"), (row or {}).get("channelSaleOrderItemCode")
        )
        if code:
            item_codes.append(code)
    return uc_return_id, None, sorted(set(item_codes))


def extract_return_date(ret: dict) -> str:
    """SAP CN DocDate: prefer the most final event (completed > received >
    delivery > created). Handles ISO strings and epoch (s or ms)."""
    rsov = ret.get("returnSaleOrderValue") or {}
    raw = (
        rsov.get("returnCompletedDate")
        or rsov.get("inventoryReceivedDate")
        or rsov.get("returnDeliveryDate")
        or rsov.get("returnCreatedDate")
        or ret.get("created")
        or ""
    )
    if not raw:
        return date.today().isoformat()
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and str(raw).isdigit()):
        try:
            epoch = int(raw)
            if epoch > 10_000_000_000:
                epoch = int(epoch / 1000)
            return datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat()
        except Exception:
            return date.today().isoformat()
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return str(raw)[:10]


def extract_return_items(ret: dict) -> list:
    return ret.get("returnSaleOrderItems") or []


def extract_return_type(ret: dict) -> str:
    rsov = ret.get("returnSaleOrderValue") or {}
    return "courier" if rsov.get("rtoReason") else "customer"


def _safe_json_loads(raw_value: Any) -> dict:
    if isinstance(raw_value, dict):
        return raw_value
    if not raw_value:
        return {}
    try:
        return json.loads(raw_value)
    except Exception:
        return {}


def _invoice_line_maps(invoice_doc: dict) -> dict[str, Any]:
    by_uc_item_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_item_code: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for idx, line in enumerate(invoice_doc.get("DocumentLines") or []):
        item_code = normalize_text(line.get("ItemCode"))
        sale_order_item_code = normalize_text(
            line.get("U_UCOrderItemID") or line.get("U_Sale_order_itemcode")
        )
        warehouse_code = normalize_text(line.get("WarehouseCode"))
        base_line = line.get("LineNum", idx)
        try:
            base_line = int(base_line)
        except Exception:
            base_line = idx

        line_ref = {
            "base_line": base_line,
            "item_code": item_code,
            "warehouse_code": warehouse_code,
            "account_code": normalize_text(line.get("AccountCode")),
            "batch_numbers": [
                {
                    "BatchNumber": normalize_text((bn or {}).get("BatchNumber")),
                    "Quantity": float((bn or {}).get("Quantity") or 0),
                }
                for bn in (line.get("BatchNumbers") or [])
                if normalize_text((bn or {}).get("BatchNumber"))
            ],
        }
        if sale_order_item_code:
            by_uc_item_id[sale_order_item_code].append(line_ref)
        if item_code:
            by_item_code[item_code].append(line_ref)

    return {"by_uc_item_id": by_uc_item_id, "by_item_code": by_item_code}


def _pick_line(
    item: dict,
    line_maps: dict[str, Any],
    used_keys: set[tuple[str, int]],
) -> dict[str, Any] | None:
    sale_order_item_code = normalize_text(item.get("saleOrderItemCode"))
    item_code = normalize_text(item.get("itemSku"))

    if sale_order_item_code:
        for line in line_maps["by_uc_item_id"].get(sale_order_item_code, []):
            key = (line["item_code"], line["base_line"])
            if key not in used_keys:
                used_keys.add(key)
                return line

    unused_candidates = []
    for line in line_maps["by_item_code"].get(item_code, []):
        key = (line["item_code"], line["base_line"])
        if key not in used_keys:
            unused_candidates.append((key, line))

    if unused_candidates:
        # First unused candidate — handles multi-batch (same SKU, multiple
        # SAP lines) and partial-quantity returns where SAP qty != UC order qty.
        key, line = unused_candidates[0]
        used_keys.add(key)
        return line
    return None


def prepare_credit_note_items(
    parent_order: dict,
    raw_items: list[dict] | None,
    fallback_warehouse: str = "",
) -> dict[str, Any]:
    """Resolve each raw UC return item to the SAP AR Invoice line it must
    reference (BaseLine/BatchNumbers/AccountCode), using the parent
    order's already-posted invoice payload (`payload_json`) as the source
    of truth. No live SAP lookup fallback here — unlike the Frappe source,
    which falls back to a live GET of the invoice by DocEntry when
    payload_json can't resolve a line, this Lambda always writes
    payload_json on post (see store.mark_posted), so that fallback path
    never triggers for orders posted through this pipeline. Left out
    rather than ported unused.
    """
    payload_doc = _safe_json_loads(parent_order.get("payload_json"))
    payload_maps = _invoice_line_maps(payload_doc)
    used_payload_keys: set[tuple[str, int]] = set()
    normalized_items: list[dict] = []

    for row in raw_items or []:
        normalized = dict(row or {})
        normalized["itemSku"] = normalize_text(
            row.get("skuCode") or row.get("itemSku") or row.get("itemTypeSkuCode")
        )
        normalized["saleOrderItemCode"] = normalize_text(
            row.get("saleOrderItemCode") or row.get("code") or row.get("channelSaleOrderItemCode")
        )
        normalized["warehouseCode"] = normalize_text(
            row.get("forwardItemFacility") or row.get("warehouseCode") or row.get("returnFacilityCode") or fallback_warehouse
        )
        try:
            normalized["quantity"] = float(
                row.get("quantity") or row.get("returnedQuantity") or row.get("totalQuantity") or 1
            )
        except Exception:
            normalized["quantity"] = 1.0

        line = _pick_line(normalized, payload_maps, used_payload_keys)
        if line:
            normalized["sapBaseLine"] = int(line["base_line"])
            normalized["sapItemCode"] = line["item_code"]
            normalized["itemSku"] = normalized["itemSku"] or line["item_code"]
            normalized["warehouseCode"] = normalized["warehouseCode"] or line["warehouse_code"]
            normalized["sapAccountCode"] = line.get("account_code") or ""
            normalized["sapBatchNumbers"] = line.get("batch_numbers") or []
        normalized_items.append(normalized)

    resolved_items = [row for row in normalized_items if row.get("sapBaseLine") not in (None, "")]
    unresolved_items = [row for row in normalized_items if row.get("sapBaseLine") in (None, "")]

    return {
        "items": resolved_items + unresolved_items,
        "resolved_count": len(resolved_items),
        "unresolved_count": len(unresolved_items),
        "unresolved": unresolved_items,
    }
