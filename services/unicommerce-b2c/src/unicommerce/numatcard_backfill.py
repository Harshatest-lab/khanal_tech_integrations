"""One-off: backfill SAP's native NumAtCard header field onto already-posted
AR Invoices in unicommerce-b2c, so they become matchable via the same
$filter=NumAtCard pattern amazon-us-b2c's dedup uses (see
sap_client.set_invoice_num_at_card). NumAtCard is unset on every historical
invoice this service has ever posted.

Runs as a Lambda (handlers.numatcard_backfill_handler), not locally --
confirmed 2026-08-20 that requests to SAP Service Layer from outside AWS
(this repo's dev machine) consistently hit "Error while connecting to
database server HDB@10.10.40.135:30013" while the real scheduled
unicommercePostInvoices Lambda kept posting successfully throughout the same
window (zero occurrences of that error in its logs) -- most likely SAP's
Service Layer routes requests to different backend workers depending on
source network path, and only the AWS-reachable path has a healthy HANA
connection right now. Batched (limit/offset) to stay well inside the Lambda
timeout -- two SAP round-trips per invoice (fetch + verify)."""

from __future__ import annotations

import logging

from unicommerce import sap_client, store

logger = logging.getLogger(__name__)


def fetch_candidates(limit: int, offset: int) -> list[tuple[str, int]]:
    conn = store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT uniware_id, sap_ar_invoice_docentry
                FROM unicommerce_orders
                WHERE pipeline_status = 'posted'
                  AND sap_ar_invoice_docentry IS NOT NULL
                  AND sap_ar_invoice_docentry != ''
                ORDER BY order_date ASC, uniware_id ASC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [(uniware_id, int(doc_entry)) for uniware_id, doc_entry in rows]


def run_backfill(limit: int = 10, offset: int = 0, apply: bool = False) -> dict:
    candidates = fetch_candidates(limit, offset)
    ok = skipped = failed = 0
    details: list[str] = []

    for uniware_id, doc_entry in candidates:
        try:
            inv = sap_client.get_invoice_by_doc_entry(doc_entry)
        except Exception as e:
            details.append(f"FAIL  {uniware_id} DocEntry={doc_entry}: could not fetch invoice ({e})")
            failed += 1
            continue

        current_num_at_card = inv.get("NumAtCard") or ""
        cancel_status = inv.get("CancelStatus") or ""

        if cancel_status == "csYes":
            details.append(f"SKIP  {uniware_id} DocEntry={doc_entry}: invoice is cancelled")
            skipped += 1
            continue

        if current_num_at_card == uniware_id:
            details.append(f"SKIP  {uniware_id} DocEntry={doc_entry}: NumAtCard already set correctly")
            skipped += 1
            continue

        if current_num_at_card:
            details.append(
                f"FAIL  {uniware_id} DocEntry={doc_entry}: NumAtCard already holds a different value "
                f"({current_num_at_card!r}) -- not overwriting"
            )
            failed += 1
            continue

        if not apply:
            details.append(f"PLAN  {uniware_id} DocEntry={doc_entry}: would set NumAtCard = {uniware_id!r}")
            ok += 1
            continue

        try:
            sap_client.set_invoice_num_at_card(doc_entry, uniware_id)
            verify = sap_client.get_invoice_by_doc_entry(doc_entry)
            if verify.get("NumAtCard") != uniware_id:
                details.append(
                    f"FAIL  {uniware_id} DocEntry={doc_entry}: patched but verify read back {verify.get('NumAtCard')!r}"
                )
                failed += 1
                continue
            details.append(f"DONE  {uniware_id} DocEntry={doc_entry}: NumAtCard set + verified")
            ok += 1
        except Exception as e:
            details.append(f"FAIL  {uniware_id} DocEntry={doc_entry}: patch failed ({e})")
            failed += 1

    for line in details:
        logger.info(line)

    return {
        "mode": "APPLY" if apply else "DRY-RUN",
        "ok": ok,
        "skipped": skipped,
        "failed": failed,
        "total": len(candidates),
        "limit": limit,
        "offset": offset,
        "details": details,
    }
