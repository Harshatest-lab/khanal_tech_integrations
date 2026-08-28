"""Read-only HANA queries: find AR invoices needing SafeExpress tracking.

This is the direct replacement for the legacy Frappe pipeline's
"replicate every matching invoice into MariaDB, then query the copy"
pattern (khanal_tech_integrations/utils/safexpress/automated_invoice_attach.py's
Fetching_from_Sap). No local invoice replica — every run queries SAP HANA
directly for exactly the candidates it needs, nothing more.

Uses khanal_shared.hana (pooled hdbcli, profile via HANA_PROFILE).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from khanal_shared.hana.pool import hana_connection

from .config import NON_SAFEXPRESS_TRANSPORTERS, SAP_TRANSPORTATION_CODE_SAFEXPRESS

logger = logging.getLogger(__name__)


@dataclass
class InvoiceCandidate:
    doc_entry: int
    doc_num: int
    series_name: str  # "" if unresolved — caller falls back to trying all configured prefixes
    update_date: str


def _run_candidate_query(days: int | None) -> list[InvoiceCandidate]:
    """AR invoices (OINV) marked for SafeExpress (TransportationCode = 1)
    with no tracking number yet (U_TrackingNo empty/null), excluding known
    non-SafeExpress transporters that share the same TransportationCode
    (see config.NON_SAFEXPRESS_TRANSPORTERS — this replaces the legacy
    code's local transporter_type column check, querying U_TN directly
    instead).

    `days=None` runs unbounded (no UpdateDate filter at all) — used once
    for the go-live backfill. `days=N` adds a rolling `UpdateDate >=
    today - N` bound — used for ongoing new-invoice discovery. Either way
    this only *discovers* candidates; whether a discovered invoice keeps
    getting retried afterward is entirely the Postgres table's job (see
    store.list_open_candidates / mark_stale_if_expired), not this query's.
    """
    exclude_placeholders = ", ".join("?" for _ in NON_SAFEXPRESS_TRANSPORTERS)
    date_clause = 'AND T0."UpdateDate" >= ADD_DAYS(CURRENT_DATE, ?)' if days is not None else ""
    query = f"""
        SELECT T0."DocEntry", T0."DocNum", T1."SeriesName", T0."UpdateDate"
        FROM "OINV" T0
        LEFT JOIN "OSRI" T1 ON T0."Series" = T1."Series"
        WHERE T0."CANCELED" = 'N'
          AND T0."TransportationCode" = ?
          AND (T0."U_TrackingNo" IS NULL OR TRIM(T0."U_TrackingNo") = '')
          AND (T0."U_TN" IS NULL OR T0."U_TN" NOT IN ({exclude_placeholders}))
          {date_clause}
        ORDER BY T0."DocEntry" DESC
    """
    params: list = [SAP_TRANSPORTATION_CODE_SAFEXPRESS, *NON_SAFEXPRESS_TRANSPORTERS]
    if days is not None:
        params.append(-int(days))

    with hana_connection() as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall() or []
        cur.close()

    return [
        InvoiceCandidate(
            doc_entry=int(doc_entry),
            doc_num=int(doc_num),
            series_name=(str(series_name).strip() if series_name else ""),
            update_date=str(update_date),
        )
        for doc_entry, doc_num, series_name, update_date in rows
    ]


def find_candidates(days: int) -> list[InvoiceCandidate]:
    """Ongoing discovery: candidates updated in the last `days` days. Runs
    every scheduled poll. Small window is fine — its only job is to catch
    invoices new since the last run, not to re-find old unresolved ones
    (the Postgres backlog handles that)."""
    candidates = _run_candidate_query(days)
    logger.info("Found %d new SafeExpress candidate invoice(s) in the last %d days", len(candidates), days)
    return candidates


def find_backfill_candidates() -> list[InvoiceCandidate]:
    """One-time, unbounded (no date filter) scan for the go-live cutover:
    every currently-untracked SafeExpress invoice in SAP, regardless of
    age. Meant to be invoked once (mode=backfill) to seed the Postgres
    queue; ongoing discovery afterward uses find_candidates' small
    window instead — see docs/architecture/PLATFORM_ARCHITECTURE.md §5."""
    candidates = _run_candidate_query(None)
    logger.info("Backfill found %d SafeExpress candidate invoice(s) with no tracking number", len(candidates))
    return candidates
