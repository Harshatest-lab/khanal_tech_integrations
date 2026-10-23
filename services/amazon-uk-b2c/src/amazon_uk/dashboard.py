"""Dashboard API for the Amazon UK orders pipeline — READ-ONLY.

Lambda port of the standalone dashboard_server.py's GET endpoints, exposed as
``dispatch(method, path, query_string) -> (status, body_str)`` which
handlers.dashboard_handler calls for /api/* requests. The static frontend
lives on its own S3 website bucket (a different origin) and calls this
cross-origin — CORS is handled by API Gateway's native httpApi.cors config
(serverless.yml), scoped to that bucket's origin, not by this module. Auth
is the Cognito JWT authorizer on /api.

Nothing here starts a job: fetching and SAP posting run only from their
EventBridge schedules (amazonUkFetch / amazonUkPostInvoices /
amazonUkFetchRefunds / amazonUkPostCreditNotes). The routes that touch
SAP-side systems — /orders/{id}/sap-preview and /refunds/{id}/sap-trace
(and their trace panels) — only READ HANA/SAP-response history to show the
payload that would be/was posted; nothing here ever POSTs to SAP.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

from amazon_uk import applog
from amazon_uk import store
from amazon_uk import post_credit_notes_to_sap
from amazon_uk import post_to_sap
from amazon_uk.invoice_payload import _extract_breakdowns

log = applog.get_logger("amz_sync")

# amazonuk.order_items is one row per order ITEM — order-level fields (status,
# purchase_date, total, etc.) are duplicated across every item row of the same
# order, so every query here collapses to one row per order first via this CTE.
# last_update_date is Amazon's only "something about this order changed"
# timestamp — there's no separate concept of a distinct "status changed" date,
# so it does double duty as both "Updated" and "Status changed" in the UI.
ORDERS_CTE = """
WITH orders AS (
    SELECT DISTINCT ON (amazon_order_id)
        amazon_order_id, purchase_date, last_update_date, order_status,
        fulfillment_channel, sales_channel, ship_service_level, order_total_amount,
        order_total_currency, number_of_items_shipped, number_of_items_unshipped,
        is_prime, fetched_at,
        MIN(shipment_date) OVER (PARTITION BY amazon_order_id) AS shipment_date
    FROM amazonuk.order_items
    ORDER BY amazon_order_id, purchase_date
)
"""

STATS_SQL = ORDERS_CTE + """
SELECT
    (SELECT COUNT(*) FROM orders)                                AS total_orders,
    (SELECT COUNT(*) FROM amazonuk.order_items)                  AS total_lines,
    (SELECT COUNT(*) FROM orders WHERE order_status = 'Shipped') AS shipped,
    (SELECT COUNT(*) FROM orders WHERE order_status = 'Pending') AS pending,
    (SELECT COUNT(*) FROM orders WHERE order_status = 'Canceled') AS canceled,
    (SELECT COALESCE(SUM(order_total_amount), 0) FROM orders
        WHERE order_status = 'Shipped')                                 AS revenue_gbp,
    (SELECT COUNT(*) FROM orders
        WHERE purchase_date >= now() - interval '7 days')               AS last_7_days,
    (SELECT MAX(fetched_at) FROM orders)                                AS last_fetched,
    (SELECT COUNT(*) FROM amazonuk.sap_postings WHERE status = 'posted') AS sap_posted,
    (SELECT COUNT(*) FROM amazonuk.sap_postings WHERE status = 'failed') AS sap_failed,
    (SELECT MAX(created_at) FROM amazonuk.sync_log)                      AS last_sync_at
"""

STATUS_SQL = ORDERS_CTE + """
SELECT order_status, COUNT(*) AS n
FROM orders GROUP BY order_status ORDER BY n DESC
"""

# Shared across ORDERS_SQL / ORDERS_COUNT_SQL — every EXISTS-based filter
# here checks across ALL line items of the order (not just the one row the
# ORDERS_CTE happens to collapse down to), since sku/item/qty/shipping/
# discount/tax-method are all line-level facts.
ORDERS_WHERE = """
WHERE (%(status)s IS NULL OR o.order_status = %(status)s)
  AND (%(q)s IS NULL OR o.amazon_order_id ILIKE '%%' || %(q)s || '%%')
  AND (
        %(sap_status)s IS NULL
        OR (%(sap_status)s = 'none' AND sp.status IS NULL)
        OR sp.status = %(sap_status)s
      )
  AND (%(order_from)s IS NULL OR o.purchase_date::date >= %(order_from)s)
  AND (%(order_to)s IS NULL OR o.purchase_date::date <= %(order_to)s)
  AND (%(ship_from)s IS NULL OR o.shipment_date::date >= %(ship_from)s)
  AND (%(ship_to)s IS NULL OR o.shipment_date::date <= %(ship_to)s)
  AND (%(updated_from)s IS NULL OR o.last_update_date::date >= %(updated_from)s)
  AND (%(updated_to)s IS NULL OR o.last_update_date::date <= %(updated_to)s)
  AND (%(sku)s IS NULL OR EXISTS (
        SELECT 1 FROM amazonuk.order_items x
        WHERE x.amazon_order_id = o.amazon_order_id AND x.seller_sku ILIKE '%%' || %(sku)s || '%%'
      ))
  AND (%(item)s IS NULL OR EXISTS (
        SELECT 1 FROM amazonuk.order_items x
        WHERE x.amazon_order_id = o.amazon_order_id AND x.title ILIKE '%%' || %(item)s || '%%'
      ))
  AND (%(qty)s IS NULL OR EXISTS (
        SELECT 1 FROM amazonuk.order_items x
        WHERE x.amazon_order_id = o.amazon_order_id AND x.quantity_ordered = %(qty)s
      ))
  AND (
        %(line_count)s IS NULL
        OR (%(line_count)s = 'single' AND (SELECT COUNT(*) FROM amazonuk.order_items x
              WHERE x.amazon_order_id = o.amazon_order_id) = 1)
        OR (%(line_count)s = 'multi' AND (SELECT COUNT(*) FROM amazonuk.order_items x
              WHERE x.amazon_order_id = o.amazon_order_id) > 1)
      )
  AND (
        %(shipping)s IS NULL
        OR (%(shipping)s = 'yes' AND EXISTS (SELECT 1 FROM amazonuk.order_items x
              WHERE x.amazon_order_id = o.amazon_order_id AND COALESCE(x.shipping_price_amount, 0) > 0))
        OR (%(shipping)s = 'no' AND NOT EXISTS (SELECT 1 FROM amazonuk.order_items x
              WHERE x.amazon_order_id = o.amazon_order_id AND COALESCE(x.shipping_price_amount, 0) > 0))
      )
  AND (
        %(discount)s IS NULL
        OR (%(discount)s = 'yes' AND EXISTS (SELECT 1 FROM amazonuk.order_items x
              WHERE x.amazon_order_id = o.amazon_order_id AND COALESCE(x.promotion_discount_amount, 0) > 0))
        OR (%(discount)s = 'no' AND NOT EXISTS (SELECT 1 FROM amazonuk.order_items x
              WHERE x.amazon_order_id = o.amazon_order_id AND COALESCE(x.promotion_discount_amount, 0) > 0))
      )
  AND (
        %(tax_method)s IS NULL
        OR (%(tax_method)s = 'facilitator' AND EXISTS (
              SELECT 1 FROM amazonuk.order_items x
              WHERE x.amazon_order_id = o.amazon_order_id
                AND EXISTS (SELECT 1 FROM jsonb_array_elements(
                      COALESCE(x.raw_order_item->'tax'->'taxCollections', '[]'::jsonb)) tc
                      WHERE tc->>'model' = 'MARKETPLACE_FACILITATOR')
            ))
        OR (%(tax_method)s = 'seller' AND NOT EXISTS (
              SELECT 1 FROM amazonuk.order_items x
              WHERE x.amazon_order_id = o.amazon_order_id
                AND EXISTS (SELECT 1 FROM jsonb_array_elements(
                      COALESCE(x.raw_order_item->'tax'->'taxCollections', '[]'::jsonb)) tc
                      WHERE tc->>'model' = 'MARKETPLACE_FACILITATOR')
            ))
      )
  AND (
        %(has_refund)s IS NULL
        OR (%(has_refund)s = 'yes' AND EXISTS (
              SELECT 1 FROM amazonuk.refund_events re WHERE re.amazon_order_id = o.amazon_order_id
            ))
        OR (%(has_refund)s = 'no' AND NOT EXISTS (
              SELECT 1 FROM amazonuk.refund_events re WHERE re.amazon_order_id = o.amazon_order_id
            ))
      )
"""

ORDERS_SQL = ORDERS_CTE + """
SELECT o.amazon_order_id, o.purchase_date, o.shipment_date, o.last_update_date, o.order_status,
       o.fulfillment_channel, o.sales_channel, o.ship_service_level, o.order_total_amount,
       o.order_total_currency, o.number_of_items_shipped, o.number_of_items_unshipped,
       o.is_prime,
       (SELECT COUNT(*) FROM amazonuk.order_items x WHERE x.amazon_order_id = o.amazon_order_id) AS line_count,
       (SELECT SUM(x.quantity_ordered) FROM amazonuk.order_items x WHERE x.amazon_order_id = o.amazon_order_id) AS total_qty,
       (SELECT STRING_AGG(DISTINCT x.seller_sku, ', ') FROM amazonuk.order_items x
           WHERE x.amazon_order_id = o.amazon_order_id) AS skus,
       (SELECT STRING_AGG(DISTINCT x.title, ' | ') FROM amazonuk.order_items x
           WHERE x.amazon_order_id = o.amazon_order_id) AS titles,
       sp.status AS sap_status, sp.sap_doc_num, sp.error AS sap_error,
       (SELECT COUNT(*) FROM amazonuk.refund_events re
           WHERE re.amazon_order_id = o.amazon_order_id) AS refund_count
FROM orders o
LEFT JOIN amazonuk.sap_postings sp ON sp.amazon_order_id = o.amazon_order_id
""" + ORDERS_WHERE + """
ORDER BY o.purchase_date DESC
LIMIT %(limit)s OFFSET %(offset)s
"""

ORDERS_COUNT_SQL = ORDERS_CTE + """
SELECT COUNT(*)
FROM orders o
LEFT JOIN amazonuk.sap_postings sp ON sp.amazon_order_id = o.amazon_order_id
""" + ORDERS_WHERE

# Exported-CSV also uses ORDERS_SQL, just with a very large limit (see
# handle_api's /api/orders — no separate export endpoint needed server-side,
# the frontend just requests a big page_size and converts to CSV client-side).

RAW_JSON_SQL = """
SELECT order_item_id, raw_order, raw_order_item, raw_report_row
FROM amazonuk.order_items
WHERE amazon_order_id = %(order_id)s
ORDER BY order_item_id
"""

TRACE_ORDER_SQL = """
SELECT amazon_order_id, order_item_id, purchase_date, last_update_date, order_status,
       fulfillment_channel, sales_channel, ship_service_level,
       ship_city, ship_postal_code, ship_country,
       order_total_amount, order_total_currency, is_business_order, is_prime,
       seller_sku, title, asin, quantity_ordered, condition_id,
       shipment_date, carrier, tracking_number,
       raw_order, raw_order_item, raw_report_row
FROM amazonuk.order_items
WHERE amazon_order_id = %(order_id)s
ORDER BY order_item_id
"""

SAP_RESPONSE_SQL = """
SELECT status, error, sap_doc_entry, sap_doc_num, sap_response, posted_at
FROM amazonuk.sap_postings
WHERE amazon_order_id = %(order_id)s
"""

# Refund events tied to this order, for the order-trace panel — lets the
# dashboard answer "was this order refunded?" without a separate trip to the
# Refunds tab. Keyed on amazon_order_id (one order can have more than one
# RELEASED refund transaction over time).
ORDER_REFUNDS_SQL = """
SELECT re.transaction_id, re.posted_date, re.transaction_type, re.total_amount, re.currency,
       cn.status AS sap_status, cn.sap_doc_num
FROM amazonuk.refund_events re
LEFT JOIN amazonuk.sap_credit_notes cn ON cn.transaction_id = re.transaction_id
WHERE re.amazon_order_id = %(order_id)s
ORDER BY re.posted_date DESC
"""

SYNC_LOG_SQL = """
SELECT id, created_at, level, message
FROM amazonuk.sync_log
WHERE (%(level)s IS NULL OR level = %(level)s)
ORDER BY id DESC
LIMIT %(limit)s OFFSET %(offset)s
"""

SYNC_LOG_COUNT_SQL = """
SELECT COUNT(*) FROM amazonuk.sync_log
WHERE (%(level)s IS NULL OR level = %(level)s)
"""

SKU_MAP_SQL = """
SELECT seller_sku, sap_item_code, item_description, is_active
FROM amazonuk.sku_item_map
WHERE (%(q)s IS NULL OR seller_sku ILIKE '%%' || %(q)s || '%%'
                     OR sap_item_code ILIKE '%%' || %(q)s || '%%'
                     OR item_description ILIKE '%%' || %(q)s || '%%')
ORDER BY seller_sku
"""

RAW_ORDER_PATH_RE = re.compile(r"^/api/orders/([^/]+)/raw$")
SAP_PREVIEW_PATH_RE = re.compile(r"^/api/orders/([^/]+)/sap-preview$")
SAP_RESPONSE_PATH_RE = re.compile(r"^/api/orders/([^/]+)/sap-response$")
ORDER_TRACE_PATH_RE = re.compile(r"^/api/orders/([^/]+)/trace$")
REFUND_SAP_TRACE_PATH_RE = re.compile(r"^/api/refunds/([^/]+)/sap-trace$")

# Refunds (Finances API 2024-06-19 /transactions): one row per transaction
# LINE ITEM, joined back to the events table for posted_date/
# transaction_type/raw JSON and to orders for purchase date. The only date
# filter the Finances API offers is PostedDate (when Amazon recorded it in
# the seller's ledger — typically ~8-10 days AFTER purchase), so the UI
# labels it as such.
REFUNDS_SQL = """
SELECT ri.transaction_id, ri.item_index, ri.amazon_order_id, re.posted_date,
       re.transaction_type, re.description,
       ri.seller_sku, ri.quantity_refunded,
       ri.charges_total, ri.fees_total, ri.tax_withheld_total, ri.promotions_total, ri.other_total,
       ri.refunded_amount, ri.net_refund, ri.currency,
       ri.breakdowns,
       re.raw_transaction,
       o.purchase_date,
       cn.status AS sap_status, cn.sap_doc_num, cn.error AS sap_error
FROM amazonuk.refund_items ri
JOIN amazonuk.refund_events re ON re.transaction_id = ri.transaction_id
LEFT JOIN amazonuk.sap_credit_notes cn ON cn.transaction_id = re.transaction_id
LEFT JOIN LATERAL (
    SELECT DISTINCT ON (x.amazon_order_id) x.purchase_date
    FROM amazonuk.order_items x
    WHERE x.amazon_order_id = ri.amazon_order_id AND x.seller_sku = ri.seller_sku
    ORDER BY x.amazon_order_id, x.purchase_date
) o ON true
WHERE (%(q)s IS NULL OR ri.amazon_order_id ILIKE '%%' || %(q)s || '%%')
  AND (%(sku)s IS NULL OR ri.seller_sku ILIKE '%%' || %(sku)s || '%%')
  AND (%(posted_from)s IS NULL OR re.posted_date::date >= %(posted_from)s)
  AND (%(posted_to)s IS NULL OR re.posted_date::date <= %(posted_to)s)
  AND (%(order_from)s IS NULL OR o.purchase_date::date >= %(order_from)s)
  AND (%(order_to)s IS NULL OR o.purchase_date::date <= %(order_to)s)
ORDER BY re.posted_date DESC, ri.amazon_order_id
LIMIT %(limit)s OFFSET %(offset)s
"""

REFUNDS_COUNT_SQL = """
SELECT COUNT(*) AS count
FROM amazonuk.refund_items ri
JOIN amazonuk.refund_events re ON re.transaction_id = ri.transaction_id
LEFT JOIN LATERAL (
    SELECT DISTINCT ON (x.amazon_order_id) x.purchase_date
    FROM amazonuk.order_items x
    WHERE x.amazon_order_id = ri.amazon_order_id AND x.seller_sku = ri.seller_sku
    ORDER BY x.amazon_order_id, x.purchase_date
) o ON true
WHERE (%(q)s IS NULL OR ri.amazon_order_id ILIKE '%%' || %(q)s || '%%')
  AND (%(sku)s IS NULL OR ri.seller_sku ILIKE '%%' || %(sku)s || '%%')
  AND (%(posted_from)s IS NULL OR re.posted_date::date >= %(posted_from)s)
  AND (%(posted_to)s IS NULL OR re.posted_date::date <= %(posted_to)s)
  AND (%(order_from)s IS NULL OR o.purchase_date::date >= %(order_from)s)
  AND (%(order_to)s IS NULL OR o.purchase_date::date <= %(order_to)s)
"""

REFUNDS_STATS_SQL = """
SELECT
    (SELECT COUNT(*) FROM amazonuk.refund_events WHERE transaction_type = 'Refund') AS refund_events,
    (SELECT COUNT(*) FROM amazonuk.refund_items)                     AS refund_items,
    (SELECT COALESCE(SUM(refunded_amount), 0) FROM amazonuk.refund_items)  AS total_refunded,
    (SELECT COALESCE(SUM(fees_total), 0) FROM amazonuk.refund_items)       AS total_fees,
    (SELECT MAX(fetched_at) FROM amazonuk.refund_events)             AS last_fetched
"""

SAP_CREDIT_NOTE_RESPONSE_SQL = """
SELECT status, error, sap_doc_entry, sap_doc_num, sap_response, posted_at
FROM amazonuk.sap_credit_notes
WHERE transaction_id = %(transaction_id)s
"""

# Backs the dashboard's notification bell — every currently-failed posting
# (invoices + credit notes), newest first. Not a live push: the frontend
# polls this on page load and its normal 5-minute auto-refresh, same as
# every other dashboard panel.
SAP_FAILURES_SQL = """
(SELECT 'invoice' AS kind, amazon_order_id AS ref_id, error, posted_at
 FROM amazonuk.sap_postings WHERE status = 'failed')
UNION ALL
(SELECT 'credit_note' AS kind, transaction_id AS ref_id, error, posted_at
 FROM amazonuk.sap_credit_notes WHERE status = 'failed')
ORDER BY posted_at DESC
LIMIT %(limit)s
"""

SAP_FAILURES_COUNT_SQL = """
SELECT
    (SELECT COUNT(*) FROM amazonuk.sap_postings WHERE status = 'failed') +
    (SELECT COUNT(*) FROM amazonuk.sap_credit_notes WHERE status = 'failed') AS total
"""


def query(sql: str, params: dict | None = None) -> list[dict]:
    conn = store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _orders_filter_params(qs: dict) -> dict:
    """Every ORDERS_WHERE placeholder, defaulted to None (= 'no filter') if
    not present in the query string. int-typed ones (qty) are parsed;
    everything else stays a plain string or None."""
    def g(key):
        return qs.get(key, [None])[0] or None

    qty = g("qty")
    return {
        "status": g("status"),
        "q": g("q"),
        "sap_status": g("sap_status"),
        "order_from": g("order_from"),
        "order_to": g("order_to"),
        "ship_from": g("ship_from"),
        "ship_to": g("ship_to"),
        "updated_from": g("updated_from"),
        "updated_to": g("updated_to"),
        "sku": g("sku"),
        "item": g("item"),
        "qty": int(qty) if qty is not None else None,
        "line_count": g("line_count"),
        "shipping": g("shipping"),
        "discount": g("discount"),
        "tax_method": g("tax_method"),
        "has_refund": g("has_refund"),
    }


def handle_order_trace(order_id: str) -> tuple[int, str]:
    """Everything the inline trace panel needs, in one call: order/line
    detail with an accurate charges & tax breakdown per line (via
    invoice_payload._extract_breakdowns on each line's raw_order_item —
    NOT the flattened item_tax_amount/promotion_discount_amount columns,
    which only hold combined item+shipping totals, not the split), SAP
    posting status + stored response, the SAP request payload preview,
    and the raw Amazon JSON."""
    rows = query(TRACE_ORDER_SQL, {"order_id": order_id})
    if not rows:
        return 404, json.dumps({"error": f"no such order: {order_id}"})
    first = rows[0]

    lines = []
    for r in rows:
        b = _extract_breakdowns(r["raw_order_item"] or {})
        lines.append({
            "order_item_id": r["order_item_id"],
            "seller_sku": r["seller_sku"],
            "title": r["title"],
            "asin": r["asin"],
            "quantity_ordered": r["quantity_ordered"],
            "condition_id": r["condition_id"],
            "item_price": b["item_price"], "item_tax": b["item_tax"], "item_discount": b["item_discount"],
            "shipping_price": b["shipping_price"], "shipping_tax": b["shipping_tax"],
            "shipping_discount": b["shipping_discount"],
            "gift_wrap_price": b["gift_wrap_price"], "gift_wrap_tax": b["gift_wrap_tax"],
            "shipment_date": r["shipment_date"], "carrier": r["carrier"], "tracking_number": r["tracking_number"],
            "raw_report_row": r["raw_report_row"],
        })

    sap_rows = query(SAP_RESPONSE_SQL, {"order_id": order_id})
    sap = sap_rows[0] if sap_rows else None

    try:
        sap_request = post_to_sap.preview_invoice_payload(order_id)
    except Exception as e:
        sap_request = {"payload": None, "skipped": [], "error": str(e)}

    payload = {
        "amazon_order_id": first["amazon_order_id"],
        "purchase_date": first["purchase_date"],
        "last_update_date": first["last_update_date"],
        "order_status": first["order_status"],
        "fulfillment_channel": first["fulfillment_channel"],
        "sales_channel": first["sales_channel"],
        "ship_service_level": first["ship_service_level"],
        "ship_city": first["ship_city"],
        "ship_postal_code": first["ship_postal_code"],
        "ship_country": first["ship_country"],
        "order_total_amount": first["order_total_amount"],
        "order_total_currency": first["order_total_currency"],
        "is_business_order": first["is_business_order"],
        "is_prime": first["is_prime"],
        "lines": lines,
        "sap": sap,
        "sap_request": sap_request,
        "refunds": query(ORDER_REFUNDS_SQL, {"order_id": order_id}),
        "raw_order": first["raw_order"],
    }
    return 200, json.dumps(payload, default=str)


def handle_api(path: str, qs: dict) -> tuple[int, str]:
    trace_match = ORDER_TRACE_PATH_RE.match(path)
    if trace_match:
        return handle_order_trace(trace_match.group(1))

    sap_response_match = SAP_RESPONSE_PATH_RE.match(path)
    if sap_response_match:
        order_id = sap_response_match.group(1)
        rows = query(SAP_RESPONSE_SQL, {"order_id": order_id})
        if not rows:
            return 404, json.dumps({"error": f"no SAP posting record for order: {order_id}"})
        return 200, json.dumps(rows[0], default=str)

    preview_match = SAP_PREVIEW_PATH_RE.match(path)
    if preview_match:
        order_id = preview_match.group(1)
        try:
            result = post_to_sap.preview_invoice_payload(order_id)
        except Exception as e:
            return 500, json.dumps({"error": str(e)})
        return 200, json.dumps(result, default=str)

    raw_match = RAW_ORDER_PATH_RE.match(path)
    if raw_match:
        order_id = raw_match.group(1)
        rows = query(RAW_JSON_SQL, {"order_id": order_id})
        if not rows:
            return 404, json.dumps({"error": f"no such order: {order_id}"})
        payload = {
            "amazon_order_id": order_id,
            "raw_order": rows[0]["raw_order"],
            "items": [
                {
                    "order_item_id": r["order_item_id"],
                    "raw_order_item": r["raw_order_item"],
                    "raw_report_row": r["raw_report_row"],
                }
                for r in rows
            ],
        }
        return 200, json.dumps(payload, default=str)

    if path == "/api/stats":
        payload = {
            "kpis": query(STATS_SQL)[0],
            "by_status": query(STATUS_SQL),
        }
        return 200, json.dumps(payload, default=str)

    if path == "/api/orders":
        page = max(int(qs.get("page", ["1"])[0] or 1), 1)
        page_size = min(int(qs.get("page_size", ["50"])[0] or 50), 5000)
        params = _orders_filter_params(qs)
        total = query(ORDERS_COUNT_SQL, params)[0]["count"]
        rows = query(ORDERS_SQL, {
            **params,
            "limit": page_size,
            "offset": (page - 1) * page_size,
        })
        return 200, json.dumps({
            "orders": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max((total + page_size - 1) // page_size, 1),
        }, default=str)

    if path == "/api/sync-log":
        page = max(int(qs.get("page", ["1"])[0] or 1), 1)
        page_size = min(int(qs.get("page_size", ["50"])[0] or 50), 500)
        params = {"level": qs.get("level", [None])[0] or None}
        total = query(SYNC_LOG_COUNT_SQL, params)[0]["count"]
        rows = query(SYNC_LOG_SQL, {**params, "limit": page_size, "offset": (page - 1) * page_size})
        return 200, json.dumps({
            "entries": rows, "total": total, "page": page, "page_size": page_size,
            "pages": max((total + page_size - 1) // page_size, 1),
        }, default=str)

    if path == "/api/sku-map":
        rows = query(SKU_MAP_SQL, {"q": qs.get("q", [None])[0] or None})
        return 200, json.dumps({"items": rows, "total": len(rows)}, default=str)

    if path == "/api/refunds":
        page = max(int(qs.get("page", ["1"])[0] or 1), 1)
        page_size = min(int(qs.get("page_size", ["50"])[0] or 50), 5000)
        params = {
            "q": qs.get("q", [None])[0] or None,
            "sku": qs.get("sku", [None])[0] or None,
            "posted_from": qs.get("posted_from", [None])[0] or None,
            "posted_to": qs.get("posted_to", [None])[0] or None,
            "order_from": qs.get("order_from", [None])[0] or None,
            "order_to": qs.get("order_to", [None])[0] or None,
        }
        total = query(REFUNDS_COUNT_SQL, params)[0]["count"]
        rows = query(REFUNDS_SQL, {
            **params,
            "limit": page_size,
            "offset": (page - 1) * page_size,
        })
        return 200, json.dumps({
            "refunds": rows,
            "stats": query(REFUNDS_STATS_SQL)[0],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max((total + page_size - 1) // page_size, 1),
        }, default=str)

    if path == "/api/sap-failures":
        limit = min(int(qs.get("limit", ["50"])[0] or 50), 500)
        total = query(SAP_FAILURES_COUNT_SQL)[0]["total"]
        rows = query(SAP_FAILURES_SQL, {"limit": limit})
        return 200, json.dumps({"total": total, "items": rows}, default=str)

    refund_sap_trace_match = REFUND_SAP_TRACE_PATH_RE.match(path)
    if refund_sap_trace_match:
        transaction_id = refund_sap_trace_match.group(1)
        sap_rows = query(SAP_CREDIT_NOTE_RESPONSE_SQL, {"transaction_id": transaction_id})
        sap = sap_rows[0] if sap_rows else None
        try:
            sap_request = post_credit_notes_to_sap.preview_credit_note_payload(transaction_id)
        except Exception as e:
            sap_request = {"payload": None, "skipped": [], "error": str(e)}
        return 200, json.dumps({"sap": sap, "sap_request": sap_request}, default=str)

    return 404, json.dumps({"error": "not found"})


def dispatch(method: str, path: str, query_string: str = "") -> tuple[int, str]:
    """Lambda-facing entrypoint: one HTTP request -> (status, body_str).
    GET only — the dashboard cannot trigger fetches or SAP postings."""
    if method != "GET":
        return 405, json.dumps({"error": "method not allowed (dashboard is read-only)"})
    return handle_api(path, parse_qs(query_string))
