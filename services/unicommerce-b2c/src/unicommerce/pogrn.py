"""Mirror SAP inbound Stock Transfers into Unicommerce as PO + GRN.

Ported from khanal_tech_integrations/unicommerce/workers/po_grn.py --
only the SAP-transfer-triggered flow (its "Use Case 1"); the second use
case there (filling a GRN for a UC PO created manually inside UC itself)
is out of scope for this build -- see
docs/superpowers/specs/2026-07-27-pogrn-inventorysync-design.md.

Known limitation: if a single SAP transfer has lines going to more than
one *different* watched warehouse, only the first watched line's
destination facility is used for the whole PO/GRN (UC's PO API takes one
Facility header per call). Every real transfer sampled from india_test
had a uniform destination across all its lines, so this is treated as an
acceptable v1 limitation rather than engineering a multi-facility split
for a case that hasn't actually been observed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from unicommerce import stock_transfers, store
from unicommerce.credentials import get_uc_credentials
from unicommerce.sap_payload import CHANNEL_WAREHOUSE_MAP, DEFAULT_WAREHOUSE
from unicommerce.settings import UnicommerceSettings, get_settings
from unicommerce.uc_client import UcApiError, UcClient

logger = logging.getLogger(__name__)

MAX_GRN_ROUNDS = 200
MAX_ATTEMPTS = 8
MAX_HOURS = 48


def watched_warehouses() -> set[str]:
    """EC-FG + every AMZ-* warehouse -- sap_payload.py's existing
    constants are the single source of truth, not duplicated here."""
    return {DEFAULT_WAREHOUSE} | set(CHANNEL_WAREHOUSE_MAP.values())


def facility_code_for_warehouse(settings: UnicommerceSettings, sap_warehouse: str) -> str | None:
    """Invert settings.warehouse_map: its existing keys (e.g.
    "AMAZON_FBA_IN_BLR7", "kfl_ecom_warehouse") already are the UC
    facility identifiers this Lambda needs -- no new setting required."""
    for uc_key, sap_code in (settings.warehouse_map or {}).items():
        if sap_code == sap_warehouse:
            return uc_key
    return None


def _to_uc_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def aggregate_po_items(line_items: list[dict]) -> list[dict]:
    """UC PO rejects duplicate-SKU rows -- sum quantity per SKU."""
    totals: dict[str, float] = {}
    for line in line_items:
        sku = str(line.get("ItemCode") or "").strip()
        qty = float(line.get("Quantity") or 0)
        if sku and qty > 0:
            totals[sku] = round(totals.get(sku, 0.0) + qty, 3)
    return [{"itemSKU": sku, "quantity": qty} for sku, qty in totals.items()]


def _grn_item(sku: str, qty: float, expiry: str | None, mfg: str | None, batch_number: str) -> dict:
    expiry_uc = _to_uc_datetime(expiry)
    mfg_uc = _to_uc_datetime(mfg)
    return {
        "skuCode": sku,
        "quantity": qty,
        "unitPrice": 1,
        "manufacturingDate": mfg_uc,
        "expiry": expiry_uc,
        "wsBatchDetail": {
            "wsBatchGroupFieldValue": {
                "vendorBatchNumber": batch_number or "NA",
                "expiryDate": expiry_uc,
                "mfd": mfg_uc,
            },
        },
    }


def build_grn_items(line_items: list[dict]) -> list[dict]:
    """One entry per SAP batch line (duplicated SKUs kept -- each batch is
    a separate GRN item), using batch number/expiry/mfg date straight from
    the SAP transfer's own BatchNumbers -- no separate UC lookup needed,
    unlike the Frappe source's InventorySync (which does need one, see
    inventory_sync.py's docstring for why that case differs)."""
    items: list[dict] = []
    for line in line_items:
        sku = str(line.get("ItemCode") or "").strip()
        if not sku:
            continue
        batches = line.get("BatchNumbers") or []
        if not batches:
            qty = float(line.get("Quantity") or 0)
            if qty > 0:
                items.append(_grn_item(sku, qty, None, None, ""))
            continue
        for b in batches:
            qty = float(b.get("Quantity") or 0)
            if qty > 0:
                items.append(_grn_item(sku, qty, b.get("ExpiryDate"), b.get("ManufacturingDate"), b.get("BatchNumber") or ""))
    return items


def round_robin_grn_rounds(grn_items: list[dict]) -> list[list[dict]]:
    """UC forbids duplicate SKUs in one GRN -- split into rounds, each
    with at most one entry per SKU. Mirrors the Frappe source's
    _push_grn_rounds batching exactly, capped at MAX_GRN_ROUNDS."""
    remaining = list(grn_items)
    rounds: list[list[dict]] = []
    for _ in range(MAX_GRN_ROUNDS):
        if not remaining:
            break
        seen: set[str] = set()
        this_round: list[dict] = []
        leftover: list[dict] = []
        for entry in remaining:
            sku = entry["skuCode"]
            if sku not in seen:
                seen.add(sku)
                this_round.append(entry)
            else:
                leftover.append(entry)
        rounds.append(this_round)
        remaining = leftover
    return rounds


def _uc_client() -> UcClient:
    uc = UcClient(get_uc_credentials())
    uc.login()
    return uc


def _as_list(value) -> list:
    if isinstance(value, str):
        return json.loads(value) if value else []
    return value or []


def fetch_new_transfers(conn, limit: int = 50) -> int:
    """Poll SAP past the watermark, stage any transfer with a line into a
    watched destination warehouse. Watermark advances past every DocEntry
    evaluated (most SAP transfers aren't ours), regardless of outcome --
    a staged-but-failed transfer is retried via recover_failed_pogrn, not
    by the polling cursor re-seeing it."""
    watermark = int(store.get_watermark(conn, "pogrn_transfers") or 0)
    watched = watched_warehouses()
    transfers = stock_transfers.list_since(watermark, limit=limit)
    staged = 0
    highest = watermark
    for summary in transfers:
        doc_entry = int(summary["DocEntry"])
        highest = max(highest, doc_entry)
        if not stock_transfers.inward_lines(summary, watched):
            continue
        full = stock_transfers.detail(doc_entry)
        lines = stock_transfers.inward_lines(full, watched)
        if not lines:
            continue
        from_wh = str(lines[0].get("FromWarehouseCode") or "")
        to_wh = str(lines[0].get("WarehouseCode") or "")
        if store.stage_pogrn_transfer(conn, doc_entry, from_wh, to_wh, lines):
            staged += 1
    if transfers:
        store.set_watermark(conn, "pogrn_transfers", str(highest))
    return staged


def _process_claimed(conn, claimed: dict, uc: UcClient | None, settings: UnicommerceSettings, dry_run: bool) -> dict:
    sap_doc_entry = claimed["sap_doc_entry"]
    line_items = _as_list(claimed["line_items"])
    to_warehouse = str(line_items[0].get("WarehouseCode") or "") if line_items else str(claimed.get("to_warehouse") or "")
    facility = facility_code_for_warehouse(settings, to_warehouse)
    if not facility:
        raise ValueError(f"No UC facility mapping for SAP warehouse {to_warehouse!r} (check settings.warehouse_map)")

    uc_po_id = claimed.get("uc_po_id") or ""
    po_items = aggregate_po_items(line_items)
    if not uc_po_id and not po_items:
        raise ValueError("No valid SKU/quantity in transfer line items")

    already_grn_ids = _as_list(claimed.get("uc_grn_ids"))
    grn_items = build_grn_items(line_items)
    rounds = round_robin_grn_rounds(grn_items)

    if dry_run:
        # Always preview both the PO and the full GRN round plan together,
        # regardless of whether uc_po_id is already set -- a reviewer needs
        # the whole picture before approving a real run (Phase B), not just
        # whichever half happens to still be pending.
        preview: dict = {"sap_doc_entry": sap_doc_entry, "status": "dry_run", "facility": facility, "would_create_grn_rounds": rounds}
        if uc_po_id:
            preview["uc_po_id"] = uc_po_id
        else:
            preview["would_create_po"] = {"vendorCode": settings.vendor_code, "purchaseOrderItems": po_items}
        return preview

    if not uc_po_id:
        po_resp = uc.create_po_approved(settings.vendor_code, po_items, facility_code=facility)
        uc_po_id = po_resp.get("purchaseOrderCode")
        if not uc_po_id:
            raise UcApiError(f"PO creation returned no purchaseOrderCode: {po_resp}")
        store.mark_pogrn_po_created(conn, sap_doc_entry, uc_po_id)

    date_compact = datetime.now(timezone.utc).strftime("%Y%m%d")
    invoice_date_uc = _to_uc_datetime(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    for round_num, round_items in enumerate(rounds[len(already_grn_ids):], start=len(already_grn_ids)):
        invoice_number = f"GRN-{sap_doc_entry}-{date_compact}-R{round_num + 1}"[:50]
        grn_resp = uc.create_inflow_receipt(uc_po_id, invoice_number, invoice_date_uc, facility_code=facility)
        grn_code = grn_resp.get("inflowReceiptCode")
        if not grn_code:
            raise UcApiError(f"GRN creation returned no inflowReceiptCode (round {round_num + 1}): {grn_resp}")
        for item in round_items:
            uc.add_grn_item(grn_code, item, facility_code=facility)
        store.append_pogrn_grn_id(conn, sap_doc_entry, grn_code)

    store.mark_pogrn_done(conn, sap_doc_entry)
    return {"sap_doc_entry": sap_doc_entry, "status": "grn_done", "facility": facility, "uc_po_id": uc_po_id}


def process_ready_transfers(conn, limit: int = 20, dry_run: bool = False) -> dict:
    settings = get_settings()
    staged = fetch_new_transfers(conn, limit=limit)
    recovery = store.recover_failed_pogrn(conn, limit=limit, max_attempts=MAX_ATTEMPTS, max_hours=MAX_HOURS)

    doc_entries = store.get_claimable_pogrn_doc_entries(conn, limit)
    processed = done = failed = 0

    if dry_run:
        # Preview only -- must never claim. claim_pogrn moves a row to
        # 'processing' with no matching mark_*/release call in dry_run
        # mode, which would permanently block any real future run from
        # claiming it (found live: a dry_run call left DocEntry 53974
        # stuck at 'processing' forever).
        results = []
        for doc_entry in doc_entries:
            row = store.get_pogrn_transfer(conn, doc_entry)
            if not row:
                continue
            processed += 1
            results.append(_process_claimed(conn, row, uc=None, settings=settings, dry_run=True))
        return {
            "staged": staged, "recovered": recovery["recovered"], "escalated_dead": recovery["escalated"],
            "processed": processed, "previews": results,
        }

    uc = _uc_client()
    for doc_entry in doc_entries:
        claimed = store.claim_pogrn(conn, doc_entry)
        if not claimed:
            continue
        processed += 1
        try:
            result = _process_claimed(conn, claimed, uc, settings, dry_run=False)
            if result.get("status") == "grn_done":
                done += 1
        except Exception as e:
            failed += 1
            store.mark_pogrn_failed(conn, doc_entry, str(e), MAX_ATTEMPTS)
            logger.error("pogrn: transfer %s crashed: %s", doc_entry, e, exc_info=True)

    return {
        "staged": staged, "recovered": recovery["recovered"], "escalated_dead": recovery["escalated"],
        "processed": processed, "done": done, "failed": failed,
    }


def process_one_transfer(conn, sap_doc_entry: int, dry_run: bool = False) -> dict:
    """Manual single-transfer retry/dry-run, bypassing the watermark."""
    settings = get_settings()
    existing = store.get_pogrn_transfer(conn, sap_doc_entry)
    if not existing:
        watched = watched_warehouses()
        full = stock_transfers.detail(sap_doc_entry)
        lines = stock_transfers.inward_lines(full, watched)
        if not lines:
            return {"status": "not_applicable", "sap_doc_entry": sap_doc_entry, "message": "No line in a watched destination warehouse"}
        from_wh = str(lines[0].get("FromWarehouseCode") or "")
        to_wh = str(lines[0].get("WarehouseCode") or "")
        store.stage_pogrn_transfer(conn, sap_doc_entry, from_wh, to_wh, lines)
        existing = store.get_pogrn_transfer(conn, sap_doc_entry)

    if dry_run:
        # Preview only -- reads the current row directly, never claims.
        return _process_claimed(conn, existing, uc=None, settings=settings, dry_run=True)

    claimed = store.claim_pogrn(conn, sap_doc_entry)
    if not claimed:
        return {"status": "already_claimed_or_done", "sap_doc_entry": sap_doc_entry}

    uc = _uc_client()
    try:
        return _process_claimed(conn, claimed, uc, settings, dry_run=False)
    except Exception as e:
        status = store.mark_pogrn_failed(conn, sap_doc_entry, str(e), MAX_ATTEMPTS)
        return {"status": status, "sap_doc_entry": sap_doc_entry, "error": str(e)}
