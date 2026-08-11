"""Post READY orders to SAP as AR Invoices.

Guarantees, in order of importance:
  1. No duplicates: SAP is checked by NumAtCard (CancelStatus csNo)
     before EVERY post. A failed check aborts the order's attempt — it
     is never treated as "not found".
  2. No wrong months: only orders with actual_ship_date on/after the
     posting cutoff are eligible (store.list_ready_orders enforces both).
  3. Atomic claims: ready -> posting via conditional UPDATE, so parallel
     invocations cannot double-post.
  4. HANA down -> order goes back to READY and the batch aborts; only a
     definitive business error (no stock, SAP rejection) marks FAILED.

dry_run computes payloads + duplicate checks but never claims, posts, or
mutates — safe to run alongside the live Frappe pipeline (parallel-run
validation before cutover).
"""

from __future__ import annotations

import logging

from amazon_us import constants, sap_client, store
from amazon_us.hana_access import (
    HanaUnavailableError,
    allocate_batches_fefo,
    get_bom_children,
    resolve_acct_code,
)
from amazon_us.invoice_payload import build_invoice_payload

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 200
MAX_ATTEMPTS = 3  # definitive-failure retries via the retry cron


def post_ready_orders(conn, limit: int = DEFAULT_LIMIT, dry_run: bool = False) -> dict:
    run_id = store.start_sync_log(conn, "post")
    try:
        mapping = store.get_sku_mapping(conn)
        ready = store.list_ready_orders(conn, constants.POSTING_CUTOFF_DATE, limit)
        logger.info("%s ready orders (limit %s, dry_run=%s)", len(ready), limit, dry_run)

        counts = {"total": len(ready), "posted": 0, "recovered": 0, "failed": 0, "skipped": 0}
        if dry_run:
            counts = _dry_run(conn, ready, mapping)
            store.finish_sync_log(conn, run_id, "success", counts)
            return {"status": "dry_run", **counts}

        for order in ready:
            oid = order["amazon_order_id"]
            if not store.claim(conn, oid, "ready", "posting"):
                continue
            if not _post_one_claimed(conn, order, mapping, counts):
                break

        store.finish_sync_log(conn, run_id, "success", counts)
        return {"status": "done", **counts}
    except Exception as e:
        store.finish_sync_log(conn, run_id, "error", error=str(e))
        raise


def post_one_order(conn, amazon_order_id: str, dry_run: bool = False) -> dict:
    """Manual pilot/ops helper: post exactly one order by id, independent
    of post_ready_orders' oldest-ship-date-first ordering. Not used by any
    scheduled cron."""
    mapping = store.get_sku_mapping(conn)
    order = store.get_order(conn, amazon_order_id)
    if order is None or order.get("stage") != "ready":
        return {
            "status": "done", "posted": 0, "recovered": 0, "failed": 0, "skipped": 1,
            "reason": "order not found or not in ready stage",
        }

    if dry_run:
        counts = _dry_run(conn, [order], mapping)
        return {"status": "dry_run", **counts}

    if not store.claim(conn, amazon_order_id, "ready", "posting"):
        return {
            "status": "done", "posted": 0, "recovered": 0, "failed": 0, "skipped": 1,
            "reason": "claim failed (concurrent worker)",
        }

    counts = {"posted": 0, "recovered": 0, "failed": 0, "skipped": 0}
    _post_one_claimed(conn, order, mapping, counts)
    return {"status": "done", **counts}


def _post_one_claimed(conn, order: dict, mapping: dict[str, str], counts: dict) -> bool:
    """Attempt to post one already-claimed ('posting') order, mutating
    counts in place. Returns False if the caller should abort the whole
    batch (HANA outage), True otherwise."""
    oid = order["amazon_order_id"]
    try:
        existing = sap_client.find_invoice_by_order_id(oid)
        if existing:
            store.mark_posted(conn, oid, existing["doc_entry"], existing["doc_num"])
            store.log_invoice_attempt(
                conn, oid, "recovered",
                doc_entry=existing["doc_entry"], doc_num=existing["doc_num"],
            )
            counts["recovered"] += 1
            logger.info("RECOVERED %s -> already in SAP DocEntry=%s", oid, existing["doc_entry"])
            return True

        payload, skipped = build_invoice_payload(order, mapping, allocate_batches_fefo, resolve_acct_code, get_bom_children)
        if payload is None:
            reason = "; ".join(str(s) for s in skipped)
            if any(s.get("reason") == "ship_date_missing" for s in skipped):
                # Not an error: the FBA sync hasn't covered it yet.
                # (list_ready_orders filters these out; belt-and-braces.)
                store.claim(conn, oid, "posting", "ready")
                counts["skipped"] += 1
                return True
            store.mark_failed(conn, oid, f"No postable lines: {reason}")
            store.log_invoice_attempt(conn, oid, "skipped", error=reason)
            counts["failed"] += 1
            return True

        result = sap_client.post_invoice(payload)
        store.mark_posted(conn, oid, result["doc_entry"], result["doc_num"])
        store.log_invoice_attempt(
            conn, oid, "posted",
            doc_entry=result["doc_entry"], doc_num=result["doc_num"],
            payload=payload, response=result["response"],
        )
        counts["posted"] += 1
        logger.info("OK %s -> DocEntry=%s DocNum=%s", oid, result["doc_entry"], result["doc_num"])
        return True

    except HanaUnavailableError as e:
        store.claim(conn, oid, "posting", "ready")
        logger.error("HANA unavailable: %s — aborting batch, orders stay ready", e)
        counts["aborted_reason"] = f"HANA unavailable: {e}"
        return False

    except sap_client.SapLookupError as e:
        # Could not prove the order is absent from SAP — do NOT post.
        store.claim(conn, oid, "posting", "ready")
        store.log_invoice_attempt(conn, oid, "skipped", error=str(e))
        counts["skipped"] += 1
        logger.error("Duplicate check failed for %s; left ready: %s", oid, e)
        return True

    except Exception as e:
        store.mark_failed(conn, oid, str(e))
        store.log_invoice_attempt(conn, oid, "failed", error=str(e))
        counts["failed"] += 1
        logger.error("FAIL %s: %s", oid, e)
        return True


def retry_failed_orders(conn, limit: int = 50) -> dict:
    """Move FAILED orders below MAX_ATTEMPTS back to READY (stock races
    self-heal; genuinely blocked orders stop retrying and stay visible)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE amazon_us_orders SET stage = 'ready', updated_at = now()
            WHERE amazon_order_id IN (
              SELECT amazon_order_id FROM amazon_us_orders
              WHERE stage = 'failed' AND attempt_count < %s
              ORDER BY updated_at ASC LIMIT %s
            )
            """,
            (MAX_ATTEMPTS, limit),
        )
        requeued = cur.rowcount
    conn.commit()
    if requeued:
        return post_ready_orders(conn, limit=limit)
    return {"status": "done", "requeued": 0}


def _dry_run(conn, ready: list[dict], mapping: dict[str, str]) -> dict:
    counts = {"total": len(ready), "would_post": 0, "already_in_sap": 0, "not_postable": 0, "errors": 0}
    for order in ready:
        oid = order["amazon_order_id"]
        try:
            existing = sap_client.find_invoice_by_order_id(oid)
            payload, skipped = build_invoice_payload(order, mapping, allocate_batches_fefo, resolve_acct_code, get_bom_children)
        except Exception as e:
            counts["errors"] += 1
            logger.error("dry-run error for %s: %s", oid, e)
            continue
        if existing:
            counts["already_in_sap"] += 1
        elif payload is None:
            counts["not_postable"] += 1
            logger.info("dry-run %s not postable: %s", oid, skipped)
        else:
            counts["would_post"] += 1
    return counts
