"""Parse the all-orders flat-file report (bulk fetch path). Pure.

GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL stages a whole month
in ~2 minutes vs hours of per-order Orders API calls. The report carries
one row per order line with order-level fields repeated on every row.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from amazon_us.dates import parse_utc, utc_to_pacific


def rows_by_order(content: str) -> dict[str, list[dict[str, str]]]:
    """Every raw row of a tab-separated Amazon report, grouped by amazon-order-id, with all its columns.
    The parsers above keep only the fields the pipeline uses; this is what gets stored as the raw record."""
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in csv.DictReader(io.StringIO(content), delimiter="\t"):
        order_id = (row.get("amazon-order-id") or "").strip()
        if order_id:
            grouped.setdefault(order_id, []).append(dict(row))
    return grouped


def parse_orders_report(content: str) -> dict[str, dict[str, Any]]:
    """{amazon_order_id: {header fields..., 'lines': [line dicts]}}."""
    orders: dict[str, dict[str, Any]] = {}
    reader = csv.DictReader(io.StringIO(content), delimiter="\t")
    for row in reader:
        order_id = (row.get("amazon-order-id") or "").strip()
        if not order_id:
            continue

        if order_id not in orders:
            orders[order_id] = {
                "amazon_order_id": order_id,
                "order_status": (row.get("order-status") or "").strip(),
                "purchase_date": utc_to_pacific((row.get("purchase-date") or "").strip()),
                "purchase_date_utc": parse_utc((row.get("purchase-date") or "").strip()),
                "fulfillment_channel": (row.get("fulfillment-channel") or "").strip(),
                "last_updated_date": (row.get("last-updated-date") or "").strip(),
                "ship_city": (row.get("ship-city") or "")[:140].strip(),
                "ship_state": (row.get("ship-state") or "")[:140].strip(),
                "ship_postal_code": (row.get("ship-postal-code") or "")[:20].strip(),
                "ship_country": (row.get("ship-country") or "")[:10].strip(),
                "buyer_name": "",  # SP-API withholds buyer PII
                "lines": [],
            }

        sku = (row.get("sku") or "").strip()
        if not sku:
            continue

        def _f(key: str) -> float:
            try:
                return float(row.get(key) or 0)
            except ValueError:
                return 0.0

        qty = _f("quantity")
        if qty <= 0:
            continue

        item_price = _f("item-price")
        orders[order_id]["lines"].append(
            {
                "order_item_id": (row.get("order-item-id") or "").strip(),
                "seller_sku": sku,
                "asin": (row.get("asin") or "").strip(),
                "title": (row.get("product-name") or "")[:140],
                "quantity": qty,
                # GROSS unit price — promotions deliberately ignored
                "unit_price": round(item_price / qty, 5) if qty else 0.0,
                "item_price_amount": item_price,
                "item_tax_amount": _f("item-tax"),
                "shipping_price_amount": _f("shipping-price"),
                "shipping_tax_amount": _f("shipping-tax"),
            }
        )

    return orders


def order_total(header: dict[str, Any]) -> float:
    return round(
        sum(
            line["item_price_amount"]
            + line["item_tax_amount"]
            + line["shipping_price_amount"]
            + line["shipping_tax_amount"]
            for line in header["lines"]
        ),
        2,
    )
