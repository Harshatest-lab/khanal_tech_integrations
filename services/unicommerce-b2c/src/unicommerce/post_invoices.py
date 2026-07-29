"""Post staged Unicommerce Orders to SAP B1 as AR Invoices.

Ported from khanal_tech_integrations/unicommerce/workers/invoice.py.
Scope note: the Frappe source's "EC-FG FEFO rescue" (a settings-gated
fallback that re-tries FEFO allocation across all of EC-FG's batches when
the UC-specified batch is unavailable there) is NOT ported yet -- it's an
optimization that reduces false batch_holds, not a correctness
requirement (without it, some orders land in batch_hold slightly more
often than Frappe would). Documented here as a deliberate scope decision,
not an oversight; port once the core loop is proven in the dual-run.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from unicommerce import hana_access, sap_client, sap_payload, store
from unicommerce.mappers import normalize_text
from unicommerce.settings import get_settings

logger = logging.getLogger(__name__)

BATCH_HOLD_MAX_ATTEMPTS = 10
BATCH_HOLD_MAX_HOURS = 48
# Must match sap_payload.BATCH_QTY_TOLERANCE.
BATCH_QTY_TOLERANCE = 0.01


# ── In-memory reservation ledger ──────────────────────────────────────────
# Scoped to a single posting run (one Lambda invocation). Prevents two
# orders in the SAME run both allocating the same batch quantity before
# either actually posts -- the FEFO row snapshot each order's batch check
# sees is shared, so without this, two orders needing the same scarce
# batch could both pass their individual check and only one's SAP post
# would actually be backed by real stock. Frappe's source also persists
# this to a SQL audit table (`__uc_batch_reservation_ledger`); that part
# is NOT ported -- it's an audit trail, not a functional requirement, and
# CloudWatch logs serve the same "what happened" purpose for now.

def new_reservation_context() -> dict:
    return {"reserved_by_key": {}, "allocs_by_order": {}}


def _reserved_qty(ctx: dict, key: tuple) -> float:
    return float(ctx["reserved_by_key"].get(key) or 0.0)


def _hold_for_order(ctx: dict, uniware_id: str, allocations: list[dict]) -> None:
    if not allocations:
        return
    ctx["allocs_by_order"][uniware_id] = allocations
    for row in allocations:
        key = (row["item_code"], row["batch_number"], row["warehouse_code"])
        ctx["reserved_by_key"][key] = float(ctx["reserved_by_key"].get(key) or 0) + float(row["qty"])


def _release_for_order(ctx: dict, uniware_id: str) -> None:
    rows = ctx["allocs_by_order"].pop(uniware_id, [])
    for row in rows:
        key = (row["item_code"], row["batch_number"], row["warehouse_code"])
        ctx["reserved_by_key"][key] = max(0.0, float(ctx["reserved_by_key"].get(key) or 0) - float(row["qty"]))


def _consume_for_order(ctx: dict, uniware_id: str) -> None:
    ctx["allocs_by_order"].pop(uniware_id, None)


# ── Pre-warm: one bulk HANA query per warehouse for the whole run ────────

def _prewarm_caches(orders: list[dict], settings) -> tuple[dict, dict]:
    """Returns (batch_managed_cache, fefo_cache): fefo_cache is
    {(item_code, wh): [{"batch_number","quantity","expiry"}, ...]}."""
    wh_items: dict[str, set[str]] = {}
    all_item_codes: set[str] = set()
    for row in orders:
        try:
            uc_data = json.loads(row["order_json"] or "{}") if isinstance(row["order_json"], str) else (row["order_json"] or {})
            items = sap_payload.parse_line_items(uc_data)
            channel = row.get("channel_name") or ""
            for item in items:
                item_code = str(item.get("itemSku") or "").strip()
                if not item_code:
                    continue
                all_item_codes.add(item_code)
                sap_wh = sap_payload._resolve_sap_warehouse(settings, channel, item.get("warehouseCode", ""))
                if sap_wh:
                    wh_items.setdefault(sap_wh, set()).add(item_code)
        except Exception:
            continue

    fefo_cache: dict = {}
    for wh, item_codes in wh_items.items():
        rows = hana_access.bulk_batch_inventory_by_warehouse(wh, list(item_codes))
        for r in rows:
            key = (r["item_code"], r["warehouse_code"])
            fefo_cache.setdefault(key, []).append(
                {"batch_number": r["batch_number"], "quantity": r["quantity"], "expiry": r.get("expiry")}
            )

    batch_managed_cache = hana_access.bulk_manage_batch_numbers(all_item_codes)
    return batch_managed_cache, fefo_cache


def _check_batches(
    uniware_id: str,
    channel_name: str,
    uc_data: dict,
    settings,
    batch_managed_cache: dict,
    fefo_cache: dict,
    reservation_ctx: dict,
) -> dict:
    """Validate all batch-tracked lines have sufficient quantity.
    Returns {"ok": bool, "missing": [...]}. Reuses the same fefo_cache the
    caller must also pass into sap_payload.ar_invoice_payload() -- both
    call fefo_allocate_from_rows() against identical, immutable rows for
    the same (item_code, qty), so the allocation this validates is exactly
    what the payload builder reconstructs (no separate handoff needed)."""
    items = sap_payload.parse_line_items(uc_data)
    aggregated: dict[tuple, dict] = {}
    shortages: list[dict] = []

    for item in items:
        batch_num = item.get("batchNumber")
        item_code = item.get("itemSku")
        qty_needed = float(item.get("quantity", 0) or 0)
        if not item_code or qty_needed <= 0:
            continue
        uc_wh = item.get("warehouseCode", "")
        sap_wh = sap_payload._resolve_sap_warehouse(settings, channel_name, uc_wh)

        if not batch_num:
            managed = batch_managed_cache.get(item_code)
            if managed is None or not managed:
                # None -> SAP lookup failed for this item, skip allocation
                # (payload builder does the same via the same cache, so
                # both layers agree). False -> genuinely not batch-managed.
                continue

            fefo_rows = fefo_cache.get((item_code, sap_wh), [])
            allocations = sap_payload.fefo_allocate_from_rows(fefo_rows, qty_needed)
            if allocations:
                for alloc in allocations:
                    key = (item_code, alloc["BatchNumber"], sap_wh)
                    aggregated.setdefault(
                        key, {"item_code": item_code, "batch_number": alloc["BatchNumber"], "warehouse_code": sap_wh, "qty_needed": 0.0}
                    )
                    aggregated[key]["qty_needed"] += float(alloc["Quantity"])
                continue

            sap_avail = round(sum(float(r.get("quantity") or 0) for r in fefo_rows), 3)
            shortages.append(
                {
                    "item_code": item_code, "batch_number": "", "warehouse_code": sap_wh,
                    "qty_needed": round(qty_needed, 3), "qty_available": sap_avail,
                    "shortage": round(max(0.0, qty_needed - sap_avail), 3),
                    "reason": "insufficient_sap_batch_qty" if fefo_rows else "no_sap_batch_available",
                }
            )
            continue

        key = (item_code, batch_num, sap_wh)
        aggregated.setdefault(key, {"item_code": item_code, "batch_number": batch_num, "warehouse_code": sap_wh, "qty_needed": 0.0})
        aggregated[key]["qty_needed"] += qty_needed

    fefo_overrides: dict = {}
    for requirement in aggregated.values():
        cache_key = (requirement["item_code"], requirement["batch_number"], requirement["warehouse_code"])
        rows_for_item = fefo_cache.get((requirement["item_code"], requirement["warehouse_code"]), [])
        available = sum(float(r.get("quantity") or 0) for r in rows_for_item if r.get("batch_number") == requirement["batch_number"])
        reserved_qty = _reserved_qty(reservation_ctx, cache_key)
        effective_available = max(0.0, available - reserved_qty)
        qty_needed = float(requirement["qty_needed"] or 0)
        if effective_available < (qty_needed - BATCH_QTY_TOLERANCE):
            shortage_reason = "reservation_conflict" if reserved_qty > 0 else "insufficient_batch_qty"
            shortages.append(
                {
                    "item_code": requirement["item_code"], "batch_number": requirement["batch_number"],
                    "warehouse_code": requirement["warehouse_code"], "qty_needed": round(qty_needed, 3),
                    "qty_available": round(effective_available, 3), "shortage": round(max(0.0, qty_needed - effective_available), 3),
                    "reserved_qty": round(reserved_qty, 3), "reason": shortage_reason,
                }
            )

    if shortages:
        return {"ok": False, "missing": shortages}

    reservation_rows = [
        {"item_code": r["item_code"], "batch_number": r["batch_number"], "warehouse_code": r["warehouse_code"], "qty": float(r["qty_needed"] or 0)}
        for r in aggregated.values()
    ]
    _hold_for_order(reservation_ctx, uniware_id, reservation_rows)
    return {"ok": True, "missing": []}


def _batch_shortage_message(shortages: list[dict]) -> str:
    if not shortages:
        return "Batch quantity not available in SAP"
    reason_map = {"insufficient_batch_qty": "insufficient qty", "reservation_conflict": "reserved by earlier order"}
    lines = []
    for s in shortages[:5]:
        reason_msg = reason_map.get(str(s.get("reason") or ""), "batch check failed")
        shortage_val = s.get("shortage") or max(0.0, float(s.get("qty_needed") or 0) - float(s.get("qty_available") or 0))
        lines.append(
            f"{s['item_code']}::{s['batch_number']} [{s['warehouse_code']}]: "
            f"need {s['qty_needed']} have {s['qty_available']} short {round(shortage_val, 3)} ({reason_msg})"
        )
    suffix = f" (+{len(shortages) - 5} more)" if len(shortages) > 5 else ""
    return " | ".join(lines) + suffix


def _post_order(conn, row: dict, settings, batch_managed_cache: dict, fefo_cache: dict, series_cache: dict, reservation_ctx: dict) -> tuple[str, list]:
    """Returns (outcome, shortages) where outcome is 'posted' | 'batch_hold' | 'verify'."""
    uniware_id = row["uniware_id"]

    # Idempotency guard: if SAP already has an invoice for this order (a
    # prior attempt that timed out before we could record the docentry),
    # mark it posted immediately instead of re-posting.
    try:
        existing = sap_client.find_invoice_by_uc_order_id(uniware_id)
        if existing:
            store.mark_posted(conn, uniware_id, str(existing["doc_entry"]), str(existing["doc_num"]), existing.get("doc_tax_amount") or 0, None)
            logger.info("_post_order(%s): SAP invoice already exists (DocEntry=%s); skipping re-post.", uniware_id, existing["doc_entry"])
            return "posted", []
    except Exception:
        # If the lookup itself fails, proceed with posting -- worst case
        # is a duplicate that verify_posted_orders catches next cycle.
        # Suppressing here avoids blocking the whole run on a transient
        # SAP outage.
        logger.warning("_post_order(%s): idempotency check failed (SAP unreachable?); proceeding with post", uniware_id, exc_info=True)

    uc_data = json.loads(row["order_json"] or "{}") if isinstance(row["order_json"], str) else (row["order_json"] or {})
    channel_name = row.get("channel_name") or ""

    batch_check = _check_batches(uniware_id, channel_name, uc_data, settings, batch_managed_cache, fefo_cache, reservation_ctx)
    if not batch_check["ok"]:
        store.mark_batch_hold(conn, uniware_id, _batch_shortage_message(batch_check["missing"]))
        return "batch_hold", batch_check["missing"]

    order_date = row.get("order_date")
    order_date_str = order_date.isoformat() if hasattr(order_date, "isoformat") else str(order_date)
    order_json_str = row["order_json"] if isinstance(row["order_json"], str) else json.dumps(row["order_json"], default=str)

    try:
        payload, tax_expected = sap_payload.ar_invoice_payload(
            uniware_id=uniware_id, channel=channel_name, order_json=order_json_str, uc_data=uc_data,
            order_date=order_date_str, settings=settings, batch_managed_cache=batch_managed_cache,
            fefo_rows_cache=fefo_cache,
        )
    except Exception as e:
        _release_for_order(reservation_ctx, uniware_id)
        store.mark_failed(conn, uniware_id, str(e), settings.max_retry_attempts)
        return "failed", []

    try:
        result, posted_payload = sap_client.post_invoice_with_series_fallback(payload, series_cache)
    except Exception as e:
        err = str(e)
        _release_for_order(reservation_ctx, uniware_id)
        if "timeout" in err.lower() or "connection" in err.lower():
            store.unclaim_order(conn, uniware_id, "verify")
            return "verify", []
        store.mark_failed(conn, uniware_id, err, settings.max_retry_attempts)
        return "failed", []

    _consume_for_order(reservation_ctx, uniware_id)

    doc_entry = result.get("doc_entry")
    doc_num = result.get("doc_num")
    tax_actual = float(result.get("response", {}).get("DocTaxAmount") or 0)

    invoice_detail = None
    if doc_entry:
        try:
            invoice_detail = sap_client.get_invoice_by_doc_entry(doc_entry)
        except Exception:
            logger.warning("_post_order(%s): could not fetch invoice detail after posting; recon will lag", uniware_id, exc_info=True)

    store.mark_posted(conn, uniware_id, str(doc_entry), str(doc_num), tax_actual, posted_payload)

    if invoice_detail:
        payload_lines = posted_payload.get("DocumentLines", [])
        invoice_lines = invoice_detail.get("DocumentLines", [])
        expected_unit = sap_payload.sum_unit_price_total(payload_lines)
        actual_unit = sap_payload.sum_unit_price_total(invoice_lines)
        actual_tax_from_lines = sap_payload.sum_tax_total(invoice_lines)
        actual_tax = actual_tax_from_lines if actual_tax_from_lines else tax_actual
        store.update_reconciliation_diffs(conn, uniware_id, tax_expected, actual_tax, abs(expected_unit - actual_unit), expected_unit, actual_unit)

    return "posted", []


def post_ready_orders(
    conn, limit: int = 50, channel_name: str | None = None, start_date: str | None = None, end_date: str | None = None
) -> dict:
    from unicommerce import fetch_sync

    settings = get_settings()

    series_ok, series_msg = sap_client.validate_active_series(settings)
    if not series_ok:
        logger.error("post_invoices series guard ABORTED: %s", series_msg)
        return {"processed": 0, "posted": 0, "batch_hold": 0, "verify": 0, "failed": 0, "abort_reason": series_msg}
    logger.info("post_invoices series guard: %s", series_msg)

    # First step of every posting run (matches Frappe workers/invoice.py):
    # freshly-fetched orders sit at 'fetched' until this promotes the
    # invoice-eligible ones to 'ready' -- without it the loop below sees
    # nothing to post.
    promoted = fetch_sync.promote_fetched_to_ready(conn, limit=limit, start_date=start_date, end_date=end_date)

    store.recover_stuck_posting(conn, settings.posting_timeout_mins)
    recovered_failed = store.recover_failed_orders(conn, limit)
    batch_recovery = store.recover_batch_hold_orders(conn, limit, BATCH_HOLD_MAX_ATTEMPTS, BATCH_HOLD_MAX_HOURS)
    verify_stats = _verify_posted_orders(conn, limit, settings)

    reservation_ctx = new_reservation_context()
    series_cache: dict = {}

    orders = store.get_ready_orders(conn, limit, channel_name=channel_name)
    batch_managed_cache, fefo_cache = _prewarm_caches(orders, settings)

    processed = posted = batch_hold = verify = failed = skipped_claimed = 0
    for row in orders:
        if not store.claim_ready_order(conn, row["uniware_id"]):
            skipped_claimed += 1
            continue
        processed += 1
        try:
            outcome, _shortages = _post_order(conn, row, settings, batch_managed_cache, fefo_cache, series_cache, reservation_ctx)
            if outcome == "posted":
                posted += 1
            elif outcome == "batch_hold":
                batch_hold += 1
            elif outcome == "verify":
                verify += 1
            elif outcome == "failed":
                failed += 1
        except Exception as e:
            failed += 1
            _release_for_order(reservation_ctx, row["uniware_id"])
            store.mark_failed(conn, row["uniware_id"], str(e), settings.max_retry_attempts)
            logger.error("post_ready_orders: order %s crashed: %s", row["uniware_id"], e, exc_info=True)

    return {
        "promoted_to_ready": promoted["moved_to_ready"],
        "deferred_fetched": promoted["deferred"],
        "recovered_failed": recovered_failed,
        "recovered_batch_hold": batch_recovery.get("recovered", 0),
        "escalated_batch_hold": batch_recovery.get("escalated", 0),
        "verify_checked": verify_stats["checked"], "verify_posted": verify_stats["posted"],
        "verify_requeued": verify_stats["requeued"], "verify_dead": verify_stats["dead"],
        "processed": processed, "posted": posted, "batch_hold": batch_hold,
        "verify": verify, "failed": failed, "skipped_claimed": skipped_claimed,
    }


def _verify_posted_orders(conn, limit: int, settings) -> dict:
    orders = store.get_verify_orders(conn, limit)
    checked = posted = requeued = dead = 0
    for row in orders:
        uniware_id = row["uniware_id"]
        checked += 1
        try:
            existing = sap_client.find_invoice_by_uc_order_id(uniware_id)
            if existing:
                store.mark_posted(conn, uniware_id, str(existing["doc_entry"]), str(existing["doc_num"]), existing.get("doc_tax_amount") or 0, None)
                posted += 1
            else:
                outcome = store.requeue_or_dead_from_verify(conn, uniware_id, settings.max_retry_attempts, "Invoice not found during verify")
                dead += 1 if outcome == "dead" else 0
                requeued += 1 if outcome == "ready" else 0
        except Exception as e:
            outcome = store.requeue_or_dead_from_verify(conn, uniware_id, settings.max_retry_attempts, f"Verify error: {str(e)[:950]}")
            dead += 1 if outcome == "dead" else 0
            requeued += 1 if outcome == "ready" else 0
    return {"checked": checked, "posted": posted, "requeued": requeued, "dead": dead}


def post_one_order(conn, uniware_id: str) -> dict:
    """Manual retry path: reset to 'ready' then claim + post through the
    same atomic path the scheduler uses (khanal_tech_integrations PR #1303
    fixed exactly this shortcut being skipped in the Frappe source's
    equivalent -- reproduce the fix here, not the original bug)."""
    settings = get_settings()

    series_ok, series_msg = sap_client.validate_active_series(settings)
    if not series_ok:
        logger.error("post_one_order(%s) series guard ABORTED: %s", uniware_id, series_msg)
        return {"status": "series_guard_failed", "uniware_id": uniware_id, "error": series_msg}

    store.unclaim_order(conn, uniware_id, "ready")
    if not store.claim_ready_order(conn, uniware_id):
        return {"status": "already_claimed", "uniware_id": uniware_id}

    with conn.cursor() as cur:
        cur.execute("SELECT uniware_id, channel_name, order_date, order_json, attempt_count FROM unicommerce_orders WHERE uniware_id = %s", (uniware_id,))
        cols = [d.name for d in cur.description]
        r = cur.fetchone()
        row = dict(zip(cols, r)) if r else None
    if row is None:
        return {"status": "not_found", "uniware_id": uniware_id}

    batch_managed_cache, fefo_cache = _prewarm_caches([row], settings)
    reservation_ctx = new_reservation_context()
    series_cache: dict = {}
    try:
        outcome, shortages = _post_order(conn, row, settings, batch_managed_cache, fefo_cache, series_cache, reservation_ctx)
        return {"status": outcome, "uniware_id": uniware_id, "shortages": shortages}
    except Exception as e:
        store.mark_failed(conn, uniware_id, str(e), settings.max_retry_attempts)
        return {"status": "failed", "uniware_id": uniware_id, "error": str(e)[:500]}
