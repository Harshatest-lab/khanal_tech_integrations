"""Upserts into amazonuk.order_items from the 3 SP-API sources.

PK is (amazon_order_id, order_item_id). Orders and OrderItems are written
together as one row per item (an order alone has no item id to key on).
The shipment report is written separately, keyed the same way, and can
arrive before or after the order/item row — ON CONFLICT DO UPDATE either
creates the row or fills in the shipment columns on an existing one.
"""

import datetime
import json

import psycopg

from amazon_uk import applog
from amazon_uk import credentials

log = applog.get_logger("amz_sync")

ORDER_ITEM_COLUMNS = [
    "amazon_order_id", "order_item_id",
    "order_status", "purchase_date", "last_update_date",
    "order_total_amount", "order_total_currency",
    "number_of_items_shipped", "number_of_items_unshipped",
    "is_prime", "is_business_order", "payment_method", "marketplace_id",
    "fulfillment_channel", "sales_channel", "ship_service_level",
    "ship_city", "ship_postal_code", "ship_country",
    "asin", "seller_sku", "title", "quantity_ordered",
    "item_price_amount", "item_tax_amount",
    "shipping_price_amount", "shipping_tax_amount",
    "promotion_discount_amount", "condition_id", "is_gift",
    "raw_order", "raw_order_item",
]

SHIPMENT_COLUMNS = [
    "amazon_order_id", "order_item_id",
    "shipment_date", "carrier", "tracking_number",
    "ship_city", "ship_postal_code", "ship_country",
    "raw_report_row",
]


# A failed order / credit note is retried by every scheduled run; after this
# many failed attempts it is marked dead and automated runs skip it (a manual
# "Post to SAP" from the dashboard still tries it). Without a limit a
# permanent failure (unmapped SKU, locked period, no stock) would fail and
# be re-emailed forever.
MAX_POST_ATTEMPTS = 5


def _upsert_sql(columns: list[str]) -> str:
    cols = ", ".join(columns)
    placeholders = ", ".join(f"%({c})s" for c in columns)
    update_cols = [c for c in columns if c not in ("amazon_order_id", "order_item_id")]
    updates = ",\n    ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    return f"""
        INSERT INTO amazonuk.order_items ({cols})
        VALUES ({placeholders})
        ON CONFLICT (amazon_order_id, order_item_id) DO UPDATE SET
            {updates},
            fetched_at = now()
    """


UPSERT_ORDER_ITEM_SQL = _upsert_sql(ORDER_ITEM_COLUMNS)
UPSERT_SHIPMENT_SQL = _upsert_sql(SHIPMENT_COLUMNS)


def connect():
    # ClientCursor = client-side parameter binding, same semantics psycopg2
    # had. Every query here (notably the dashboard's "%(x)s IS NULL OR ..."
    # filters) was written against that; server-side binding can't infer the
    # type of a bare "$1 IS NULL" parameter.
    db = credentials.get_db_credentials()
    return psycopg.connect(
        host=db.host,
        port=db.port,
        dbname=db.dbname,
        user=db.user,
        password=db.password,
        connect_timeout=15,
        cursor_factory=psycopg.ClientCursor,
    )


# Amazon's new fulfillmentStatus is UPPERCASE and spells Cancelled with two
# Ls ("CANCELLED") — normalized to the old convention ("Shipped"/"Pending"/
# "Canceled") so every existing SQL query, dashboard pill, and status filter
# keeps working unchanged.
_ORDER_STATUS_MAP = {"SHIPPED": "Shipped", "PENDING": "Pending", "CANCELLED": "Canceled"}


def flatten_order_item(order: dict, item: dict) -> dict:
    """order/item both come from the single bulk orders endpoint
    (/orders/2026-01-01/orders) — order already has 'orderItems' embedded,
    this just flattens one (order, item) pair into a row.

    Field-shape note: this schema has no IsPrime equivalent anywhere
    (confirmed: zero matches for "prime" in a full raw response dump) — a
    real, permanent gap versus the old v0 Orders API. is_business_order
    survives via the order's "programs" array containing "AMAZON_BUSINESS".
    """
    fulfillment = order.get("fulfillment") or {}
    recipient_address = (order.get("recipient") or {}).get("deliveryAddress") or {}
    sales_channel = order.get("salesChannel") or {}
    proceeds_total = (order.get("proceeds") or {}).get("grandTotal") or {}
    payment_executions = (order.get("payment") or {}).get("paymentExecutions") or []

    items_fulfillment = [oi.get("fulfillment") or {} for oi in order.get("orderItems", [])]
    items_shipped = sum(f.get("quantityFulfilled") or 0 for f in items_fulfillment)
    items_unshipped = sum(f.get("quantityUnfulfilled") or 0 for f in items_fulfillment)

    product = item.get("product") or {}
    unit_price = product.get("price", {}).get("unitPrice") or {}
    breakdowns = {
        b["type"]: b.get("subtotal", {}).get("amount")
        for b in (item.get("proceeds") or {}).get("breakdowns", [])
    }

    return {
        "amazon_order_id": order["orderId"],
        "order_item_id": item["orderItemId"],
        "order_status": _ORDER_STATUS_MAP.get(fulfillment.get("fulfillmentStatus"), fulfillment.get("fulfillmentStatus")),
        "purchase_date": order.get("createdTime"),
        "last_update_date": order.get("lastUpdatedTime"),
        "order_total_amount": proceeds_total.get("amount"),
        "order_total_currency": proceeds_total.get("currencyCode"),
        "number_of_items_shipped": items_shipped,
        "number_of_items_unshipped": items_unshipped,
        "is_prime": None,  # no equivalent field in this API version
        "is_business_order": "AMAZON_BUSINESS" in (order.get("programs") or []),
        "payment_method": payment_executions[0].get("paymentMethod") if payment_executions else None,
        "marketplace_id": sales_channel.get("marketplaceId"),
        "fulfillment_channel": "AFN" if fulfillment.get("fulfilledBy") == "AMAZON" else "MFN",
        "sales_channel": sales_channel.get("marketplaceName"),
        "ship_service_level": (fulfillment.get("fulfillmentServiceLevel") or "").capitalize() or None,
        "ship_city": recipient_address.get("city"),
        "ship_postal_code": recipient_address.get("postalCode"),
        "ship_country": recipient_address.get("countryCode"),
        "asin": product.get("asin"),
        "seller_sku": product.get("sellerSku"),
        "title": product.get("title"),
        "quantity_ordered": item.get("quantityOrdered"),
        # ITEM/TAX/DISCOUNT breakdowns only exist once Amazon has processed the
        # order (present by the time it ships); before that, fall back to the
        # listed unit price so item_price_amount is never empty for a live order.
        "item_price_amount": breakdowns.get("ITEM") or unit_price.get("amount"),
        "item_tax_amount": breakdowns.get("TAX"),
        "shipping_price_amount": breakdowns.get("SHIPPING"),
        "shipping_tax_amount": breakdowns.get("SHIPPING_TAX"),
        "promotion_discount_amount": breakdowns.get("DISCOUNT"),
        "condition_id": None,
        "is_gift": None,
        "raw_order": json.dumps(order),
        "raw_order_item": json.dumps(item),
    }


def flatten_shipment_row(row: dict) -> dict:
    """Pull out only the ship date + carrier/tracking (what triggers the SAP
    invoice); everything else the report carries stays in raw_report_row.
    """
    return {
        "amazon_order_id": row["amazon-order-id"],
        "order_item_id": row["amazon-order-item-id"],
        "shipment_date": row.get("shipment-date") or None,
        "carrier": row.get("carrier") or None,
        "tracking_number": row.get("tracking-number") or None,
        "ship_city": row.get("ship-city") or None,
        "ship_postal_code": row.get("ship-postal-code") or None,
        "ship_country": row.get("ship-country") or None,
        "raw_report_row": json.dumps(row),
    }


def _upsert(sql: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
        return len(rows)
    finally:
        conn.close()


def upsert_orders(orders: list[dict]) -> int:
    """Upsert every item of every order in ONE connection / ONE transaction.
    (Per-order upserts opened a new RDS connection each — ~1 s apiece over
    the network, ~4 min for 213 orders, a big slice of the Lambda's 900 s.)
    All-or-nothing: a failure leaves no half-stored batch."""
    rows = [flatten_order_item(order, item)
            for order in orders for item in order.get("orderItems", [])]
    return _upsert(UPSERT_ORDER_ITEM_SQL, rows)


def upsert_order_items(order: dict) -> int:
    """order already has 'orderItems' embedded (single bulk endpoint) —
    no separate per-order item fetch needed."""
    rows = [flatten_order_item(order, item) for item in order.get("orderItems", [])]
    return _upsert(UPSERT_ORDER_ITEM_SQL, rows)


def upsert_shipment_rows(report_rows: list[dict]) -> int:
    """Attach shipment data to orders ALREADY in the table only.

    The shipment report covers a trailing window by date, so it routinely
    includes orders whose order/item records were never fetched (e.g. a
    September shipment when only August orders have been backfilled).
    Upserting those used to CREATE order-level rows with every order field
    NULL (no purchase_date, no status, no raw_order) — garbage rows that
    then showed up in the dashboard and KPIs. 2026-09-16: found 450 such
    rows, deleted them, and fixed the root cause here: skip any report row
    whose amazon_order_id has no real order row (raw_order IS NOT NULL).
    Shipment columns for known orders still upsert as before.
    """
    rows = [flatten_shipment_row(row) for row in report_rows]
    if not rows:
        return 0
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT amazon_order_id FROM amazonuk.order_items "
                "WHERE raw_order IS NOT NULL"
            )
            known_orders = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()
    matched = [row for row in rows if row["amazon_order_id"] in known_orders]
    skipped = len(rows) - len(matched)
    if skipped:
        log.info(f"shipment report: skipped {skipped} rows for orders not fetched "
                 f"(no order row in DB — shipment-only rows would be garbage)")
    return _upsert(UPSERT_SHIPMENT_SQL, matched)


def get_last_watermark(source: str) -> datetime.datetime | None:
    """Most recent watermark for source (e.g. 'orders') — the cursor the
    next incremental fetch resumes from. None if no run has completed yet."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(watermark) FROM amazonuk.fetch_log WHERE source = %s", (source,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def record_fetch_log(source: str, watermark, orders_fetched: int, items_stored: int) -> None:
    conn = connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO amazonuk.fetch_log (source, watermark, orders_fetched, items_stored)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (source, watermark, orders_fetched, items_stored),
                )
    finally:
        conn.close()


# ============================ refunds (Finances API 2024-06-19) ============================

# Which column each item.breakdowns[].breakdownType rolls into. Shipping is
# a buyer-facing charge like ProductCharges (both count toward
# refunded_amount); DigitalServicesFee is Amazon-fee-like.
KNOWN_BREAKDOWN_TYPES = {
    "ProductCharges": "charges_total",
    "Shipping": "charges_total",
    "AmazonFees": "fees_total",
    "DigitalServicesFee": "fees_total",
    "Tax": "tax_withheld_total",
    "PromoRebates": "promotions_total",
}

# Expected on every FBAInventoryReimbursement item — its breakdownType is
# always the transaction type's own name. Not fetched right now (see
# fetch_refunds.WANTED_TRANSACTION_TYPES) but kept here so a stray one
# doesn't trip the unknown-type warning below.
EXPECTED_REIMBURSEMENT_BREAKDOWN = "FBAInventoryReimbursement"


def _breakdown_totals(breakdowns: list[dict]) -> dict[str, float]:
    """Top-level item.breakdowns entries -> {breakdownType: amount}. Amazon
    already aggregates each branch's amount, so no need to recurse further."""
    totals: dict[str, float] = {}
    for b in breakdowns or []:
        bt = b.get("breakdownType")
        amt = (b.get("breakdownAmount") or {}).get("currencyAmount") or 0
        totals[bt] = totals.get(bt, 0) + amt
    return totals


def flatten_refund_transaction(tx: dict) -> tuple[dict, list[dict]]:
    """One /finances/2024-06-19/transactions entry -> (event_row, [item_rows]).

    event_row keys amazonuk.refund_events; item_rows key amazonuk.refund_items
    (one per distinct OrderAdjustmentItemId — see grouping below; falls back
    to position-keyed for items with no adjustment id).
    """
    related = {r["relatedIdentifierName"]: r["relatedIdentifierValue"]
               for r in tx.get("relatedIdentifiers") or []}
    order_id = related.get("ORDER_ID")
    total_amount = (tx.get("totalAmount") or {}).get("currencyAmount")
    currency = (tx.get("totalAmount") or {}).get("currencyCode")

    # Amazon can split ONE logical refund line across two tx['items'] array
    # entries sharing the same ORDER_ADJUSTMENT_ITEM_ID (different
    # sub-amounts, e.g. -7.58 and -18.04 for the same item). Storing each
    # array position as its own row violates refund_items' own "one row per
    # refunded line item" grain and double-counts the line downstream (e.g.
    # two Credit Memo lines crediting the same invoice BaseLine). Group by
    # adjustment id first; items with none (reimbursements) each get their
    # own singleton group via a position-based fallback key.
    groups: dict[str, list[dict]] = {}
    for idx, item in enumerate(tx.get("items") or []):
        item_related = {r["itemRelatedIdentifierName"]: r["itemRelatedIdentifierValue"]
                         for r in item.get("relatedIdentifiers") or []}
        key = item_related.get("ORDER_ADJUSTMENT_ITEM_ID") or f"__no_adjustment_id_{idx}"
        groups.setdefault(key, []).append(item)

    items = []
    for out_idx, (adjustment_item_id, group) in enumerate(groups.items()):
        first = group[0]
        item_related = {r["itemRelatedIdentifierName"]: r["itemRelatedIdentifierValue"]
                         for r in first.get("relatedIdentifiers") or []}
        context = next((c for c in first.get("contexts") or []
                         if c.get("contextType") == "ProductContext"), {})

        if len(group) > 1:
            quantities = {g.get("quantityShipped") for g in
                          (next((c for c in it.get("contexts") or [] if c.get("contextType") == "ProductContext"), {})
                           for it in group)}
            log.info(
                f"refund txn {tx.get('transactionId')}: order_adjustment_item {adjustment_item_id} "
                f"split across {len(group)} entries (amounts {[i.get('totalAmount') for i in group]}) — "
                f"merged into one row" + (f", quantities disagreed {quantities}" if len(quantities) > 1 else ""))

        merged_breakdowns = [b for it in group for b in (it.get("breakdowns") or [])]
        item_total = sum((it.get("totalAmount") or {}).get("currencyAmount") or 0 for it in group)

        totals = _breakdown_totals(merged_breakdowns)
        unknown_types = set(totals) - set(KNOWN_BREAKDOWN_TYPES) - {EXPECTED_REIMBURSEMENT_BREAKDOWN}
        if unknown_types:
            log.warning(
                f"refund txn {tx.get('transactionId')} ({tx.get('transactionType')}): "
                f"unrecognized breakdown type(s) {unknown_types} rolled into other_total "
                f"(update KNOWN_BREAKDOWN_TYPES if one of these deserves its own column)")

        column_totals = {"charges_total": 0.0, "fees_total": 0.0,
                          "tax_withheld_total": 0.0, "promotions_total": 0.0, "other_total": 0.0}
        for bt, amt in totals.items():
            column_totals[KNOWN_BREAKDOWN_TYPES.get(bt, "other_total")] += amt

        items.append({
            "transaction_id": tx["transactionId"],
            "item_index": out_idx,
            "order_adjustment_item_id": item_related.get("ORDER_ADJUSTMENT_ITEM_ID"),
            "amazon_order_id": order_id,
            "seller_sku": context.get("sku"),
            "asin": context.get("asin"),
            "quantity_refunded": context.get("quantityShipped"),
            "product_description": first.get("description"),
            "breakdowns": json.dumps(merged_breakdowns) if merged_breakdowns else None,
            "charges_total": round(column_totals["charges_total"], 2),
            "fees_total": round(column_totals["fees_total"], 2),
            "tax_withheld_total": round(column_totals["tax_withheld_total"], 2),
            "promotions_total": round(column_totals["promotions_total"], 2),
            "other_total": round(column_totals["other_total"], 2),
            "refunded_amount": round(-column_totals["charges_total"], 2),
            "net_refund": round(item_total, 2) if group else None,
            "currency": (first.get("totalAmount") or {}).get("currencyCode") or currency,
        })

    event_row = {
        "transaction_id": tx["transactionId"],
        "transaction_type": tx["transactionType"],
        "transaction_status": tx.get("transactionStatus"),
        "amazon_order_id": order_id,
        "refund_id": related.get("REFUND_ID"),
        "settlement_id": related.get("SETTLEMENT_ID"),
        "financial_event_group_id": related.get("FINANCIAL_EVENT_GROUP_ID"),
        "description": tx.get("description"),
        "posted_date": tx["postedDate"],
        "marketplace_name": (tx.get("marketplaceDetails") or {}).get("marketplaceName"),
        "total_amount": round(total_amount, 2) if total_amount is not None else None,
        "currency": currency,
        "raw_transaction": json.dumps(tx),
    }
    return event_row, items


UPSERT_REFUND_EVENT_SQL = """
    INSERT INTO amazonuk.refund_events
        (transaction_id, transaction_type, transaction_status, amazon_order_id,
         refund_id, settlement_id, financial_event_group_id, description,
         posted_date, marketplace_name, total_amount, currency, raw_transaction)
    VALUES (%(transaction_id)s, %(transaction_type)s, %(transaction_status)s, %(amazon_order_id)s,
            %(refund_id)s, %(settlement_id)s, %(financial_event_group_id)s, %(description)s,
            %(posted_date)s, %(marketplace_name)s, %(total_amount)s, %(currency)s, %(raw_transaction)s)
    ON CONFLICT (transaction_id) DO UPDATE SET
        transaction_status = EXCLUDED.transaction_status,
        total_amount = EXCLUDED.total_amount,
        raw_transaction = EXCLUDED.raw_transaction,
        fetched_at = now()
"""

UPSERT_REFUND_ITEM_SQL = """
    INSERT INTO amazonuk.refund_items
        (transaction_id, item_index, order_adjustment_item_id, amazon_order_id,
         seller_sku, asin, quantity_refunded, product_description, breakdowns,
         charges_total, fees_total, tax_withheld_total, promotions_total, other_total,
         refunded_amount, net_refund, currency)
    VALUES (%(transaction_id)s, %(item_index)s, %(order_adjustment_item_id)s, %(amazon_order_id)s,
            %(seller_sku)s, %(asin)s, %(quantity_refunded)s, %(product_description)s, %(breakdowns)s,
            %(charges_total)s, %(fees_total)s, %(tax_withheld_total)s, %(promotions_total)s, %(other_total)s,
            %(refunded_amount)s, %(net_refund)s, %(currency)s)
    ON CONFLICT (transaction_id, item_index) DO UPDATE SET
        seller_sku = EXCLUDED.seller_sku, asin = EXCLUDED.asin,
        quantity_refunded = EXCLUDED.quantity_refunded,
        product_description = EXCLUDED.product_description, breakdowns = EXCLUDED.breakdowns,
        charges_total = EXCLUDED.charges_total, fees_total = EXCLUDED.fees_total,
        tax_withheld_total = EXCLUDED.tax_withheld_total,
        promotions_total = EXCLUDED.promotions_total, other_total = EXCLUDED.other_total,
        refunded_amount = EXCLUDED.refunded_amount, net_refund = EXCLUDED.net_refund,
        currency = EXCLUDED.currency,
        fetched_at = now()
"""


def upsert_refunds(transactions: list[dict]) -> int:
    """Store a page/batch of Refund transactions idempotently, keyed by
    Amazon's own transactionId, in ONE connection/transaction. Returns the
    number of transactions stored."""
    if not transactions:
        return 0
    conn = connect()
    try:
        with conn:
            with conn.cursor() as cur:
                for tx in transactions:
                    event_row, item_rows = flatten_refund_transaction(tx)
                    cur.execute(UPSERT_REFUND_EVENT_SQL, event_row)
                    if item_rows:
                        cur.executemany(UPSERT_REFUND_ITEM_SQL, item_rows)
        return len(transactions)
    finally:
        conn.close()
