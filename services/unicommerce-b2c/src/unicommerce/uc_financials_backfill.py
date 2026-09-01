"""One-off: recompute uc_taxable_value/uc_cgst/uc_sgst/uc_igst/uc_total on
already-staged orders using the current mappers.compute_uc_financials, and
write the corrected figures back.

Exists because mappers.compute_uc_financials only ever runs at fetch/upsert
time -- an order that's already 'posted' is never re-touched by the normal
fetch flow (no reason to, for posting purposes), so a bug fix to that
function does nothing for historical rows on its own. Confirmed real
2026-09-01: compute_uc_financials silently dropped the entire tax component
for AMAZON_FLEX orders (only read UC-native GST fields, never fell back to
the flat taxPercentage flex_fetch.py's synthetic orders carry) -- fixed in
the function itself, this module is the one-off correction pass for the
~475 already-posted August orders that bug already touched.

Pure recompute from each row's own already-stored order_json -- no re-fetch
from Amazon/UC, no SAP write, no change to pipeline_status or posting state
whatsoever. Batched (limit/offset) and defaults to dry-run, same
apply-flag pattern as numatcard_backfill.py.
"""

from __future__ import annotations

import json
import logging

from unicommerce import mappers, store

logger = logging.getLogger(__name__)

_FIELDS = ("uc_taxable_value", "uc_cgst", "uc_sgst", "uc_igst", "uc_total")


def fetch_candidates(limit: int, offset: int, channel_name: str | None = None) -> list[dict]:
    conn = store.connect()
    try:
        with conn.cursor() as cur:
            where = "WHERE order_json IS NOT NULL AND order_json != '{}'::jsonb"
            params: list = []
            if channel_name:
                where += " AND channel_name = %s"
                params.append(channel_name)
            params.extend([limit, offset])
            cur.execute(
                f"""
                SELECT uniware_id, channel_name, order_json,
                       uc_taxable_value, uc_cgst, uc_sgst, uc_igst, uc_total
                FROM unicommerce_orders
                {where}
                ORDER BY order_date ASC, uniware_id ASC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows


def _update_financials(conn, uniware_id: str, financials: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_orders
            SET uc_taxable_value = %(uc_taxable_value)s,
                uc_cgst = %(uc_cgst)s,
                uc_sgst = %(uc_sgst)s,
                uc_igst = %(uc_igst)s,
                uc_total = %(uc_total)s,
                updated_at = now()
            WHERE uniware_id = %(uniware_id)s
            """,
            {**financials, "uniware_id": uniware_id},
        )
    conn.commit()


def run_backfill(limit: int = 200, offset: int = 0, apply: bool = False, channel_name: str | None = None) -> dict:
    candidates = fetch_candidates(limit, offset, channel_name)
    unchanged = corrected = failed = 0
    details: list[str] = []

    conn = store.connect() if apply else None
    try:
        for row in candidates:
            uniware_id = row["uniware_id"]
            try:
                order = row["order_json"] if isinstance(row["order_json"], dict) else json.loads(row["order_json"])
                order.setdefault("channel", row.get("channel_name"))
                recomputed = mappers.compute_uc_financials(order)
            except Exception as e:
                details.append(f"FAIL  {uniware_id}: {e}")
                failed += 1
                continue

            old_total = float(row.get("uc_total") or 0)
            new_total = recomputed["uc_total"]
            if abs(old_total - new_total) < 0.01:
                unchanged += 1
                continue

            details.append(f"CHANGE {uniware_id} ({row.get('channel_name')}): uc_total {old_total} -> {new_total}")
            corrected += 1
            if apply:
                _update_financials(conn, uniware_id, recomputed)
    finally:
        if conn is not None:
            conn.close()

    return {
        "candidates": len(candidates),
        "unchanged": unchanged,
        "corrected": corrected,
        "failed": failed,
        "applied": apply,
        "details": details[:50],
    }
