"""Walmart Returns fetch — backfill and daily sync with watermark."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import psycopg

from walmart.credentials import DATABASE_URL, WM_API_BASE, WM_BASIC_AUTH, WM_SVC_NAME
from walmart.walmart_store import datetime_to_utc_api_string, utc_iso_string_to_datetime

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
RETURN_TO_SENDER_REASON = "RETURN_TO_SENDER"


# ---------------------------------------------------------------------------
# HTTP helper (identical to order_updation._http_json)
# ---------------------------------------------------------------------------

def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    data: bytes | None = None,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Walmart Returns API {exc.code} {method} {url}: {body}") from exc


# ---------------------------------------------------------------------------
# Authentication (identical to order_updation.get_access_token)
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    """Exchange client credentials for a short-lived access token."""
    payload = _http_json(
        "POST",
        f"{WM_API_BASE}/token",
        headers={
            "Authorization": WM_BASIC_AUTH,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "WM_SVC.NAME": WM_SVC_NAME,
        },
        data=b"grant_type=client_credentials",
    )
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Token response missing access_token: {payload}")
    return str(token)


def _returns_headers(access_token: str) -> dict[str, str]:
    return {
        "WM_SEC.ACCESS_TOKEN": access_token,
        "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
        "WM_SVC.NAME": WM_SVC_NAME,
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------

def _returns_url(
    *,
    start_date: str,
    end_date: str,
    limit: int = DEFAULT_LIMIT,
    use_creation: bool = False,
) -> str:
    """Build the /returns query URL.

    backfill uses returnCreationStartDate/EndDate.
    sync uses returnLastModifiedStartDate/EndDate.
    """
    start_key = "returnCreationStartDate" if use_creation else "returnLastModifiedStartDate"
    end_key = "returnCreationEndDate" if use_creation else "returnLastModifiedEndDate"
    query = urlencode({start_key: start_date, end_key: end_date, "limit": limit})
    return f"{WM_API_BASE}/returns?{query}"


# ---------------------------------------------------------------------------
# Helpers for extracting fields from return orders
# ---------------------------------------------------------------------------

def _iso_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(utc_iso_string_to_datetime(value).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _max_status_time_ms(return_orders: list[dict[str, Any]]) -> int | None:
    max_ms: int | None = None
    for ret in return_orders:
        for line in ret.get("returnOrderLines") or []:
            ms = _iso_to_ms(line.get("statusTime"))
            if ms is not None and (max_ms is None or ms > max_ms):
                max_ms = ms
    return max_ms


def _credit_note_status_for_insert(lines: list[dict[str, Any]]) -> str:
    """'Not Applicable' only when every line reason is RETURN_TO_SENDER, else 'Not Posted'."""
    if lines and all(
        (line.get("returnReason") or "") == RETURN_TO_SENDER_REASON for line in lines
    ):
        return "Not Applicable"
    return "Not Posted"


def _build_upsert_row(ret: dict[str, Any], fetched_at: datetime) -> dict[str, Any]:
    lines: list[dict[str, Any]] = ret.get("returnOrderLines") or []

    po_ids = sorted({str(ln["purchaseOrderId"]) for ln in lines if ln.get("purchaseOrderId")})
    reasons = sorted({str(ln["returnReason"]) for ln in lines if ln.get("returnReason")})

    # status / statusTime — use the last line's values (all lines consistent in practice)
    status = (lines[-1].get("status") if lines else None) or None
    status_time_str = max(
        (ln["statusTime"] for ln in lines if ln.get("statusTime")),
        default=None,
    )
    delivery_status = (lines[-1].get("currentDeliveryStatus") if lines else None) or None
    refund_status = (lines[-1].get("currentRefundStatus") if lines else None) or None

    refund_block = ret.get("totalRefundAmount") or {}

    return_order_date_str = ret.get("returnOrderDate")

    return {
        "return_order_id": str(ret["returnOrderId"]),
        "customer_order_id": ret.get("customerOrderId"),
        "return_order_date": (
            utc_iso_string_to_datetime(return_order_date_str)
            if return_order_date_str
            else None
        ),
        "refund_mode": ret.get("refundMode"),
        "total_refund_amount": refund_block.get("currencyAmount"),
        "total_refund_currency": refund_block.get("currencyUnit"),
        "purchase_order_ids": ",".join(po_ids) if po_ids else None,
        "return_reasons": ",".join(reasons) if reasons else None,
        "status": status,
        "status_time": (
            utc_iso_string_to_datetime(status_time_str) if status_time_str else None
        ),
        "current_delivery_status": delivery_status,
        "current_refund_status": refund_status,
        "credit_note_status_for_insert": _credit_note_status_for_insert(lines),
        "raw_json": json.dumps(ret),
        "fetched_at": fetched_at,
    }


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_UPSERT_RETURN_INSERT = """
INSERT INTO walmart_returns (
    return_order_id, customer_order_id, return_order_date, refund_mode,
    total_refund_amount, total_refund_currency,
    purchase_order_ids, return_reasons,
    status, status_time, current_delivery_status, current_refund_status,
    credit_note_status, raw_json, fetched_at
) VALUES (
    %(return_order_id)s, %(customer_order_id)s, %(return_order_date)s, %(refund_mode)s,
    %(total_refund_amount)s, %(total_refund_currency)s,
    %(purchase_order_ids)s, %(return_reasons)s,
    %(status)s, %(status_time)s, %(current_delivery_status)s, %(current_refund_status)s,
    %(credit_note_status_for_insert)s, %(raw_json)s::jsonb, %(fetched_at)s
)
ON CONFLICT (return_order_id) DO UPDATE SET
    customer_order_id       = EXCLUDED.customer_order_id,
    return_order_date       = EXCLUDED.return_order_date,
    refund_mode             = EXCLUDED.refund_mode,
    total_refund_amount     = EXCLUDED.total_refund_amount,
    total_refund_currency   = EXCLUDED.total_refund_currency,
    purchase_order_ids      = EXCLUDED.purchase_order_ids,
    return_reasons          = EXCLUDED.return_reasons,
    status                  = EXCLUDED.status,
    status_time             = EXCLUDED.status_time,
    current_delivery_status = EXCLUDED.current_delivery_status,
    current_refund_status   = EXCLUDED.current_refund_status,
    raw_json                = EXCLUDED.raw_json,
    fetched_at              = EXCLUDED.fetched_at,
    updated_at              = now()
WHERE
    walmart_returns.status                  IS DISTINCT FROM EXCLUDED.status
    OR walmart_returns.status_time          IS DISTINCT FROM EXCLUDED.status_time
    OR walmart_returns.current_delivery_status IS DISTINCT FROM EXCLUDED.current_delivery_status
    OR walmart_returns.current_refund_status   IS DISTINCT FROM EXCLUDED.current_refund_status
    OR walmart_returns.purchase_order_ids   IS DISTINCT FROM EXCLUDED.purchase_order_ids
    OR walmart_returns.return_reasons       IS DISTINCT FROM EXCLUDED.return_reasons
RETURNING
    (xmax = 0) AS inserted,
    return_order_id,
    status
"""

_INSERT_SYNC_LOG = """
INSERT INTO walmart_return_sync_log
    (status, sync_mode, from_date, to_date, record_count,
     max_status_time_ms, next_last_modified_start, message)
VALUES
    (%(status)s, %(sync_mode)s, %(from_date)s, %(to_date)s, %(record_count)s,
     %(max_status_time_ms)s, %(next_last_modified_start)s, %(message)s)
RETURNING id
"""

_FETCH_LAST_WATERMARK = """
SELECT max_status_time_ms, next_last_modified_start
FROM walmart_return_sync_log
WHERE status = 'success' AND max_status_time_ms IS NOT NULL
ORDER BY id DESC
LIMIT 1
"""


def _fetch_watermark(conn: psycopg.Connection) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(_FETCH_LAST_WATERMARK)
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return {
        "max_status_time_ms": int(row[0]),
        "next_last_modified_start": str(row[1]),
    }


def _write_sync_log(
    conn: psycopg.Connection,
    *,
    status: str,
    sync_mode: str,
    from_date: str,
    to_date: str,
    record_count: int,
    max_status_time_ms: int | None,
    next_last_modified_start: str | None,
    message: str | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            _INSERT_SYNC_LOG,
            {
                "status": status,
                "sync_mode": sync_mode,
                "from_date": utc_iso_string_to_datetime(from_date),
                "to_date": utc_iso_string_to_datetime(to_date),
                "record_count": record_count,
                "max_status_time_ms": max_status_time_ms,
                "next_last_modified_start": next_last_modified_start,
                "message": message,
            },
        )
        row = cur.fetchone()
    conn.commit()
    return int(row[0]) if row else -1


_MARK_ORDER_HAS_RETURN = """
UPDATE walmart_order_headers
SET has_return = true
WHERE purchase_order_id = ANY(%(po_ids)s::text[])
  AND has_return = false
"""


def _po_ids_from_row(row: dict[str, Any]) -> list[str]:
    raw = row.get("purchase_order_ids") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def _upsert_page(
    conn: psycopg.Connection,
    rows: list[dict[str, Any]],
    *,
    sync_mode: str,
) -> dict[str, int]:
    """Upsert one page of return rows, stamp has_return on linked orders, commit."""
    inserted = 0
    updated = 0
    skipped = 0
    linked_po_ids: set[str] = set()

    with conn.cursor() as cur:
        for row in rows:
            if sync_mode == "sync":
                # In sync mode skip INSERT — only touch existing rows
                cur.execute(
                    "SELECT 1 FROM walmart_returns WHERE return_order_id = %(return_order_id)s",
                    {"return_order_id": row["return_order_id"]},
                )
                if cur.fetchone() is None:
                    skipped += 1
                    continue

            cur.execute(_UPSERT_RETURN_INSERT, row)
            result = cur.fetchone()
            if result is None:
                # WHERE clause blocked the update — no fields changed
                skipped += 1
            else:
                was_inserted = result[0]
                if was_inserted:
                    inserted += 1
                else:
                    updated += 1

            # Collect PO IDs regardless of insert/update/skip so we always
            # keep walmart_order_headers.has_return in sync.
            linked_po_ids.update(_po_ids_from_row(row))

        # Stamp has_return = true on every linked order in one query per page.
        if linked_po_ids:
            cur.execute(_MARK_ORDER_HAS_RETURN, {"po_ids": list(linked_po_ids)})

    conn.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


# ---------------------------------------------------------------------------
# Core fetch loop
# ---------------------------------------------------------------------------

def fetch_returns(
    start_date: str,
    end_date: str,
    *,
    use_creation: bool,
    limit: int = DEFAULT_LIMIT,
    access_token: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch all return pages for the given date window."""
    token = access_token or get_access_token()
    headers = _returns_headers(token)

    url: str | None = _returns_url(
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        use_creation=use_creation,
    )

    all_returns: list[dict[str, Any]] = []
    page_num = 0

    while url:
        page_num += 1
        logger.info("Fetching Walmart returns page=%s url=%s", page_num, url)
        page = _http_json("GET", url, headers=headers)
        batch: list[dict[str, Any]] = page.get("returnOrders") or []
        all_returns.extend(batch)
        logger.info(
            "Returns page %s: fetched %s (running total=%s)",
            page_num,
            len(batch),
            len(all_returns),
        )
        next_cursor = (page.get("meta") or {}).get("nextCursor")
        if not next_cursor:
            break
        # nextCursor always starts with "?"
        url = f"{WM_API_BASE}/returns{next_cursor}"

    logger.info(
        "Returns fetch complete: total=%s pages=%s window=%s -> %s",
        len(all_returns),
        page_num,
        start_date,
        end_date,
    )
    return all_returns


# ---------------------------------------------------------------------------
# Main run functions
# ---------------------------------------------------------------------------

def get_db_connection() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)


def run_returns_backfill(
    *,
    start_date: str,
    end_date: str,
    limit: int = DEFAULT_LIMIT,
    store: bool = True,
) -> dict[str, Any]:
    """Backfill using returnCreationStartDate / returnCreationEndDate.

    Inserts new rows and updates existing ones.
    """
    logger.info("Returns backfill: %s -> %s", start_date, end_date)
    fetched_at = datetime.now(timezone.utc)

    all_returns = fetch_returns(
        start_date, end_date, use_creation=True, limit=limit
    )

    totals = {"inserted": 0, "updated": 0, "skipped": 0}

    if store and all_returns:
        with get_db_connection() as conn:
            # Build upsert rows
            rows = [_build_upsert_row(r, fetched_at) for r in all_returns]

            # Process in pages of DEFAULT_LIMIT to commit incrementally
            for i in range(0, len(rows), DEFAULT_LIMIT):
                chunk = rows[i : i + DEFAULT_LIMIT]
                counts = _upsert_page(conn, chunk, sync_mode="backfill")
                totals["inserted"] += counts["inserted"]
                totals["updated"] += counts["updated"]
                totals["skipped"] += counts["skipped"]

            max_ms = _max_status_time_ms(all_returns)
            next_start = (
                datetime_to_utc_api_string(
                    datetime.fromtimestamp(max_ms / 1000.0, tz=timezone.utc)
                )
                if max_ms is not None
                else None
            )
            _write_sync_log(
                conn,
                status="success",
                sync_mode="backfill",
                from_date=start_date,
                to_date=end_date,
                record_count=len(all_returns),
                max_status_time_ms=max_ms,
                next_last_modified_start=next_start,
            )

    return {
        "sync_mode": "backfill",
        "start_date": start_date,
        "end_date": end_date,
        "total_returns": len(all_returns),
        **totals,
    }


def run_returns_sync(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = DEFAULT_LIMIT,
    store: bool = True,
) -> dict[str, Any]:
    """Daily sync using returnLastModifiedStartDate / returnLastModifiedEndDate.

    start_date defaults to the watermark from walmart_return_sync_log.
    end_date defaults to now().
    UPDATE only — never inserts new rows.
    """
    with get_db_connection() as conn:
        if start_date is None:
            watermark = _fetch_watermark(conn)
            if watermark is None:
                raise RuntimeError(
                    "No walmart_return_sync_log watermark found. "
                    "Run returns_backfill first, or pass start_date explicitly."
                )
            start_date = watermark["next_last_modified_start"]
            logger.info("Using watermark as start: %s", start_date)

        if end_date is None:
            end_date = datetime_to_utc_api_string(datetime.now(timezone.utc))
            logger.info("Using current UTC as end: %s", end_date)

        logger.info("Returns sync: %s -> %s", start_date, end_date)
        fetched_at = datetime.now(timezone.utc)

        all_returns = fetch_returns(start_date, end_date, use_creation=False, limit=limit)

        totals = {"inserted": 0, "updated": 0, "skipped": 0}

        if store and all_returns:
            rows = [_build_upsert_row(r, fetched_at) for r in all_returns]
            for i in range(0, len(rows), DEFAULT_LIMIT):
                chunk = rows[i : i + DEFAULT_LIMIT]
                counts = _upsert_page(conn, chunk, sync_mode="sync")
                totals["inserted"] += counts["inserted"]
                totals["updated"] += counts["updated"]
                totals["skipped"] += counts["skipped"]

            max_ms = _max_status_time_ms(all_returns)
            next_start = (
                datetime_to_utc_api_string(
                    datetime.fromtimestamp(max_ms / 1000.0, tz=timezone.utc)
                )
                if max_ms is not None
                else None
            )
            _write_sync_log(
                conn,
                status="success",
                sync_mode="sync",
                from_date=start_date,
                to_date=end_date,
                record_count=len(all_returns),
                max_status_time_ms=max_ms,
                next_last_modified_start=next_start,
            )

    return {
        "sync_mode": "sync",
        "start_date": start_date,
        "end_date": end_date,
        "total_returns": len(all_returns),
        **totals,
    }
