"""Read-only HANA batch/FEFO queries for Unicommerce invoice posting.

Uses khanal_shared.hana (pooled hdbcli; the pool SETs the configured
schema on checkout, so table names are unqualified here) -- same base as
amazon_us/hana_access.py, but bulk/multi-item instead of single-item:
Unicommerce posting runs process many orders per cycle, so pre-warming
FEFO rows for an entire warehouse in one query (chunked by item count)
collapses what would be dozens of per-line HANA round-trips, same
reasoning as the Frappe source (client/sap.py's
bulk_batch_inventory_by_warehouse / bulk_manage_batch_numbers).
"""

from __future__ import annotations

import logging

from khanal_shared.hana.pool import hana_connection

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 500


def find_invoice_by_order_id(uc_order_id: str) -> dict | None:
    """Exact-match AR Invoice dedup lookup via INV1.U_Order -- the
    line-level UDF ar_invoice_payload always sets, and a real HANA column
    (confirmed via SYS.TABLE_COLUMNS). More reliable than SAP Service
    Layer's U_UCOrderID header-UDF filter, which this SAP install's
    Invoices entity type doesn't recognize at all (confirmed live: HTTP
    400 "Property 'U_UCOrderID' of 'Document' is invalid" -- not a HANA
    projection gap, the UDF plain doesn't exist here). Returns the most
    recent non-cancelled match, or None if genuinely not found."""
    key = str(uc_order_id or "").strip()
    if not key:
        return None
    with hana_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            '''
            SELECT h."DocEntry", h."DocNum", h."VatSum"
            FROM "OINV" h
            INNER JOIN "INV1" l ON l."DocEntry" = h."DocEntry"
            WHERE l."U_Order" = ? AND h."CANCELED" = 'N'
            ORDER BY h."DocEntry" DESC
            ''',
            [key],
        )
        row = cur.fetchone()
        cur.close()
    if not row:
        return None
    return {"doc_entry": int(row[0]), "doc_num": int(row[1] or 0), "doc_tax_amount": float(row[2] or 0)}


def find_credit_note_by_base_invoice(base_doc_entry: int | str) -> dict | None:
    """Exact-match Credit Note dedup lookup via RIN1.BaseEntry/BaseType --
    the standard "Copy From" link every CN we post carries back to its
    parent AR Invoice (BaseType 13), not a custom field. Same rationale as
    find_invoice_by_order_id: SAP SL's U_UCReturnID header-UDF filter
    doesn't work on this install (ORIN has no such column at all), and
    since our posting flow is strictly one CN per invoice, BaseEntry alone
    is an exact, reliable key -- no custom field needed for this one."""
    try:
        entry = int(base_doc_entry)
    except (TypeError, ValueError):
        return None
    with hana_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            '''
            SELECT h."DocEntry", h."DocNum", h."VatSum"
            FROM "ORIN" h
            INNER JOIN "RIN1" l ON l."DocEntry" = h."DocEntry"
            WHERE l."BaseEntry" = ? AND l."BaseType" = 13 AND h."CANCELED" = 'N'
            ORDER BY h."DocEntry" DESC
            ''',
            [entry],
        )
        row = cur.fetchone()
        cur.close()
    if not row:
        return None
    return {"doc_entry": int(row[0]), "doc_num": int(row[1] or 0), "doc_tax_amount": float(row[2] or 0)}


def bulk_batch_inventory_by_warehouse(whs_code: str, item_codes: list[str] | set[str]) -> list[dict]:
    """FEFO-ordered batch rows for the given items in one warehouse.

    Row shape: {"item_code", "batch_number", "warehouse_code", "quantity", "expiry"}.
    Returns [] if HANA is unreachable -- caller falls back to per-line
    checks or treats it as a shortage, same fail-safe policy as the
    Frappe source.
    """
    safe_whs = str(whs_code or "").strip()
    cleaned = sorted({str(c or "").strip() for c in (item_codes or []) if str(c or "").strip()})
    if not safe_whs or not cleaned:
        return []

    rows_out: list[dict] = []
    try:
        with hana_connection() as conn:
            cur = conn.cursor()
            for start in range(0, len(cleaned), _CHUNK_SIZE):
                chunk = cleaned[start : start + _CHUNK_SIZE]
                placeholders = ",".join(["?"] * len(chunk))
                query = f'''
                    SELECT q."ItemCode", b."DistNumber",
                           COALESCE(SUM(q."Quantity"), 0) AS qty,
                           MIN(b."ExpDate") AS exp_date, MIN(b."MnfDate") AS mnf_date
                    FROM "OBTQ" q
                    LEFT JOIN "OBTN" b
                      ON b."ItemCode" = q."ItemCode" AND b."SysNumber" = q."SysNumber"
                    WHERE q."WhsCode" = ? AND q."Quantity" > 0
                      AND q."ItemCode" IN ({placeholders})
                      AND b."DistNumber" IS NOT NULL
                    GROUP BY q."ItemCode", b."DistNumber"
                    ORDER BY COALESCE(MIN(b."ExpDate"), MIN(b."MnfDate")) ASC NULLS LAST,
                             b."DistNumber" ASC
                '''
                cur.execute(query, [safe_whs, *chunk])
                for row in cur.fetchall():
                    item_code, batch_num, qty, exp_date, mnf_date = row[0], row[1], row[2], row[3], row[4]
                    if not item_code or not batch_num:
                        continue
                    try:
                        qty_f = float(qty or 0)
                    except (TypeError, ValueError):
                        qty_f = 0.0
                    rows_out.append(
                        {
                            "item_code": str(item_code),
                            "batch_number": str(batch_num),
                            "warehouse_code": safe_whs,
                            "quantity": qty_f,
                            "expiry": exp_date.isoformat() if hasattr(exp_date, "isoformat") else exp_date,
                        }
                    )
            cur.close()
    except Exception as exc:
        logger.warning("bulk_batch_inventory_by_warehouse HANA query failed wh=%s: %s", safe_whs, exc)
        return []
    return rows_out


def bulk_invoice_totals_by_doc_entries(doc_entries: list[str | int]) -> list[dict]:
    """Real SAP AR Invoice totals for a batch of DocEntries, straight from
    OINV -- for cross-checking our own recorded/recomputed figures against
    what SAP actually has on the document, not what we last sent it.

    Row shape: {"doc_entry", "doc_num", "doc_total", "vat_sum", "canceled"}.
    Returns [] if HANA is unreachable or doc_entries is empty, same
    fail-safe policy as bulk_batch_inventory_by_warehouse.
    """
    cleaned = sorted({int(d) for d in (doc_entries or []) if str(d or "").strip().isdigit()})
    if not cleaned:
        return []

    rows_out: list[dict] = []
    try:
        with hana_connection() as conn:
            cur = conn.cursor()
            for start in range(0, len(cleaned), _CHUNK_SIZE):
                chunk = cleaned[start : start + _CHUNK_SIZE]
                placeholders = ",".join(["?"] * len(chunk))
                query = f'''
                    SELECT "DocEntry", "DocNum", "DocTotal", "VatSum", "CANCELED"
                    FROM "OINV"
                    WHERE "DocEntry" IN ({placeholders})
                '''
                cur.execute(query, chunk)
                for row in cur.fetchall():
                    doc_entry, doc_num, doc_total, vat_sum, canceled = row[0], row[1], row[2], row[3], row[4]
                    rows_out.append(
                        {
                            "doc_entry": int(doc_entry),
                            "doc_num": int(doc_num or 0),
                            "doc_total": float(doc_total or 0),
                            "vat_sum": float(vat_sum or 0),
                            "canceled": str(canceled or "N"),
                        }
                    )
            cur.close()
    except Exception as exc:
        logger.warning("bulk_invoice_totals_by_doc_entries HANA query failed: %s", exc)
        return []
    return rows_out


def bulk_manage_batch_numbers(item_codes: list[str] | set[str]) -> dict[str, bool | None]:
    """{item_code: ManageBatchNumbers?}.

    Tri-state: True = batch-managed, False = not batch-managed,
    None = lookup failed (caller MUST skip batch allocation for this item
    to stay consistent with the payload builder -- same contract as the
    Frappe source's BatchManagedCache).
    """
    cleaned = sorted({str(c or "").strip() for c in (item_codes or []) if str(c or "").strip()})
    if not cleaned:
        return {}

    out: dict[str, bool | None] = {}
    try:
        with hana_connection() as conn:
            cur = conn.cursor()
            for start in range(0, len(cleaned), _CHUNK_SIZE):
                chunk = cleaned[start : start + _CHUNK_SIZE]
                placeholders = ",".join(["?"] * len(chunk))
                # "ManBtchNum" is OITM's real HANA column for this flag --
                # "ManageBatchNumbers" is only the SAP Service Layer OData
                # property name for the same field, confirmed against
                # SYS.TABLE_COLUMNS (both india_test's and OBTQ/OBTN's
                # columns already matched; this was the one wrong name).
                cur.execute(
                    f'SELECT "ItemCode", "ManBtchNum" FROM "OITM" WHERE "ItemCode" IN ({placeholders})',
                    list(chunk),
                )
                for row in cur.fetchall():
                    code = str(row[0] or "").strip()
                    if not code:
                        continue
                    raw = str(row[1] or "").strip().upper()
                    out[code] = raw in ("Y", "YES", "TRUE", "1", "TYES")
            cur.close()
    except Exception as exc:
        logger.warning("bulk_manage_batch_numbers HANA query failed: %s", exc)
        # Unlike bulk_batch_inventory_by_warehouse (safe to return [] and
        # let the caller treat it as "no batches available"), a failed
        # lookup here must propagate as unknown (None) per item, not False
        # -- treating an unknown item as "not batch-managed" could skip a
        # real batch allocation requirement.
        return {code: None for code in cleaned}

    for code in cleaned:
        out.setdefault(code, False)
    return out
