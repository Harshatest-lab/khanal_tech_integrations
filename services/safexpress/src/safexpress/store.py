"""Postgres persistence — a thin work-queue, not an invoice replica.

One row per candidate invoice DocEntry: waybill/POD state and retry
bookkeeping only. SAP stays the source of truth for everything else
(customer, totals, line items, addresses) — this table never mirrors
that, unlike the legacy Frappe "SAP AR Invoice Detail" doctype.
See sql/safexpress.sql for the DDL.
"""

from __future__ import annotations

from typing import Any

import math

import psycopg

from .config import OLDEST_FAIRNESS_FRACTION, POD_STATUS_PENDING, POD_STATUS_STALE, STALE_AFTER_DAYS
from .credentials import DATABASE_URL

OPEN_STATUSES = ("pending", "waybill_found", "no_waybill")


def connect() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)


def upsert_candidate(conn: psycopg.Connection, doc_entry: int, doc_num: int, series_name: str) -> None:
    """Insert a newly-discovered candidate, or touch an existing row.
    A row previously given up on (`stale`) is revived to `pending` here —
    HANA rediscovering it means it's still genuinely open (TransportationCode
    = 1, still no U_TrackingNo), so it re-enters the backlog instead of
    staying abandoned. Any other status (in-progress or completed) is left
    alone."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO safexpress_pod_tracking (doc_entry, doc_num, series_name, pod_status)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (doc_entry) DO UPDATE SET
                doc_num = EXCLUDED.doc_num,
                series_name = COALESCE(NULLIF(EXCLUDED.series_name, ''), safexpress_pod_tracking.series_name),
                pod_status = CASE
                    WHEN safexpress_pod_tracking.pod_status = %s THEN %s
                    ELSE safexpress_pod_tracking.pod_status
                END,
                updated_at = now()
            """,
            (doc_entry, doc_num, series_name, POD_STATUS_PENDING, POD_STATUS_STALE, POD_STATUS_PENDING),
        )
    conn.commit()


def get_tracking_row(conn: psycopg.Connection, doc_entry: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT doc_entry, doc_num, series_name, waybill_number, pod_url, pod_date,
                   pod_status, attempt_count, last_error
            FROM safexpress_pod_tracking WHERE doc_entry = %s
            """,
            (doc_entry,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    cols = ("doc_entry", "doc_num", "series_name", "waybill_number", "pod_url", "pod_date",
            "pod_status", "attempt_count", "last_error")
    return dict(zip(cols, row))


def set_waybill(conn: psycopg.Connection, doc_entry: int, waybill_number: str, pod_status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE safexpress_pod_tracking
            SET waybill_number = %s, pod_status = %s, updated_at = now()
            WHERE doc_entry = %s
            """,
            (waybill_number, pod_status, doc_entry),
        )
    conn.commit()


def set_pod_uploaded(conn: psycopg.Connection, doc_entry: int, pod_url: str, pod_date: str | None, pod_status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE safexpress_pod_tracking
            SET pod_url = %s, pod_date = %s, pod_status = %s, updated_at = now()
            WHERE doc_entry = %s
            """,
            (pod_url, pod_date, pod_status, doc_entry),
        )
    conn.commit()


def mark_completed(conn: psycopg.Connection, doc_entry: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE safexpress_pod_tracking
            SET pod_status = 'completed', checked_at = now(), updated_at = now()
            WHERE doc_entry = %s
            """,
            (doc_entry,),
        )
    conn.commit()


def mark_failed(conn: psycopg.Connection, doc_entry: int, error: str, pod_status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE safexpress_pod_tracking
            SET pod_status = %s, last_error = %s, attempt_count = attempt_count + 1,
                checked_at = now(), updated_at = now()
            WHERE doc_entry = %s
            """,
            (pod_status, error[:2000], doc_entry),
        )
    conn.commit()


def list_pending_sap(conn: psycopg.Connection, limit: int = 100) -> list[dict[str, Any]]:
    """Rows stuck with a POD already uploaded but SAP PATCH still failing —
    the retry-only path, no fresh HANA/SafeExpress calls needed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT doc_entry, doc_num, series_name, waybill_number, pod_url, pod_date
            FROM safexpress_pod_tracking
            WHERE pod_status = 'pending_sap'
            ORDER BY updated_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    cols = ("doc_entry", "doc_num", "series_name", "waybill_number", "pod_url", "pod_date")
    return [dict(zip(cols, r)) for r in rows]


def mark_stale_if_expired(conn: psycopg.Connection, stale_after_days: int = STALE_AFTER_DAYS) -> int:
    """Gives up on rows that have been open (not completed) for longer than
    stale_after_days — past that point an unresolved invoice is more likely
    cancelled or mis-tagged with the wrong transporter than genuinely still
    in transit. Returns the number of rows marked. A later HANA sighting
    revives a stale row (see upsert_candidate)."""
    placeholders = ", ".join(["%s"] * len(OPEN_STATUSES))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE safexpress_pod_tracking
            SET pod_status = %s, updated_at = now()
            WHERE pod_status IN ({placeholders})
              AND created_at < now() - (%s || ' days')::interval
            """,
            (POD_STATUS_STALE, *OPEN_STATUSES, str(int(stale_after_days))),
        )
        count = cur.rowcount
    conn.commit()
    return count


def _split_fairness_limit(limit: int, oldest_fraction: float) -> tuple[int, int]:
    """Pure helper: how many of `limit` backlog slots go to the oldest-first
    reserve vs. the newest-first majority. Always reserves at least 1 slot
    for the oldest side when limit > 0, so a single-row backlog run still
    makes progress on old invoices."""
    if limit <= 0:
        return 0, 0
    oldest_n = max(1, math.floor(limit * oldest_fraction))
    oldest_n = min(oldest_n, limit)
    return limit - oldest_n, oldest_n


def list_open_candidates(
    conn: psycopg.Connection, limit: int, oldest_fraction: float = OLDEST_FAIRNESS_FRACTION
) -> list[dict[str, Any]]:
    """The actual work queue for a poll run: open (not completed/stale)
    rows, mostly newest-invoice-first (highest DocEntry — recent shipments
    are far likelier to already have a waybill), with a small reserved
    slice of the oldest open rows so nothing waits forever behind a stream
    of new invoices. This — not the live HANA window — is what determines
    whether a previously-discovered invoice keeps getting retried."""
    newest_n, oldest_n = _split_fairness_limit(limit, oldest_fraction)
    if newest_n == 0 and oldest_n == 0:
        return []

    placeholders = ", ".join(["%s"] * len(OPEN_STATUSES))
    cols = "doc_entry, doc_num, series_name, waybill_number, pod_url, pod_date, pod_status"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {cols} FROM safexpress_pod_tracking
            WHERE pod_status IN ({placeholders})
            ORDER BY doc_entry DESC
            LIMIT %s
            """,
            (*OPEN_STATUSES, newest_n),
        )
        newest_rows = cur.fetchall()
        seen_doc_entries = {r[0] for r in newest_rows}

        oldest_rows: list[tuple] = []
        if oldest_n > 0:
            exclude_placeholders = ", ".join(["%s"] * len(seen_doc_entries)) if seen_doc_entries else ""
            exclude_clause = f"AND doc_entry NOT IN ({exclude_placeholders})" if seen_doc_entries else ""
            cur.execute(
                f"""
                SELECT {cols} FROM safexpress_pod_tracking
                WHERE pod_status IN ({placeholders})
                {exclude_clause}
                ORDER BY doc_entry ASC
                LIMIT %s
                """,
                (*OPEN_STATUSES, *seen_doc_entries, oldest_n),
            )
            oldest_rows = cur.fetchall()

    col_names = ("doc_entry", "doc_num", "series_name", "waybill_number", "pod_url", "pod_date", "pod_status")
    return [dict(zip(col_names, r)) for r in (*newest_rows, *oldest_rows)]
