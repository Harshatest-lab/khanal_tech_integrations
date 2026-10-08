"""Post RELEASED Amazon UK refunds to SAP B1 as AR Credit Memos, one per
refund transaction (not one per order — an order can get more than one
separate RELEASED refund over time; see refund_events).

Triggered by handlers.post_credit_notes_handler — the daily EventBridge
schedule (gated by AMZ_UK_POSTING_CUTOFF_DATE, the same cutoff
post_to_sap.py uses for invoices — applied here against the refund's
posted_date instead of shipment_date); the dashboard is read-only.

Flow per refund transaction:
  1. Skip if already recorded posted in amazonuk.sap_credit_notes
  2. Skip if the order has no posted AR Invoice yet (amazonuk.sap_postings) —
     nothing to credit against
  3. Build the payload, then check SAP directly for an existing Credit Memo
     matching it (recovers from a prior run that posted successfully but
     crashed before recording it locally — see credit_note_payload.py's
     _document_signature for how "matching" is decided)
  4. POST to SAP

Batch DB access: a run over N refunds opens ONE connection for the whole
batch and bulk-fetches everything (refund_events/refund_items via
transaction_id = ANY(...), sap_postings via amazon_order_id = ANY(...),
sap_credit_notes via transaction_id = ANY(...) — all three already
indexed, transaction_id/amazon_order_id are primary keys) before looping
in memory. post_single_refund and preview_credit_note_payload reuse the
exact same bulk functions with a single-element id list, so there's one
code path, not two.
"""

import datetime as _dt

from psycopg.types.json import Json

from amazon_uk import applog
from amazon_uk import config
from amazon_uk import sap_client
from amazon_uk import store
from amazon_uk.credit_note_payload import build_credit_note_payload
from amazon_uk.invoice_payload import get_sku_mapping

log = applog.get_logger("amz_sync")

# SAP B1 Series number for AR Credit Memos (ORIN).
# NOT the same series as AR Invoices (post_to_sap.py's SERIES = 74).
CREDIT_NOTE_SERIES = 76


def posting_cutoff() -> str:
    """Same required AMZ_UK_POSTING_CUTOFF_DATE post_to_sap.py uses,
    applied here to the refund's posted_date instead of shipment_date —
    one operational knob, not two. Deliberately no default: an unattended
    run with no cutoff would post everything, so a missing/invalid value
    stops the run (and the crash alert fires) before any SAP call."""
    raw = config.get("AMZ_UK_POSTING_CUTOFF_DATE")  # raises RuntimeError if unset
    try:
        return _dt.date.fromisoformat(raw).isoformat()
    except ValueError:
        raise RuntimeError(
            f"AMZ_UK_POSTING_CUTOFF_DATE must be YYYY-MM-DD, got {raw!r}"
        ) from None


def _refunds_not_posted(conn, start_date: str, end_date: str) -> list[str]:
    """RELEASED refund transactions, posted within [start_date, end_date],
    whose order already has a posted invoice and that don't have a
    'posted' row in sap_credit_notes yet. Returns transaction_ids."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT re.transaction_id
            FROM amazonuk.refund_events re
            JOIN amazonuk.sap_postings sp
                ON sp.amazon_order_id = re.amazon_order_id AND sp.status = 'posted'
            LEFT JOIN amazonuk.sap_credit_notes cn
                ON cn.transaction_id = re.transaction_id AND cn.status = 'posted'
            WHERE re.transaction_status = 'RELEASED'
              AND re.posted_date::date BETWEEN %(start)s AND %(end)s
              AND cn.transaction_id IS NULL
            ORDER BY re.posted_date
            """,
            {"start": start_date, "end": end_date},
        )
        return [row[0] for row in cur.fetchall()]


def _refunds_not_posted_all(conn, cutoff: str) -> list[str]:
    """Daily automated scope — every not-posted RELEASED refund posted on
    or after ``cutoff`` whose order already has a posted invoice. Mirrors
    post_to_sap._shipped_orders_not_posted_all."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT re.transaction_id
            FROM amazonuk.refund_events re
            JOIN amazonuk.sap_postings sp
                ON sp.amazon_order_id = re.amazon_order_id AND sp.status = 'posted'
            LEFT JOIN amazonuk.sap_credit_notes cn
                ON cn.transaction_id = re.transaction_id AND cn.status = 'posted'
            WHERE re.transaction_status = 'RELEASED'
              AND re.posted_date::date >= %(cutoff)s
              AND cn.transaction_id IS NULL
            ORDER BY re.posted_date
            """,
            {"cutoff": cutoff},
        )
        return [row[0] for row in cur.fetchall()]


def _bulk_refund_events_and_items(conn, transaction_ids: list[str]) -> dict[str, tuple[dict, list[dict]]]:
    """{transaction_id: (event, [item, ...])} for every id given — 2 queries
    total (transaction_id = ANY(...), the primary key on both tables) no
    matter how many transactions, instead of 2 queries per transaction."""
    if not transaction_ids:
        return {}
    result: dict[str, tuple[dict, list]] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM amazonuk.refund_events WHERE transaction_id = ANY(%s)", (transaction_ids,))
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            event = dict(zip(cols, row))
            result[event["transaction_id"]] = (event, [])

        cur.execute("SELECT * FROM amazonuk.refund_items WHERE transaction_id = ANY(%s) ORDER BY transaction_id, item_index",
                    (transaction_ids,))
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            item = dict(zip(cols, row))
            txn_id = item["transaction_id"]
            if txn_id in result:
                result[txn_id][1].append(item)
    return result


def _bulk_posted_invoices(conn, amazon_order_ids: list[str]) -> dict[str, str]:
    """{amazon_order_id: sap_doc_entry} for every posted invoice among the
    given orders — amazon_order_id is sap_postings' primary key."""
    if not amazon_order_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT amazon_order_id, sap_doc_entry FROM amazonuk.sap_postings "
            "WHERE amazon_order_id = ANY(%s) AND status = 'posted'",
            (list(amazon_order_ids),),
        )
        return dict(cur.fetchall())


def _bulk_locally_posted(conn, transaction_ids: list[str]) -> dict[str, dict]:
    """{transaction_id: {doc_entry, doc_num}} for every refund already
    recorded posted locally — transaction_id is sap_credit_notes' PK."""
    if not transaction_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT transaction_id, sap_doc_entry, sap_doc_num FROM amazonuk.sap_credit_notes "
            "WHERE transaction_id = ANY(%s) AND status = 'posted'",
            (transaction_ids,),
        )
        return {row[0]: {"doc_entry": row[1], "doc_num": row[2]} for row in cur.fetchall()}


def _record(conn, transaction_id: str, amazon_order_id: str, status: str, doc_entry: str = None,
            doc_num: str = None, error: str = None, sap_response: dict = None) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO amazonuk.sap_credit_notes
                    (transaction_id, amazon_order_id, sap_doc_entry, sap_doc_num, status, error,
                     sap_response, posted_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (transaction_id) DO UPDATE SET
                    sap_doc_entry = EXCLUDED.sap_doc_entry, sap_doc_num = EXCLUDED.sap_doc_num,
                    status = EXCLUDED.status, error = EXCLUDED.error,
                    sap_response = EXCLUDED.sap_response, posted_at = now()
                """,
                (transaction_id, amazon_order_id, doc_entry, doc_num, status, error,
                 Json(sap_response) if sap_response is not None else None),
            )


def _document_signature(doc: dict) -> tuple:
    """(DocDate, {(BaseEntry, BaseLine, Quantity), ...}) — identifies a
    Credit Memo by WHAT it reverses AND WHEN, not by any text field.

    DocDate alone doesn't make this airtight: two genuinely different real
    refunds for the same order/SKU/quantity posted on the exact same
    calendar day would still collide. Narrows the false-positive window a
    lot for free (using data already on the document) without needing a
    new SAP field; a dedicated UDF stamped with the refund's
    transaction_id is still the only fully airtight fix if that residual
    risk needs closing completely.
    """
    line_sig = frozenset(
        (int(l["BaseEntry"]), int(l["BaseLine"]), round(float(l.get("Quantity") or 0), 6))
        for l in (doc.get("DocumentLines") or [])
        if l.get("BaseEntry") is not None and l.get("BaseLine") is not None
    )
    doc_date = str(doc.get("DocDate"))[:10] if doc.get("DocDate") else None
    return (doc_date, line_sig)


def _find_existing_credit_note(amazon_order_id: str, payload: dict) -> dict | None:
    """Disambiguates among every Credit Memo already posted for this order
    (there can legitimately be several — one per RELEASED refund) by
    comparing DocDate + DocumentLines content, since NumAtCard/Comments
    carry only the order id. SAP calls, not DB — unrelated to the batch
    DB-connection reuse above."""
    wanted_sig = _document_signature(payload)
    for candidate in sap_client.find_credit_notes_by_order(amazon_order_id):
        full = sap_client.get_credit_note(candidate["doc_entry"])
        if _document_signature(full) == wanted_sig:
            return {"doc_entry": candidate["doc_entry"], "doc_num": candidate["doc_num"], "response": full}
    return None


def _post_one_refund(conn, transaction_id: str, sku_map: dict, event: dict | None, items: list[dict],
                      invoice_doc_entry: str | None, already_posted: dict | None) -> dict:
    """Post a single refund transaction's Credit Memo, given already-fetched
    data (see the bulk_* functions / module docstring for why this doesn't
    query for its own inputs). Returns a result dict with status in
    {"posted", "recovered", "failed"}."""
    if event is None:
        error = "Refund transaction not found"
        _record(conn, transaction_id, "", "failed", error=error)
        return {"transaction_id": transaction_id, "status": "failed", "error": error}

    amazon_order_id = event["amazon_order_id"]

    if already_posted:
        return {"transaction_id": transaction_id, "order": amazon_order_id, "status": "recovered", **already_posted}

    if not invoice_doc_entry:
        error = "No posted AR Invoice for this order yet — nothing to credit against"
        _record(conn, transaction_id, amazon_order_id, "failed", error=error)
        return {"transaction_id": transaction_id, "order": amazon_order_id, "status": "failed", "error": error}

    invoice = sap_client.get_invoice(invoice_doc_entry)
    payload, skipped = build_credit_note_payload(
        invoice, items, amazon_order_id, transaction_id, event["posted_date"], CREDIT_NOTE_SERIES, sku_map)
    if payload is None:
        error = f"No postable lines: {skipped}"
        _record(conn, transaction_id, amazon_order_id, "failed", error=error)
        return {"transaction_id": transaction_id, "order": amazon_order_id, "status": "failed", "error": error}

    existing = _find_existing_credit_note(amazon_order_id, payload)
    if existing:
        _record(conn, transaction_id, amazon_order_id, "posted", existing["doc_entry"], existing["doc_num"],
                sap_response=existing["response"])
        return {"transaction_id": transaction_id, "order": amazon_order_id, "status": "recovered",
                "doc_entry": existing["doc_entry"], "doc_num": existing["doc_num"]}

    try:
        sap_result = sap_client.post_credit_note(payload)
    except sap_client.SapPostError as e:
        error = str(e)[:1000]
        _record(conn, transaction_id, amazon_order_id, "failed", error=error, sap_response=e.response_body)
        return {"transaction_id": transaction_id, "order": amazon_order_id, "status": "failed", "error": error}

    _record(conn, transaction_id, amazon_order_id, "posted", sap_result["doc_entry"], sap_result["doc_num"],
            sap_response=sap_result["response"])
    return {"transaction_id": transaction_id, "order": amazon_order_id, "status": "posted",
            "doc_entry": sap_result["doc_entry"], "doc_num": sap_result["doc_num"], "skipped_lines": skipped}


def _post_refund_ids(conn, transaction_ids: list[str], sku_map: dict) -> dict:
    """Shared by post_refunds (a date-range batch) and post_single_refund
    (one id) — same bulk-fetch-then-loop-in-memory path either way."""
    if not transaction_ids:
        return {"total": 0, "posted": 0, "failed": 0, "results": []}

    events_and_items = _bulk_refund_events_and_items(conn, transaction_ids)
    order_ids = {e["amazon_order_id"] for e, _ in events_and_items.values()}
    invoices_by_order = _bulk_posted_invoices(conn, list(order_ids))
    locally_posted = _bulk_locally_posted(conn, transaction_ids)

    posted = failed = 0
    results = []
    for transaction_id in transaction_ids:
        event, items = events_and_items.get(transaction_id, (None, []))
        try:
            result = _post_one_refund(
                conn, transaction_id, sku_map, event, items,
                invoices_by_order.get(event["amazon_order_id"]) if event else None,
                locally_posted.get(transaction_id),
            )
            results.append(result)
            if result["status"] in ("posted", "recovered"):
                posted += 1
            else:
                failed += 1
        except Exception as e:
            error = str(e)[:1000]
            _record(conn, transaction_id, event["amazon_order_id"] if event else "", "failed", error=error)
            failed += 1
            results.append({"transaction_id": transaction_id, "status": "failed", "error": error})

    return {"total": posted + failed, "posted": posted, "failed": failed, "results": results}


def post_refunds(start_date: str, end_date: str) -> dict:
    """Manual/backfill scope — see _refunds_not_posted."""
    sku_map = get_sku_mapping()
    conn = store.connect()
    try:
        transaction_ids = _refunds_not_posted(conn, start_date, end_date)
        return _post_refund_ids(conn, transaction_ids, sku_map)
    finally:
        conn.close()


def post_all_refunds() -> dict:
    """Daily automated flow — see _refunds_not_posted_all, limited to
    refunds posted on/after AMZ_UK_POSTING_CUTOFF_DATE (required)."""
    cutoff = posting_cutoff()  # validated before any DB/SAP call — see post_to_sap.post_all_shipped_orders
    sku_map = get_sku_mapping()
    conn = store.connect()
    try:
        transaction_ids = _refunds_not_posted_all(conn, cutoff)
        return _post_refund_ids(conn, transaction_ids, sku_map)
    finally:
        conn.close()


def retry_failed_refunds() -> dict:
    """Re-attempt every refund currently recorded as failed, inside the
    posting cutoff. Mirrors post_to_sap.retry_failed_orders."""
    cutoff = posting_cutoff()
    conn = store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cn.transaction_id
                FROM amazonuk.sap_credit_notes cn
                JOIN amazonuk.refund_events re ON re.transaction_id = cn.transaction_id
                WHERE cn.status = 'failed'
                  AND re.posted_date::date >= %(cutoff)s
                ORDER BY 1
                """,
                {"cutoff": cutoff},
            )
            transaction_ids = [row[0] for row in cur.fetchall()]
        sku_map = get_sku_mapping()
        return _post_refund_ids(conn, transaction_ids, sku_map)
    finally:
        conn.close()


def preview_credit_note_payload(transaction_id: str) -> dict:
    """Build the Credit Memo payload for one refund transaction WITHOUT
    posting it — lets the dashboard show exactly what would be sent to SAP,
    same purpose as post_to_sap.preview_invoice_payload.
    Returns {"payload": dict | None, "skipped": list, "error": str | None}.
    """
    conn = store.connect()
    try:
        events_and_items = _bulk_refund_events_and_items(conn, [transaction_id])
        if transaction_id not in events_and_items:
            return {"payload": None, "skipped": [], "error": "Refund transaction not found"}
        event, items = events_and_items[transaction_id]

        amazon_order_id = event["amazon_order_id"]
        invoice_doc_entry = _bulk_posted_invoices(conn, [amazon_order_id]).get(amazon_order_id)
        if not invoice_doc_entry:
            return {"payload": None, "skipped": [],
                    "error": "No posted AR Invoice for this order yet — nothing to credit against"}
    finally:
        conn.close()

    invoice = sap_client.get_invoice(invoice_doc_entry)
    payload, skipped = build_credit_note_payload(
        invoice, items, amazon_order_id, transaction_id, event["posted_date"], CREDIT_NOTE_SERIES,
        get_sku_mapping())
    return {"payload": payload, "skipped": skipped, "error": None}


def post_single_refund(transaction_id: str) -> dict:
    """Post exactly one refund transaction's Credit Memo. Used by the
    dashboard's per-row 'Post to SAP' button on the Refunds page."""
    sku_map = get_sku_mapping()
    conn = store.connect()
    try:
        result = _post_refund_ids(conn, [transaction_id], sku_map)
        if result["results"]:
            return result["results"][0]
        return {"transaction_id": transaction_id, "status": "failed", "error": "Refund transaction not found"}
    except Exception as e:
        error = str(e)[:1000]
        _record(conn, transaction_id, "", "failed", error=error)
        return {"transaction_id": transaction_id, "status": "failed", "error": error}
    finally:
        conn.close()
