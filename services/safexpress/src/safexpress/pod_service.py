"""Orchestrates one polling pass: HANA candidates -> SafeExpress tracking
-> POD download/upload -> SAP PATCH -> Postgres work-queue update.

This is the direct replacement for safexpress2/pod_service.py — same
two-phase-commit discipline (persist the S3 URL before touching SAP, so a
network blip on the SAP PATCH never loses an already-uploaded POD), but
driven by a live HANA query instead of a local MariaDB replica.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

import psycopg

from . import client, hana_candidates, pod_upload, sap_client, store
from .config import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_LOOKBACK_DAYS,
    MAX_CONSECUTIVE_FAILURES,
    MAX_LOOKBACK_DAYS,
    MIN_RUN_BUDGET_SECONDS,
    POD_STATUS_COMPLETED,
    POD_STATUS_FAILED,
    POD_STATUS_NO_WAYBILL,
    POD_STATUS_PENDING_SAP,
    POD_STATUS_WAYBILL_FOUND,
    STALE_AFTER_DAYS,
)
from .exceptions import SafexpressPODNotFoundError, SafexpressWaybillNotFoundError
from .utils import parse_delivery_date

logger = logging.getLogger(__name__)


def lookback_days(configured: int, last_activity: datetime | None, now: datetime | None = None) -> int:
    """How far back discovery must look. Normally the small configured window; but
    if the pipeline has been silent longer than that (an outage, a broken deploy),
    invoices created while it was down fall outside the window and would never be
    found, so widen it to cover the whole silence. Capped at MAX_LOOKBACK_DAYS."""
    if last_activity is None:
        return min(configured, MAX_LOOKBACK_DAYS)
    now = now or datetime.now(timezone.utc)
    silent_days = (now - last_activity).days + 1
    return min(max(configured, silent_days), MAX_LOOKBACK_DAYS)


def poll_and_process(
    days: int = DEFAULT_LOOKBACK_DAYS, batch_size: int = DEFAULT_BATCH_SIZE, time_left: Callable[[], float] | None = None
) -> dict[str, Any]:
    """`days` only controls *discovery* of invoices new since the last run
    (a small rolling HANA window) — it no longer bounds what gets retried.
    What actually gets worked each run is the Postgres backlog (every open
    row, not just freshly-discovered ones), pulled mostly newest-invoice
    -first with a small oldest-first reserve (store.list_open_candidates),
    capped at `batch_size` rows so one invocation can't run past the Lambda
    timeout regardless of how large the backlog is. See
    docs/architecture/PLATFORM_ARCHITECTURE.md §5 for why: a rolling
    discovery-only window used to also gate retries, so an invoice whose
    waybill/POD showed up after the window closed was silently dropped
    forever.

    `time_left` returns the seconds the Lambda has left; when it runs low the
    loop stops starting new invoices (see MIN_RUN_BUDGET_SECONDS) and the next
    run carries on."""
    stats = {"days": 0, "discovered": 0, "backlog": 0, "processed": 0, "stale": 0, "completed": 0,
              "pending_sap": 0, "waybill_found": 0, "no_waybill": 0, "failed": 0, "out_of_time": False, "stopped_on_errors": False, "errors": []}

    with store.connect() as conn:
        stats["stale"] = store.mark_stale_if_expired(conn, STALE_AFTER_DAYS)
        if stats["stale"]:
            logger.info("Marked %d row(s) stale (open past the %d-day cutoff)", stats["stale"], STALE_AFTER_DAYS)

        days = lookback_days(min(int(days), MAX_LOOKBACK_DAYS), store.last_activity(conn))
        stats["days"] = days
        discovered = hana_candidates.find_candidates(days)
        stats["discovered"] = len(discovered)
        for c in discovered:
            store.upsert_candidate(conn, c.doc_entry, c.doc_num, c.series_name)

        backlog = store.list_open_candidates(conn, limit=batch_size)
        stats["backlog"] = len(backlog)

        consecutive_failures = 0
        for row in backlog:
            if time_left is not None and time_left() < MIN_RUN_BUDGET_SECONDS:
                stats["out_of_time"] = True
                logger.warning("Stopping with %d of %d backlog row(s) untouched: Lambda time is nearly up", len(backlog) - stats["processed"], len(backlog))
                break
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # Several unrelated invoices failing in a row means Safexpress or SAP is down, not that these
                # invoices are bad. Stop, so the outage doesn't use up every row's retries; the next run tries again.
                stats["stopped_on_errors"] = True
                logger.error("Stopping after %d failures in a row; looks like an outage", consecutive_failures)
                break
            doc_entry = row["doc_entry"]
            stats["processed"] += 1
            try:
                result = process_one_invoice(conn, doc_entry, row["doc_num"], row["series_name"])
                stats[result] = stats.get(result, 0) + 1
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                logger.error("Unhandled error processing DocEntry %s: %s", doc_entry, e, exc_info=True)
                store.mark_failed(conn, doc_entry, str(e), POD_STATUS_FAILED)
                stats["failed"] += 1
                stats["errors"].append({"doc_entry": doc_entry, "error": str(e)[:300]})

        logger.info("Processed %d/%d backlog row(s)", stats["processed"], stats["backlog"])

    return stats


def seed_backfill() -> dict[str, Any]:
    """One-time go-live cutover step (mode=backfill, invoked manually
    once): an unbounded HANA scan for every currently-untracked SafeExpress
    invoice, regardless of age, seeded straight into the Postgres backlog.
    Does not process anything itself — the next scheduled `poll` runs work
    the backlog down via list_open_candidates' LIFO-with-fairness order, a
    bounded number of rows per invocation, same as any other discovery."""
    candidates = hana_candidates.find_backfill_candidates()
    with store.connect() as conn:
        for c in candidates:
            store.upsert_candidate(conn, c.doc_entry, c.doc_num, c.series_name)
    logger.info("Backfill seeded %d candidate(s) into the backlog", len(candidates))
    return {"seeded": len(candidates)}


def retry_pending_sap(limit: int = DEFAULT_BATCH_SIZE) -> dict[str, Any]:
    """Retries SAP PATCH only, for rows that already have a POD uploaded
    but whose SAP write previously failed — no fresh HANA/SafeExpress
    calls, matching safexpress2's Pending_SAP fast-path."""
    stats = {"attempted": 0, "completed": 0, "failed": 0, "errors": []}
    with store.connect() as conn:
        rows = store.list_pending_sap(conn, limit=limit)
        stats["attempted"] = len(rows)
        for row in rows:
            try:
                sap_client.update_invoice_with_pod(
                    row["doc_entry"], row["waybill_number"], row["pod_url"],
                    str(row["pod_date"]) if row["pod_date"] else None,
                )
                store.mark_completed(conn, row["doc_entry"])
                stats["completed"] += 1
            except Exception as e:
                store.mark_failed(conn, row["doc_entry"], str(e), POD_STATUS_PENDING_SAP)
                stats["failed"] += 1
                stats["errors"].append({"doc_entry": row["doc_entry"], "error": str(e)[:300]})
    return stats


def process_one_invoice(conn: psycopg.Connection, doc_entry: int, doc_num: int, series_name: str) -> str:
    """Returns one of: completed | pending_sap | no_waybill | failed."""
    # Step 1: waybill
    try:
        tracking = client.get_tracking_by_invoice(series_name, str(doc_num))
    except SafexpressWaybillNotFoundError:
        store.set_waybill(conn, doc_entry, "", POD_STATUS_NO_WAYBILL)
        return "no_waybill"

    waybill_number = tracking["shipment"]["waybill"]
    store.set_waybill(conn, doc_entry, waybill_number, POD_STATUS_WAYBILL_FOUND)

    pod_date: str | None = None
    raw_date = tracking["shipment"].get("deliveryDate")
    if raw_date:
        try:
            pod_date = parse_delivery_date(raw_date)
        except Exception as e:
            logger.warning("Could not parse delivery date %r for DocEntry %s: %s", raw_date, doc_entry, e)

    # Step 2: POD download + upload
    series_for_filename = tracking.get("_successful_series") or series_name or "UNKNOWN"
    try:
        from .auth import get_pod_auth_headers, get_pod_base_url

        pod_data = client.get_pod_document(waybill_number, get_pod_base_url(), get_pod_auth_headers())
    except SafexpressPODNotFoundError:
        return "waybill_found"  # waybill exists but no POD yet — stays in the backlog, retried next run

    pod_url = pod_upload.upload_pod(pod_data, series_for_filename, doc_num, waybill_number)

    # Step 3: persist the upload BEFORE touching SAP (two-phase commit — a
    # SAP-side failure must never lose an already-uploaded POD).
    store.set_pod_uploaded(conn, doc_entry, pod_url, pod_date, POD_STATUS_PENDING_SAP)

    # Step 4: PATCH SAP
    try:
        sap_client.update_invoice_with_pod(doc_entry, waybill_number, pod_url, pod_date)
    except Exception as e:
        store.mark_failed(conn, doc_entry, str(e), POD_STATUS_PENDING_SAP)
        return "pending_sap"

    store.mark_completed(conn, doc_entry)
    return "completed"
