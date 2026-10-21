"""Postgres persistence for the Amazon US pipeline (psycopg, repo style).

All SQL lives here. Stage machine on amazon_us_orders:

    fetched -> ready -> posting -> posted
                  \\-> failed (posting error; last_error says why)

Rules enforced at the SQL level:
  - POSTING/POSTED orders are never downgraded by a re-fetch (metadata
    refresh only).
  - claim() is an atomic conditional UPDATE — parallel workers cannot
    double-claim an order.
  - Refunds live in amazon_us_cn_log keyed by refund_id; an order's stage
    stays 'posted' through any number of partial refunds.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

import psycopg

from amazon_us.credentials import DATABASE_URL

logger = logging.getLogger(__name__)


def connect() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)


# ── Single-flight locks ──────────────────────────────────────────────────
# Postgres advisory locks, not Python-level -- have to hold across process
# boundaries because the thing being guarded (SP-API quota) is external and
# shared by more than one Lambda function. amazonFetch's report-mode sync
# and amazonFbaShipSync's FBA report both draw from the *same* createReport
# quota bucket, so REPORTS_API_LOCK has to stop them stepping on each other
# too, not just guard within one function. Session-scoped: released
# explicitly via release_lock(), or automatically by Postgres the moment the
# connection drops -- a Lambda hard-timeout kills the connection, which
# frees the lock with no explicit cleanup needed.
ORDERS_API_LOCK = "amz:orders_api"
REPORTS_API_LOCK = "amz:reports_api"


def try_acquire_lock(conn: psycopg.Connection, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (name,))
        acquired = bool(cur.fetchone()[0])
    conn.commit()
    return acquired


def release_lock(conn: psycopg.Connection, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (name,))
    conn.commit()


# ── Staging ──────────────────────────────────────────────────────────────

_PROTECTED_STAGES = ("posting", "posted")


def get_order_stage(conn: psycopg.Connection, amazon_order_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stage FROM amazon_us_orders WHERE amazon_order_id = %s", (amazon_order_id,)
        )
        row = cur.fetchone()
    return row[0] if row else None


def get_order_stage_and_status(conn: psycopg.Connection, amazon_order_id: str) -> tuple[str, str] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stage, order_status FROM amazon_us_orders WHERE amazon_order_id = %s", (amazon_order_id,)
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else None


def _lines_total(lines: list[dict[str, Any]]) -> float:
    return round(
        sum(
            float(l.get("item_price_amount") or 0)
            + float(l.get("item_tax_amount") or 0)
            + float(l.get("shipping_price_amount") or 0)
            + float(l.get("shipping_tax_amount") or 0)
            for l in lines
        ),
        2,
    )


def upsert_order(conn: psycopg.Connection, fields: dict[str, Any], lines: list[dict[str, Any]]) -> str:
    """Insert or refresh one staged order + its lines.

    Returns 'created' | 'updated' | 'refreshed' (refreshed = order is
    POSTING/POSTED; only status + raw payload were touched, lines and
    stage left alone).

    order_total_amount is always recomputed from `lines` (when given) --
    never trusted from the caller's fields dict. Confirmed live 2026-08-28:
    the report-fetch path and the Orders-API path can each touch this
    order's header and lines at different times, and whichever call last
    set order_total_amount doesn't necessarily agree with the lines
    currently on file (1487 of ~10.3k August orders drifted, always
    undercounting -- SAP's own DocTotal, built fresh from live line data
    at post time, was correct throughout). Deriving the header from its
    own lines here makes drift structurally impossible instead of
    depending on every caller to keep them in sync."""
    oid = fields["amazon_order_id"]
    existing_stage = get_order_stage(conn, oid)

    if existing_stage in _PROTECTED_STAGES:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE amazon_us_orders SET
                  order_status = %(order_status)s,
                  raw_payload = %(raw_payload)s,
                  updated_at = now()
                WHERE amazon_order_id = %(amazon_order_id)s
                """,
                {
                    "amazon_order_id": oid,
                    "order_status": fields.get("order_status", ""),
                    "raw_payload": json.dumps(fields.get("raw_payload") or {}),
                },
            )
        conn.commit()
        return "refreshed"

    params = {
        "amazon_order_id": oid,
        "order_status": fields.get("order_status", ""),
        "stage": fields["stage"],
        "purchase_date": fields.get("purchase_date"),
        "purchase_date_utc": fields.get("purchase_date_utc"),
        "actual_ship_date": fields.get("actual_ship_date"),
        "fulfillment_channel": fields.get("fulfillment_channel", ""),
        "order_total_amount": _lines_total(lines) if lines else fields.get("order_total_amount", 0),
        "ship_city": fields.get("ship_city", ""),
        "ship_state": fields.get("ship_state", ""),
        "ship_postal_code": fields.get("ship_postal_code", ""),
        "ship_country": fields.get("ship_country", ""),
        "buyer_name": fields.get("buyer_name", ""),
        "last_error": fields.get("last_error"),
        "raw_payload": json.dumps(fields.get("raw_payload") or {}),
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO amazon_us_orders (
              amazon_order_id, order_status, stage, purchase_date, purchase_date_utc,
              actual_ship_date, fulfillment_channel, order_total_amount, ship_city, ship_state,
              ship_postal_code, ship_country, buyer_name, last_error, raw_payload
            ) VALUES (
              %(amazon_order_id)s, %(order_status)s, %(stage)s, %(purchase_date)s, %(purchase_date_utc)s,
              %(actual_ship_date)s, %(fulfillment_channel)s, %(order_total_amount)s, %(ship_city)s, %(ship_state)s,
              %(ship_postal_code)s, %(ship_country)s, %(buyer_name)s, %(last_error)s, %(raw_payload)s
            )
            ON CONFLICT (amazon_order_id) DO UPDATE SET
              order_status = EXCLUDED.order_status,
              stage = EXCLUDED.stage,
              purchase_date = EXCLUDED.purchase_date,
              purchase_date_utc = EXCLUDED.purchase_date_utc,
              actual_ship_date = COALESCE(EXCLUDED.actual_ship_date, amazon_us_orders.actual_ship_date),
              fulfillment_channel = CASE WHEN EXCLUDED.fulfillment_channel = '' THEN amazon_us_orders.fulfillment_channel ELSE EXCLUDED.fulfillment_channel END,
              order_total_amount = EXCLUDED.order_total_amount,
              ship_city = EXCLUDED.ship_city,
              ship_state = EXCLUDED.ship_state,
              ship_postal_code = EXCLUDED.ship_postal_code,
              ship_country = EXCLUDED.ship_country,
              buyer_name = EXCLUDED.buyer_name,
              last_error = EXCLUDED.last_error,
              raw_payload = EXCLUDED.raw_payload,
              updated_at = now()
            """,
            params,
        )
        for line in lines:
            cur.execute(
                """
                INSERT INTO amazon_us_order_lines (
                  amazon_order_id, order_item_id, seller_sku, asin, title, quantity,
                  unit_price, item_price_amount, item_tax_amount,
                  shipping_price_amount, shipping_tax_amount
                ) VALUES (
                  %(amazon_order_id)s, %(order_item_id)s, %(seller_sku)s, %(asin)s, %(title)s, %(quantity)s,
                  %(unit_price)s, %(item_price_amount)s, %(item_tax_amount)s,
                  %(shipping_price_amount)s, %(shipping_tax_amount)s
                )
                ON CONFLICT (amazon_order_id, order_item_id, seller_sku) DO UPDATE SET
                  asin = EXCLUDED.asin,
                  title = EXCLUDED.title,
                  quantity = EXCLUDED.quantity,
                  unit_price = EXCLUDED.unit_price,
                  item_price_amount = EXCLUDED.item_price_amount,
                  item_tax_amount = EXCLUDED.item_tax_amount,
                  shipping_price_amount = EXCLUDED.shipping_price_amount,
                  shipping_tax_amount = EXCLUDED.shipping_tax_amount,
                  updated_at = now()
                """,
                {"amazon_order_id": oid, **line},
            )
    conn.commit()
    return "updated" if existing_stage else "created"


def get_order(conn: psycopg.Connection, amazon_order_id: str) -> dict | None:
    """Order header + lines, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT amazon_order_id, order_status, stage, purchase_date, purchase_date_utc,
                   actual_ship_date, fulfillment_channel, order_total_amount, ship_city, ship_state,
                   ship_postal_code, ship_country, buyer_name, sap_doc_entry, sap_doc_num,
                   last_error, attempt_count
            FROM amazon_us_orders WHERE amazon_order_id = %s
            """,
            (amazon_order_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d.name for d in cur.description]
        order = dict(zip(cols, row))
        cur.execute(
            """
            SELECT order_item_id, seller_sku, asin, title, quantity, unit_price,
                   item_price_amount, item_tax_amount, shipping_price_amount, shipping_tax_amount
            FROM amazon_us_order_lines WHERE amazon_order_id = %s ORDER BY id
            """,
            (amazon_order_id,),
        )
        line_cols = [d.name for d in cur.description]
        order["lines"] = [dict(zip(line_cols, r)) for r in cur.fetchall()]
    return order


def list_ready_orders(
    conn: psycopg.Connection, cutoff_date: str, limit: int, to_date: str | None = None
) -> list[dict]:
    """READY orders eligible for SAP posting: actual_ship_date present
    (ship-date is the SAP DocDate — no ship date, no post) and on/after the
    posting cutoff (pre-cutoff revenue was manually booked by Finance).
    Oldest ship dates first. `to_date` narrows the window further (e.g. an
    ops-triggered "post just this range") — it can only shrink the window,
    never extend it before cutoff_date."""
    with conn.cursor() as cur:
        if to_date:
            cur.execute(
                """
                SELECT amazon_order_id FROM amazon_us_orders
                WHERE stage = 'ready'
                  AND actual_ship_date IS NOT NULL
                  AND actual_ship_date >= %s::date
                  AND actual_ship_date <= %s::date
                ORDER BY actual_ship_date ASC, amazon_order_id ASC
                LIMIT %s
                """,
                (cutoff_date, to_date, limit),
            )
        else:
            cur.execute(
                """
                SELECT amazon_order_id FROM amazon_us_orders
                WHERE stage = 'ready'
                  AND actual_ship_date IS NOT NULL
                  AND actual_ship_date >= %s::date
                ORDER BY actual_ship_date ASC, amazon_order_id ASC
                LIMIT %s
                """,
                (cutoff_date, limit),
            )
        ids = [r[0] for r in cur.fetchall()]
    return [o for oid in ids if (o := get_order(conn, oid))]


def claim(conn: psycopg.Connection, amazon_order_id: str, from_stage: str, to_stage: str) -> bool:
    """Atomically move an order between stages. False = someone else won.

    Claiming into 'posting' also reclaims a stale 'posting' claim (crashed
    worker) after 30 minutes -- same threshold as claim_cn's and recon.py's
    stuck-posting invariant (both already had this; plain order claims
    didn't). Confirmed live 2026-08-19: 16 orders left in 'posting' by
    invocations that crashed/timed out mid-post, with no automatic path
    back to 'ready' -- required a manual SQL fix to recover.
    """
    with conn.cursor() as cur:
        if to_stage == "posting":
            cur.execute(
                """
                UPDATE amazon_us_orders
                SET stage = %s, updated_at = now()
                WHERE amazon_order_id = %s
                  AND (stage = %s
                       OR (stage = 'posting' AND updated_at < now() - interval '30 minutes'))
                """,
                (to_stage, amazon_order_id, from_stage),
            )
        else:
            cur.execute(
                """
                UPDATE amazon_us_orders
                SET stage = %s, updated_at = now()
                WHERE amazon_order_id = %s AND stage = %s
                """,
                (to_stage, amazon_order_id, from_stage),
            )
        claimed = cur.rowcount == 1
    conn.commit()
    return claimed


def mark_posted(
    conn: psycopg.Connection, amazon_order_id: str, doc_entry: int, doc_num: int
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE amazon_us_orders SET
              stage = 'posted', sap_doc_entry = %s, sap_doc_num = %s,
              posted_at = now(), last_error = NULL, updated_at = now()
            WHERE amazon_order_id = %s
            """,
            (doc_entry, doc_num, amazon_order_id),
        )
    conn.commit()


def mark_failed(conn: psycopg.Connection, amazon_order_id: str, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE amazon_us_orders SET
              stage = 'failed', last_error = %s,
              attempt_count = attempt_count + 1, updated_at = now()
            WHERE amazon_order_id = %s
            """,
            (error[:2000], amazon_order_id),
        )
    conn.commit()


def set_actual_ship_dates(conn: psycopg.Connection, ship_dates: dict[str, str]) -> int:
    """Bulk-update actual_ship_date for staged orders; returns rows updated.

    Each call only sees the earliest ship date within its own report window,
    so this never overwrites a date with a *later* one (a rolling window
    that ages out a partial shipment's earliest record must not drag the
    stored date forward) and never touches an order that's 'posted' or
    currently 'posting' (its SAP DocDate is already fixed, or about to be,
    from whatever ship date the in-flight post already snapshotted —
    same protection upsert_order gives the header via _PROTECTED_STAGES)."""
    updated = 0
    with conn.cursor() as cur:
        for oid, ship_date in ship_dates.items():
            cur.execute(
                """
                UPDATE amazon_us_orders SET actual_ship_date = %s::date, updated_at = now()
                WHERE amazon_order_id = %s
                  AND stage NOT IN ('posting', 'posted')
                  AND (actual_ship_date IS NULL OR actual_ship_date > %s::date)
                """,
                (ship_date, oid, ship_date),
            )
            updated += cur.rowcount
    conn.commit()
    return updated


def promote_fetched_orders(conn: psycopg.Connection, mapping: dict[str, str]) -> int:
    """Re-evaluate FETCHED orders: Shipped + every line SKU mapped -> READY.
    Runs after each SKU-map refresh so SAP-side mapping fixes unstick
    orders without a re-fetch. Returns count promoted."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT h.amazon_order_id, array_agg(DISTINCT l.seller_sku)
            FROM amazon_us_orders h
            JOIN amazon_us_order_lines l ON l.amazon_order_id = h.amazon_order_id
            WHERE h.stage = 'fetched' AND h.order_status = 'Shipped'
            GROUP BY h.amazon_order_id
            """
        )
        rows = cur.fetchall()
        promoted = 0
        for oid, skus in rows:
            if all(s in mapping for s in skus):
                cur.execute(
                    """
                    UPDATE amazon_us_orders
                    SET stage = 'ready', last_error = NULL, updated_at = now()
                    WHERE amazon_order_id = %s AND stage = 'fetched'
                    """,
                    (oid,),
                )
                promoted += cur.rowcount
    conn.commit()
    return promoted


def backfill_order_totals(conn: psycopg.Connection, amazon_order_ids: list[str]) -> int:
    """One-off correction for order_total_amount drift that predates
    upsert_order always deriving it from lines (see that function's
    docstring for the 2026-08-28 finding: 1487 of ~10.3k August orders
    had a stale header total while their lines and SAP's own DocTotal
    agreed). Recomputes the header from each order's own current lines,
    same formula upsert_order now uses going forward. Scoped to the
    given id list -- never sweeps every order in the table."""
    if not amazon_order_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE amazon_us_orders o SET
              order_total_amount = sub.total, updated_at = now()
            FROM (
              SELECT amazon_order_id,
                     SUM(item_price_amount + item_tax_amount + shipping_price_amount + shipping_tax_amount) AS total
              FROM amazon_us_order_lines
              WHERE amazon_order_id = ANY(%s)
              GROUP BY amazon_order_id
            ) sub
            WHERE o.amazon_order_id = sub.amazon_order_id
            """,
            (amazon_order_ids,),
        )
        updated = cur.rowcount
    conn.commit()
    return updated


# ── Invoice / CN audit logs ──────────────────────────────────────────────

def log_invoice_attempt(
    conn: psycopg.Connection,
    amazon_order_id: str,
    status: str,
    *,
    doc_entry: int | None = None,
    doc_num: int | None = None,
    payload: dict | None = None,
    response: dict | None = None,
    error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO amazon_us_invoice_log
              (amazon_order_id, status, sap_doc_entry, sap_doc_num, sap_payload, sap_response, error_message)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                amazon_order_id,
                status,
                doc_entry,
                doc_num,
                json.dumps(payload, default=str) if payload else None,
                json.dumps(response, default=str) if response else None,
                error[:2000] if error else None,
            ),
        )
    conn.commit()


def get_cn_log(conn: psycopg.Connection, refund_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT refund_id, status, sap_doc_entry, sap_doc_num, attempt_count "
            "FROM amazon_us_cn_log WHERE refund_id = %s",
            (refund_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return dict(zip(("refund_id", "status", "sap_doc_entry", "sap_doc_num", "attempt_count"), row))


def claim_cn(conn: psycopg.Connection, refund_id: str, amazon_order_id: str, posted_date: str | None) -> bool:
    """Atomically claim a refund event for posting (marks it 'posting').
    False means it's already posted, permanently failed, or another run
    claimed it <30min ago -- the caller must not post in any of those
    cases. 'failed_permanent' (SAP says the base document line is
    structurally closed -- see sap_client.is_base_document_closed) is
    excluded from reclaim on purpose: unlike a plain 'failed' (worth
    retrying next run), retrying this never succeeds. A stale 'posting'
    claim (crashed worker) is reclaimable after 30 minutes, same threshold
    as the order stage's stuck-posting invariant."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO amazon_us_cn_log (refund_id, amazon_order_id, posted_date, status, attempt_count)
            VALUES (%s, %s, %s, 'posting', 1)
            ON CONFLICT (refund_id) DO UPDATE SET
              status = 'posting',
              attempt_count = amazon_us_cn_log.attempt_count + 1,
              updated_at = now()
            WHERE amazon_us_cn_log.status NOT IN ('posted', 'failed_permanent')
              AND (amazon_us_cn_log.status != 'posting'
                   OR amazon_us_cn_log.updated_at < now() - interval '30 minutes')
            """,
            (refund_id, amazon_order_id, posted_date),
        )
        claimed = cur.rowcount == 1
    conn.commit()
    return claimed


def requeue_failed_permanent_cns(conn: psycopg.Connection, refund_ids: list[str]) -> list[str]:
    """Ops helper: move exactly these refund_ids from 'failed_permanent' back
    to 'failed' so claim_cn will reclaim them. Only for when the cause of the
    permanent failure has been fixed outside the pipeline (e.g. Finance
    reopened the closed base invoices). Never touches any other status, and
    never a refund_id that wasn't named. Returns the ids actually moved."""
    if not refund_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE amazon_us_cn_log SET status = 'failed', updated_at = now()
            WHERE refund_id = ANY(%s) AND status = 'failed_permanent'
            RETURNING refund_id
            """,
            (list(refund_ids),),
        )
        moved = [r[0] for r in cur.fetchall()]
    conn.commit()
    return moved


def upsert_cn_log(
    conn: psycopg.Connection,
    refund_id: str,
    amazon_order_id: str,
    posted_date: str | None,
    status: str,
    *,
    skip_reason: str | None = None,
    doc_entry: int | None = None,
    doc_num: int | None = None,
    base_doc_entry: int | None = None,
    payload: dict | None = None,
    response: dict | None = None,
    error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO amazon_us_cn_log (
              refund_id, amazon_order_id, posted_date, status, skip_reason,
              sap_doc_entry, sap_doc_num, base_doc_entry, sap_payload, sap_response,
              error_message, attempt_count
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
            ON CONFLICT (refund_id) DO UPDATE SET
              status = EXCLUDED.status,
              skip_reason = EXCLUDED.skip_reason,
              sap_doc_entry = COALESCE(EXCLUDED.sap_doc_entry, amazon_us_cn_log.sap_doc_entry),
              sap_doc_num = COALESCE(EXCLUDED.sap_doc_num, amazon_us_cn_log.sap_doc_num),
              base_doc_entry = COALESCE(EXCLUDED.base_doc_entry, amazon_us_cn_log.base_doc_entry),
              sap_payload = COALESCE(EXCLUDED.sap_payload, amazon_us_cn_log.sap_payload),
              sap_response = COALESCE(EXCLUDED.sap_response, amazon_us_cn_log.sap_response),
              error_message = EXCLUDED.error_message,
              attempt_count = amazon_us_cn_log.attempt_count + 1,
              updated_at = now()
            """,
            (
                refund_id,
                amazon_order_id,
                posted_date,
                status,
                skip_reason,
                doc_entry,
                doc_num,
                base_doc_entry,
                json.dumps(payload, default=str) if payload else None,
                json.dumps(response, default=str) if response else None,
                error[:2000] if error else None,
            ),
        )
    conn.commit()
    record_cn_attempt(
        conn, refund_id, amazon_order_id, status,
        doc_entry=doc_entry, doc_num=doc_num, payload=payload, response=response, error=error or skip_reason,
    )


def save_raw(conn: psycopg.Connection, source: str, ref: str, payload: Any) -> None:
    """Keep what Amazon sent, exactly as received, in amazon_us_raw_events.

    Append-only and de-duplicated by content: the same record fetched again adds nothing, but a changed one
    (status moved, items added) is kept as a new version, so history is never overwritten the way
    amazon_us_orders.raw_payload is. Best effort: a failure here must never stop the sync that called it."""
    if not payload:
        return
    body = json.dumps(payload, default=str, sort_keys=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO amazon_us_raw_events (source, ref, payload_hash, payload)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (source, ref, payload_hash) DO NOTHING
                """,
                (source, ref, hashlib.sha256(body.encode()).hexdigest(), body),
            )
        conn.commit()
    except Exception:  # noqa: BLE001 -- see docstring
        conn.rollback()
        logger.exception("could not save raw %s record for %s", source, ref)


def record_cn_attempt(
    conn: psycopg.Connection, refund_id: str, amazon_order_id: str, status: str, *,
    doc_entry: int | None, doc_num: int | None, payload: dict | None, response: dict | None, error: str | None,
) -> None:
    """One row per credit-note attempt in amazon_us_cn_attempts. amazon_us_cn_log keeps only the latest state of
    a refund; this keeps every try with what was sent and SAP's answer. Best effort, like save_raw."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO amazon_us_cn_attempts
                  (refund_id, amazon_order_id, status, sap_doc_entry, sap_doc_num, sap_payload, sap_response, error_message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    refund_id, amazon_order_id, status, doc_entry, doc_num,
                    json.dumps(payload, default=str) if payload else None,
                    json.dumps(response, default=str) if response else None,
                    error[:2000] if error else None,
                ),
            )
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.exception("could not record credit note attempt for %s", refund_id)


# ── SKU map ──────────────────────────────────────────────────────────────

def replace_hana_sku_rows(conn: psycopg.Connection, sku_map: dict[str, list[str]]) -> tuple[int, list[str]]:
    """Replace source='hana' rows with the latest single-code SKUs.
    SKUs mapped to more than one SAP item are excluded (never guessed) and
    returned for alerting; an override row is the only way to use them."""
    singles = {sku: codes[0] for sku, codes in sku_map.items() if len(codes) == 1}
    ambiguous = sorted(sku for sku, codes in sku_map.items() if len(codes) > 1)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM amazon_us_sku_map WHERE source = 'hana'")
        for sku, code in singles.items():
            cur.execute(
                """
                INSERT INTO amazon_us_sku_map (seller_sku, sap_item_code, source)
                VALUES (%s, %s, 'hana')
                ON CONFLICT (seller_sku, source) DO UPDATE
                  SET sap_item_code = EXCLUDED.sap_item_code, updated_at = now()
                """,
                (sku, code),
            )
    conn.commit()
    return len(singles), ambiguous


def upsert_sku_override(conn: psycopg.Connection, seller_sku: str, sap_item_code: str) -> None:
    """Manually pin a seller_sku -> sap_item_code mapping that survives
    every HANA refresh (replace_hana_sku_rows only ever touches
    source='hana' rows) and wins over any hana-sourced mapping for the
    same SKU. For SKUs not yet in SAP's OITM.U_Amz_SKU field -- the
    durable fix is still to add it there; this unblocks orders in the
    meantime without waiting on that."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO amazon_us_sku_map (seller_sku, sap_item_code, source)
            VALUES (%s, %s, 'override')
            ON CONFLICT (seller_sku, source) DO UPDATE
              SET sap_item_code = EXCLUDED.sap_item_code, updated_at = now()
            """,
            (seller_sku, sap_item_code),
        )
    conn.commit()


def get_sku_mapping(conn: psycopg.Connection) -> dict[str, str]:
    """{seller_sku: sap_item_code}; override rows win over hana rows."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (seller_sku) seller_sku, sap_item_code
            FROM amazon_us_sku_map
            ORDER BY seller_sku, CASE source WHEN 'override' THEN 0 ELSE 1 END
            """
        )
        return {sku: code for sku, code in cur.fetchall()}


# ── Watermarks + sync log ────────────────────────────────────────────────

def get_watermark(conn: psycopg.Connection, name: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM amazon_us_watermarks WHERE name = %s", (name,))
        row = cur.fetchone()
    return row[0] if row else None


def set_watermark(conn: psycopg.Connection, name: str, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO amazon_us_watermarks (name, value) VALUES (%s, %s)
            ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (name, value),
        )
    conn.commit()


def start_sync_log(
    conn: psycopg.Connection, mode: str, window_from: str | None = None, window_to: str | None = None
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO amazon_us_sync_log (mode, window_from, window_to)
            VALUES (%s, %s, %s) RETURNING id
            """,
            (mode, window_from, window_to),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_sync_log(
    conn: psycopg.Connection, run_id: int, status: str, counts: dict | None = None, error: str | None = None
) -> None:
    # A DB error earlier in this run can leave the transaction aborted; without
    # this rollback the UPDATE below would itself raise and the error status
    # (and message) would never get written, masking the real failure.
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE amazon_us_sync_log SET
              finished_at = now(), status = %s, counts = %s, error_message = %s
            WHERE id = %s
            """,
            (status, json.dumps(counts or {}, default=str), error[:2000] if error else None, run_id),
        )
    conn.commit()


def utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Pending Reports API jobs (resumable poll) ───────────────────────────
# See sql/amazon_us_b2c.sql for amazon_us_pending_reports. Created here
# defensively (CREATE TABLE IF NOT EXISTS) so picking this up doesn't
# depend on a separate prod migration step -- same idempotent DDL either
# way, just runnable from inside the Lambda that already has DATABASE_URL.

_ENSURE_PENDING_REPORTS_SQL = """
CREATE TABLE IF NOT EXISTS amazon_us_pending_reports (
  id           bigserial   PRIMARY KEY,
  purpose      text        NOT NULL,
  report_id    text        NOT NULL,
  requested_at timestamptz NOT NULL DEFAULT now(),
  context      jsonb       NOT NULL DEFAULT '{}'::jsonb,
  status       text        NOT NULL DEFAULT 'pending',
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_amazon_us_pending_reports_purpose
  ON amazon_us_pending_reports (purpose, status);
"""


def _ensure_pending_reports_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(_ENSURE_PENDING_REPORTS_SQL)
    conn.commit()


def create_pending_report(conn: psycopg.Connection, purpose: str, report_id: str, context: dict) -> int:
    _ensure_pending_reports_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO amazon_us_pending_reports (purpose, report_id, context)
            VALUES (%s, %s, %s) RETURNING id
            """,
            (purpose, report_id, json.dumps(context, default=str)),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    return row_id


def list_pending_reports(conn: psycopg.Connection, purpose: str) -> list[dict]:
    _ensure_pending_reports_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, report_id, context FROM amazon_us_pending_reports
            WHERE purpose = %s AND status = 'pending'
            ORDER BY requested_at ASC
            """,
            (purpose,),
        )
        rows = cur.fetchall()
    return [{"id": r[0], "report_id": r[1], "context": r[2]} for r in rows]


def finish_pending_report(conn: psycopg.Connection, row_id: int, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE amazon_us_pending_reports SET status = %s, updated_at = now() WHERE id = %s",
            (status, row_id),
        )
    conn.commit()


# ── Pre-cutoff cleanup ───────────────────────────────────────────────────
# Orders shipped before AMZ_POSTING_CUTOFF_DATE were manually booked by
# Finance from the MTR pre-pipeline (see constants.POSTING_CUTOFF_DATE) --
# the posting query (list_ready_orders) already refuses to ever post them,
# so any such order sitting in stage != 'posted' is permanently dead, not
# a bug waiting to be retried. 'posting' is deliberately excluded even
# though it should never occur pre-cutoff (the cutoff guard blocks the
# transition) -- a belt-and-suspenders skip of anything that might be
# mid-flight rather than assuming the guard always held.
_PRECUTOFF_UNPOSTED_STAGES = ("fetched", "ready", "failed")


def count_precutoff_unposted(conn: psycopg.Connection, cutoff_date: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*), MIN(actual_ship_date), MAX(actual_ship_date)
            FROM amazon_us_orders
            WHERE stage = ANY(%s) AND actual_ship_date < %s
            """,
            (list(_PRECUTOFF_UNPOSTED_STAGES), cutoff_date),
        )
        count, min_date, max_date = cur.fetchone()
        cur.execute(
            """
            SELECT amazon_order_id, stage, actual_ship_date, order_total_amount
            FROM amazon_us_orders
            WHERE stage = ANY(%s) AND actual_ship_date < %s
            ORDER BY actual_ship_date DESC
            LIMIT 10
            """,
            (list(_PRECUTOFF_UNPOSTED_STAGES), cutoff_date),
        )
        sample = [
            {"amazon_order_id": r[0], "stage": r[1], "actual_ship_date": str(r[2]), "order_total_amount": float(r[3])}
            for r in cur.fetchall()
        ]
    return {
        "count": int(count or 0),
        "min_ship_date": str(min_date) if min_date else None,
        "max_ship_date": str(max_date) if max_date else None,
        "sample": sample,
    }


def count_unposted_by_purchase_month(conn: psycopg.Connection, month_start: str, month_end: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM amazon_us_orders
            WHERE stage = ANY(%s) AND purchase_date >= %s AND purchase_date < %s
            """,
            (list(_PRECUTOFF_UNPOSTED_STAGES), month_start, month_end),
        )
        count = cur.fetchone()[0]
        cur.execute(
            """
            SELECT amazon_order_id, stage, order_status, purchase_date, actual_ship_date
            FROM amazon_us_orders
            WHERE stage = ANY(%s) AND purchase_date >= %s AND purchase_date < %s
            ORDER BY purchase_date DESC
            LIMIT 10
            """,
            (list(_PRECUTOFF_UNPOSTED_STAGES), month_start, month_end),
        )
        sample = [
            {"amazon_order_id": r[0], "stage": r[1], "order_status": r[2], "purchase_date": str(r[3]), "actual_ship_date": str(r[4]) if r[4] else None}
            for r in cur.fetchall()
        ]
    return {"count": int(count or 0), "sample": sample}


def delete_unposted_by_purchase_month(conn: psycopg.Connection, month_start: str, month_end: str) -> dict:
    """Same shape as delete_precutoff_unposted, scoped by purchase_date
    instead of actual_ship_date -- for orders that never shipped at all
    (actual_ship_date IS NULL) so the ship-date-based purge can't catch
    them, but are old enough purchase-date-wise to be dead, not
    in-flight. Caller is responsible for confirming (e.g. via
    debug_june_purchase_unposted_breakdown) that nothing in scope has
    actual_ship_date >= the posting cutoff, i.e. is still legitimately
    postable -- this function does not re-check that itself."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM amazon_us_invoice_log
            WHERE amazon_order_id IN (
              SELECT amazon_order_id FROM amazon_us_orders
              WHERE stage = ANY(%s) AND purchase_date >= %s AND purchase_date < %s
            )
            """,
            (list(_PRECUTOFF_UNPOSTED_STAGES), month_start, month_end),
        )
        invoice_log_deleted = cur.rowcount
        cur.execute(
            """
            DELETE FROM amazon_us_orders
            WHERE stage = ANY(%s) AND purchase_date >= %s AND purchase_date < %s
            """,
            (list(_PRECUTOFF_UNPOSTED_STAGES), month_start, month_end),
        )
        orders_deleted = cur.rowcount
    conn.commit()
    return {"orders_deleted": orders_deleted, "invoice_log_deleted": invoice_log_deleted}


def delete_precutoff_unposted(conn: psycopg.Connection, cutoff_date: str) -> dict:
    """Deletes matching amazon_us_orders rows (amazon_us_order_lines cascade
    automatically) plus any amazon_us_invoice_log rows referencing them --
    that table has no FK, so a plain orders delete would leave orphaned
    failed-attempt log rows pointing at an amazon_order_id that no longer
    exists."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM amazon_us_invoice_log
            WHERE amazon_order_id IN (
              SELECT amazon_order_id FROM amazon_us_orders
              WHERE stage = ANY(%s) AND actual_ship_date < %s
            )
            """,
            (list(_PRECUTOFF_UNPOSTED_STAGES), cutoff_date),
        )
        invoice_log_deleted = cur.rowcount
        cur.execute(
            """
            DELETE FROM amazon_us_orders
            WHERE stage = ANY(%s) AND actual_ship_date < %s
            """,
            (list(_PRECUTOFF_UNPOSTED_STAGES), cutoff_date),
        )
        orders_deleted = cur.rowcount
    conn.commit()
    return {"orders_deleted": orders_deleted, "invoice_log_deleted": invoice_log_deleted}
