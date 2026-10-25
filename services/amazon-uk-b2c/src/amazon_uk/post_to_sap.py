"""Post shipped Amazon UK orders to SAP B1 as AR Invoices (one invoice per order).

Triggered only by handlers.post_invoices_handler — the daily EventBridge
schedule (gated by AMZ_UK_POSTING_CUTOFF_DATE); the dashboard is read-only.

Flow per order:
  1. Skip if already recorded posted in amazonuk.sap_postings
  2. Check SAP directly for an existing invoice (NumAtCard) — recovers from a
     prior run that posted successfully but crashed before recording it
     locally, so a retry never double-posts (real incident in the sibling
     Amazon US pipeline when this check was skipped)
  3. Build the payload (all of the order's items) and allocate FEFO batches
  4. POST to SAP, record the result
"""

import datetime as _dt

from psycopg.types.json import Json

from amazon_uk import applog
from amazon_uk import config
from amazon_uk import store
from amazon_uk import hana_client
from amazon_uk import sap_client
from amazon_uk.invoice_payload import build_invoice_payload, get_sku_mapping

log = applog.get_logger("amz_sync")

CARD_CODE = "C0003"
SERIES = 74
WAREHOUSE = "AMZ-FBA"


def posting_cutoff() -> str:
    """AMZ_UK_POSTING_CUTOFF_DATE (YYYY-MM-DD) — required. The scheduled
    posting run only touches orders whose ship date (= the invoice DocDate)
    is on/after it, so an old order that changes status can never be
    invoiced automatically. Deliberately no default: an unattended run with
    no cutoff would post everything, so a missing/invalid value stops the
    run (and the crash alert fires) before any SAP or HANA call."""
    raw = config.get("AMZ_UK_POSTING_CUTOFF_DATE")  # raises RuntimeError if unset
    try:
        return _dt.date.fromisoformat(raw).isoformat()
    except ValueError:
        raise RuntimeError(
            f"AMZ_UK_POSTING_CUTOFF_DATE must be YYYY-MM-DD, got {raw!r}"
        ) from None


def _shipped_orders_not_posted(start_date: str, end_date: str) -> list[str]:
    """Manual/backfill scope — Shipped + not-posted orders whose purchase_date
    falls in [start_date, end_date]. Misses an order purchased outside this
    range whose status only just changed to Shipped — see
    _shipped_orders_not_posted_all for the daily automated flow."""
    conn = store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT oi.amazon_order_id
                FROM amazonuk.order_items oi
                LEFT JOIN amazonuk.sap_postings sp
                    ON sp.amazon_order_id = oi.amazon_order_id AND (sp.status = 'posted' OR sp.dead)
                WHERE oi.order_status = 'Shipped'
                  AND oi.purchase_date::date BETWEEN %(start)s AND %(end)s
                  AND sp.amazon_order_id IS NULL
                ORDER BY 1
                """,
                {"start": start_date, "end": end_date},
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _shipped_orders_not_posted_all(cutoff: str) -> list[str]:
    """Daily automated scope — every Shipped + not-posted order shipped on or
    after ``cutoff``, regardless of purchase date. An order purchased days ago
    that only just flipped to Shipped (caught by
    sync_amazonuk.sync_incremental's lastUpdatedAfter watermark) is exactly
    what this is for. Orders with no shipment_date yet are not candidates
    until the shipment report supplies one (they could not be posted anyway)."""
    conn = store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT oi.amazon_order_id
                FROM amazonuk.order_items oi
                LEFT JOIN amazonuk.sap_postings sp
                    ON sp.amazon_order_id = oi.amazon_order_id AND (sp.status = 'posted' OR sp.dead)
                WHERE oi.order_status = 'Shipped'
                  AND oi.shipment_date::date >= %(cutoff)s
                  AND sp.amazon_order_id IS NULL
                ORDER BY 1
                """,
                {"cutoff": cutoff},
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _failed_order_ids(cutoff: str) -> list[str]:
    """Every order recorded as status='failed' that is inside the posting
    cutoff — the retry target."""
    conn = store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sp.amazon_order_id
                FROM amazonuk.sap_postings sp
                WHERE sp.status = 'failed' AND NOT sp.dead
                  AND EXISTS (SELECT 1 FROM amazonuk.order_items oi
                              WHERE oi.amazon_order_id = sp.amazon_order_id
                                AND oi.shipment_date::date >= %(cutoff)s)
                ORDER BY 1
                """,
                {"cutoff": cutoff},
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def retry_failed_orders() -> dict:
    """Re-attempt every order currently recorded as failed. Stock/period-
    lock/VAT-anomaly failures only clear once the underlying SAP/HANA data
    does, so this is expected to still show some failures on each retry —
    that's not a bug, see _get_posted_locally / _post_one_order for how
    each attempt is (re)recorded."""
    return _post_order_ids(_failed_order_ids(posting_cutoff()))


def _order_rows(amazon_order_id: str) -> list[dict]:
    conn = store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM amazonuk.order_items WHERE amazon_order_id = %s", (amazon_order_id,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _record(amazon_order_id: str, status: str, doc_entry: str = None, doc_num: str = None, error: str = None,
            sap_response: dict = None) -> dict:
    """Upsert the posting row. A 'failed' record counts one attempt and turns
    the row dead at store.MAX_POST_ATTEMPTS. Returns {"attempts", "dead"} for a
    failure, {} otherwise."""
    failed = status == "failed"
    conn = store.connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO amazonuk.sap_postings
                        (amazon_order_id, sap_doc_entry, sap_doc_num, status, error, sap_response, posted_at,
                         attempts, dead)
                    VALUES (%(id)s, %(entry)s, %(num)s, %(status)s, %(error)s, %(resp)s, now(),
                            %(first)s, %(failed)s AND %(first)s >= %(max)s)
                    ON CONFLICT (amazon_order_id) DO UPDATE SET
                        sap_doc_entry = EXCLUDED.sap_doc_entry, sap_doc_num = EXCLUDED.sap_doc_num,
                        status = EXCLUDED.status, error = EXCLUDED.error,
                        sap_response = EXCLUDED.sap_response, posted_at = now(),
                        attempts = amazonuk.sap_postings.attempts + %(first)s,
                        dead = %(failed)s AND amazonuk.sap_postings.attempts + %(first)s >= %(max)s
                    RETURNING attempts, dead
                    """,
                    {"id": amazon_order_id, "entry": doc_entry, "num": doc_num, "status": status,
                     "error": error, "resp": Json(sap_response) if sap_response is not None else None,
                     "first": 1 if failed else 0, "failed": failed, "max": store.MAX_POST_ATTEMPTS},
                )
                attempts, dead = cur.fetchone()
    finally:
        conn.close()
    if failed:
        if dead:
            log.error(f"{amazon_order_id}: failed {attempts} times - marked dead, automated runs will skip it: {error}")
        return {"attempts": attempts, "dead": dead}
    return {}


def _get_posted_locally(amazon_order_id: str) -> dict | None:
    """Existing recorded 'posted' result from amazonuk.sap_postings, if
    any — the module docstring's step 1, checked before any live SAP call.
    Real gap found live 2026-09-17: this check didn't exist anywhere, so
    post_single_order (the dashboard's per-row Post button, called with a
    raw order_id and no prior filtering) made 2 live SAP calls
    (find_invoice_by_order_id + get_invoice) even for an order already
    known locally to be posted. The batch flow never hit this because
    _shipped_orders_not_posted[_all]'s SQL already excludes posted orders
    before _post_one_order is ever called for them."""
    conn = store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sap_doc_entry, sap_doc_num FROM amazonuk.sap_postings "
                "WHERE amazon_order_id = %s AND status = 'posted'",
                (amazon_order_id,),
            )
            row = cur.fetchone()
            return {"doc_entry": row[0], "doc_num": row[1]} if row else None
    finally:
        conn.close()


def _post_one_order(amazon_order_id: str, hana_cursor, sku_map: dict) -> dict:
    """Post a single order. Returns a result dict with status in
    {"posted", "recovered", "failed"}. Raises HanaUnavailableError —
    caller must NOT record that as a failure (leave it for retry)."""
    already_posted = _get_posted_locally(amazon_order_id)
    if already_posted:
        return {"order": amazon_order_id, "status": "recovered", **already_posted}

    existing = sap_client.find_invoice_by_order_id(amazon_order_id)
    if existing:
        # find_invoice_by_order_id only ever fetched DocEntry/DocNum via
        # $select — fetch the full invoice so sap_response reflects the
        # real SAP object here too, not just the two extracted fields.
        full = sap_client.get_invoice(existing["doc_entry"])
        _record(amazon_order_id, "posted", existing["doc_entry"], existing["doc_num"], sap_response=full)
        return {"order": amazon_order_id, "status": "recovered", **existing}

    rows = _order_rows(amazon_order_id)
    if not rows:
        error = "Order not found"
        info = _record(amazon_order_id, "failed", error=error)
        return {"order": amazon_order_id, "status": "failed", "error": error, **info}

    payload, skipped = build_invoice_payload(rows, hana_cursor, WAREHOUSE, CARD_CODE, SERIES, sku_map)
    if payload is None:
        error = f"No postable lines: {skipped}"
        info = _record(amazon_order_id, "failed", error=error)
        return {"order": amazon_order_id, "status": "failed", "error": error, **info}

    try:
        sap_result = sap_client.post_invoice(payload)
    except sap_client.SapPostError as e:
        error = str(e)[:1000]
        info = _record(amazon_order_id, "failed", error=error, sap_response=e.response_body)
        return {"order": amazon_order_id, "status": "failed", "error": error, **info}

    _record(amazon_order_id, "posted", sap_result["doc_entry"], sap_result["doc_num"],
            sap_response=sap_result["response"])
    return {"order": amazon_order_id, "status": "posted",
            "doc_entry": sap_result["doc_entry"], "doc_num": sap_result["doc_num"]}


def _post_order_ids(order_ids: list[str]) -> dict:
    posted = failed = 0
    results = []

    # Fetched once for the whole batch — it doesn't change mid-run, so
    # there's no reason to re-query it per order (was 1 extra DB connection
    # per order before this fix).
    sku_map = get_sku_mapping()

    hana_conn, hana_cursor = hana_client.open_connection()
    try:
        for amazon_order_id in order_ids:
            try:
                result = _post_one_order(amazon_order_id, hana_cursor, sku_map)
                results.append(result)
                if result["status"] in ("posted", "recovered"):
                    posted += 1
                else:
                    failed += 1

            except hana_client.HanaUnavailableError as e:
                results.append({"order": amazon_order_id, "status": "hana_down", "error": str(e)})
                break  # HANA down — stop, leave remaining orders unposted for retry

            except Exception as e:
                error = str(e)[:1000]
                info = _record(amazon_order_id, "failed", error=error)
                failed += 1
                results.append({"order": amazon_order_id, "status": "failed", "error": error, **info})
    finally:
        hana_client.close(hana_conn, hana_cursor)

    return {"total": len(order_ids), "posted": posted, "failed": failed, "results": results}


def post_shipped_orders(start_date: str, end_date: str) -> dict:
    """Manual/backfill — see _shipped_orders_not_posted."""
    return _post_order_ids(_shipped_orders_not_posted(start_date, end_date))


def post_all_shipped_orders() -> dict:
    """Daily automated flow — see _shipped_orders_not_posted_all, limited to
    orders shipped on/after AMZ_UK_POSTING_CUTOFF_DATE (required). Runs an
    hour after the fetch Lambda so newly-Shipped orders (however old their
    purchase date) have already been re-fetched and are eligible."""
    return _post_order_ids(_shipped_orders_not_posted_all(posting_cutoff()))


def preview_invoice_payload(amazon_order_id: str) -> dict:
    """Build the AR Invoice payload for one order WITHOUT posting it — lets
    the dashboard show exactly what would be sent to SAP before committing.
    Returns {"payload": dict | None, "skipped": list}.
    """
    hana_conn, hana_cursor = hana_client.open_connection()
    try:
        rows = _order_rows(amazon_order_id)
        if not rows:
            return {"payload": None, "skipped": [], "error": "Order not found"}
        payload, skipped = build_invoice_payload(rows, hana_cursor, WAREHOUSE, CARD_CODE, SERIES, get_sku_mapping())
        return {"payload": payload, "skipped": skipped}
    finally:
        hana_client.close(hana_conn, hana_cursor)


def post_single_order(amazon_order_id: str) -> dict:
    """Post exactly one order. Used by the dashboard's per-row 'Post to SAP' button."""
    hana_conn, hana_cursor = hana_client.open_connection()
    try:
        return _post_one_order(amazon_order_id, hana_cursor, get_sku_mapping())
    except hana_client.HanaUnavailableError as e:
        return {"order": amazon_order_id, "status": "hana_down", "error": str(e)}
    except Exception as e:
        error = str(e)[:1000]
        info = _record(amazon_order_id, "failed", error=error)
        return {"order": amazon_order_id, "status": "failed", "error": error, **info}
    finally:
        hana_client.close(hana_conn, hana_cursor)
