"""Mirror SAP outbound Stock Transfers into Unicommerce as an inventory
decrement.

Ported from khanal_tech_integrations/unicommerce/workers/inventory.py,
partly upgraded per docs/superpowers/specs/2026-07-27-pogrn-inventorysync-design.md:
the Frappe source matches a pre-recorded batch number against a UC
shelf-stock lookup (itemTypeInventory/get) to find shelfCode/inventoryType,
silently skipping any line whose batch string doesn't match -- fragile,
and unnecessary, since we already have the authoritative batch + quantity
straight from the SAP transfer itself. That part of the lookup is
dropped. shelfCode/inventoryType themselves still need a live UC lookup,
though: confirmed live that these are real per-facility UC configuration
(e.g. "Shelf_001"), not a generic value -- a made-up "DEFAULT" shelf
code was rejected outright with INVALID_SHELF_CODE. So this keeps one UC
read per unique SKU (cheaper than the Frappe source's per-batch-line
lookup), just not for batch matching.
"""

from __future__ import annotations

import json
import logging

from unicommerce import pogrn, stock_transfers, store
from unicommerce.credentials import get_uc_credentials
from unicommerce.settings import UnicommerceSettings, get_settings
from unicommerce.uc_client import UcApiError, UcClient

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
MAX_HOURS = 48
_DEFAULT_SHELF_CODE = "DEFAULT"
_DEFAULT_INVENTORY_TYPE = "GOOD_INVENTORY"


def _uc_client() -> UcClient:
    uc = UcClient(get_uc_credentials())
    uc.login()
    return uc


def _as_list(value) -> list:
    if isinstance(value, str):
        return json.loads(value) if value else []
    return value or []


def resolve_shelf(uc: UcClient, sku: str, facility: str) -> tuple[str, str]:
    """(shelfCode, inventoryType) for this SKU at this facility, from UC's
    own existing inventory record -- falls back to a generic value only
    if the facility genuinely has no inventory rows for this SKU at all
    (e.g. a brand-new facility that's never received stock via UC), in
    which case the adjustment will likely fail anyway since there's
    nothing to decrement, but at least fails with a UC-side error rather
    than us silently fabricating a shelf that was never real."""
    rows = uc.get_item_type_inventory(sku, facility)
    if not rows:
        return _DEFAULT_SHELF_CODE, _DEFAULT_INVENTORY_TYPE
    first = rows[0]
    return str(first.get("shelf") or _DEFAULT_SHELF_CODE), str(first.get("type") or _DEFAULT_INVENTORY_TYPE)


def build_adjustments(line_items: list[dict], remarks: str, facility: str, uc: UcClient) -> list[dict]:
    """One REMOVE adjustment per SAP batch line -- batch/qty come straight
    from the transfer's own BatchNumbers (no UC lookup needed for that
    part), but shelfCode/inventoryType are resolved from UC's own
    inventory record per unique SKU (real per-facility config, see
    resolve_shelf). facility must be repeated inside each adjustment, not
    just the request's Facility header -- confirmed live, UC rejects a
    per-item adjustment missing it with INVALID_FACILITY_CODE even
    though the header is set correctly, matching the Frappe source's own
    per-item facilityCode."""
    adjustments: list[dict] = []
    shelf_cache: dict[str, tuple[str, str]] = {}
    for line in line_items:
        sku = str(line.get("ItemCode") or "").strip()
        if not sku:
            continue
        if sku not in shelf_cache:
            shelf_cache[sku] = resolve_shelf(uc, sku, facility)
        shelf_code, inventory_type = shelf_cache[sku]
        batches = line.get("BatchNumbers") or []
        if not batches:
            qty = float(line.get("Quantity") or 0)
            if qty > 0:
                adjustments.append(_adjustment(sku, qty, remarks, facility, shelf_code, inventory_type))
            continue
        for b in batches:
            qty = float(b.get("Quantity") or 0)
            if qty > 0:
                adjustments.append(_adjustment(sku, qty, remarks, facility, shelf_code, inventory_type))
    return adjustments


def _adjustment(sku: str, qty: float, remarks: str, facility: str, shelf_code: str, inventory_type: str) -> dict:
    return {
        "itemSKU": sku,
        "quantity": qty,
        "shelfCode": shelf_code,
        "inventoryType": inventory_type,
        "adjustmentType": "REMOVE",
        "facilityCode": facility,
        "remarks": remarks,
    }


def fetch_new_transfers(conn, limit: int = 50) -> int:
    """Same polling shape as pogrn.fetch_new_transfers, filtered to the
    outward side instead (EC-FG->EC-B2C specifically, or any transfer out
    of an AMZ-* warehouse)."""
    watermark = int(store.get_watermark(conn, "inventory_sync_transfers") or 0)
    amz_warehouses = pogrn.watched_warehouses() - {"EC-FG"}
    transfers = stock_transfers.list_since(watermark, limit=limit)
    staged = 0
    highest = watermark
    for summary in transfers:
        doc_entry = int(summary["DocEntry"])
        highest = max(highest, doc_entry)
        if not stock_transfers.outward_lines(summary, amz_warehouses):
            continue
        full = stock_transfers.detail(doc_entry)
        lines = stock_transfers.outward_lines(full, amz_warehouses)
        if not lines:
            continue
        from_wh = str(lines[0].get("FromWarehouseCode") or "")
        to_wh = str(lines[0].get("WarehouseCode") or "")
        if store.stage_inventory_sync_transfer(conn, doc_entry, from_wh, to_wh, lines):
            staged += 1
    if transfers:
        store.set_watermark(conn, "inventory_sync_transfers", str(highest))
    return staged


def _process_claimed(claimed: dict, uc: UcClient, settings: UnicommerceSettings, dry_run: bool) -> dict:
    """uc is required even when dry_run=True -- resolving real shelf
    codes is a read, not a write, and dry_run only needs to skip the
    final adjust_inventory_bulk call, not UC reads used to build an
    accurate preview."""
    sap_doc_entry = claimed["sap_doc_entry"]
    line_items = _as_list(claimed["line_items"])
    from_warehouse = str(line_items[0].get("FromWarehouseCode") or "") if line_items else str(claimed.get("from_warehouse") or "")
    facility = pogrn.facility_code_for_warehouse(settings, from_warehouse)
    if not facility:
        raise ValueError(f"No UC facility mapping for SAP warehouse {from_warehouse!r} (check settings.warehouse_map)")

    remarks = f"Outward stock transfer - SAP DocEntry {sap_doc_entry}"
    adjustments = build_adjustments(line_items, remarks, facility, uc)
    if not adjustments:
        raise ValueError("No valid SKU/quantity in transfer line items")

    if dry_run:
        return {"sap_doc_entry": sap_doc_entry, "status": "dry_run", "facility": facility, "would_adjust": adjustments}

    resp = uc.adjust_inventory_bulk(adjustments, facility_code=facility)
    item_responses = resp.get("inventoryAdjustmentResponses") or []
    failures = [r for r in item_responses if not r.get("successful")]
    if item_responses and failures:
        detail = "; ".join(
            f"{(r.get('facilityInventoryAdjustment') or {}).get('itemSKU', '?')}: "
            f"{', '.join(e.get('message', 'unknown') for e in (r.get('errors') or []))}"
            for r in failures
        )
        raise UcApiError(f"{len(failures)}/{len(adjustments)} adjustments failed: {detail}")

    return {"sap_doc_entry": sap_doc_entry, "status": "synced", "facility": facility, "adjusted": len(adjustments)}


def process_ready_transfers(conn, limit: int = 20, dry_run: bool = False) -> dict:
    settings = get_settings()
    staged = fetch_new_transfers(conn, limit=limit)
    recovery = store.recover_failed_inventory_sync(conn, limit=limit, max_attempts=MAX_ATTEMPTS, max_hours=MAX_HOURS)

    doc_entries = store.get_claimable_inventory_sync_doc_entries(conn, limit)
    processed = synced = failed = 0
    uc = _uc_client()  # needed even for dry_run: shelf-code resolution is a read

    if dry_run:
        # Preview only -- must never claim (same reasoning as pogrn.py's
        # process_ready_transfers: a claim with no matching release in
        # dry_run mode would permanently block a real future run).
        results = []
        for doc_entry in doc_entries:
            row = store.get_inventory_sync_transfer(conn, doc_entry)
            if not row:
                continue
            processed += 1
            results.append(_process_claimed(row, uc, settings=settings, dry_run=True))
        return {
            "staged": staged, "recovered": recovery["recovered"], "escalated_dead": recovery["escalated"],
            "processed": processed, "previews": results,
        }

    for doc_entry in doc_entries:
        claimed = store.claim_inventory_sync(conn, doc_entry)
        if not claimed:
            continue
        processed += 1
        try:
            result = _process_claimed(claimed, uc, settings, dry_run=False)
            if result.get("status") == "synced":
                store.mark_inventory_synced(conn, doc_entry)
                synced += 1
        except Exception as e:
            failed += 1
            store.mark_inventory_sync_failed(conn, doc_entry, str(e), MAX_ATTEMPTS)
            logger.error("inventory_sync: transfer %s crashed: %s", doc_entry, e, exc_info=True)

    return {
        "staged": staged, "recovered": recovery["recovered"], "escalated_dead": recovery["escalated"],
        "processed": processed, "synced": synced, "failed": failed,
    }


def process_one_transfer(conn, sap_doc_entry: int, dry_run: bool = False) -> dict:
    """Manual single-transfer retry/dry-run, bypassing the watermark."""
    settings = get_settings()
    existing = store.get_inventory_sync_transfer(conn, sap_doc_entry)
    if not existing:
        amz_warehouses = pogrn.watched_warehouses() - {"EC-FG"}
        full = stock_transfers.detail(sap_doc_entry)
        lines = stock_transfers.outward_lines(full, amz_warehouses)
        if not lines:
            return {"status": "not_applicable", "sap_doc_entry": sap_doc_entry, "message": "No line matching the outward warehouse rules"}
        from_wh = str(lines[0].get("FromWarehouseCode") or "")
        to_wh = str(lines[0].get("WarehouseCode") or "")
        store.stage_inventory_sync_transfer(conn, sap_doc_entry, from_wh, to_wh, lines)
        existing = store.get_inventory_sync_transfer(conn, sap_doc_entry)

    uc = _uc_client()  # needed even for dry_run: shelf-code resolution is a read

    if dry_run:
        return _process_claimed(existing, uc, settings=settings, dry_run=True)

    claimed = store.claim_inventory_sync(conn, sap_doc_entry)
    if not claimed:
        return {"status": "already_claimed_or_done", "sap_doc_entry": sap_doc_entry}

    try:
        result = _process_claimed(claimed, uc, settings, dry_run=False)
        if result.get("status") == "synced":
            store.mark_inventory_synced(conn, sap_doc_entry)
        return result
    except Exception as e:
        status = store.mark_inventory_sync_failed(conn, sap_doc_entry, str(e), MAX_ATTEMPTS)
        return {"status": status, "sap_doc_entry": sap_doc_entry, "error": str(e)}
