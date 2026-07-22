"""Fetch + stage Amazon orders into Postgres.

Three modes:
  - incremental: Orders API LastUpdatedAfter watermark (hourly cron).
    Catches new orders and status transitions (Pending -> Shipped).
  - report: bulk flat-file report for a date window (daily cron with
    FETCH_LOOKBACK_DAYS lookback; the MTR keys on ship date while this
    report keys on purchase date, so the lookback stages the superset).
  - backfill: report mode with an explicit window (manual invoke).

Staging rule: Shipped + every SKU mapped + has lines -> 'ready';
otherwise 'fetched'. POSTING/POSTED orders are never downgraded (the
store enforces it). Staging more than we post is safe — posting is
gated separately by ship date + cutoff.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from amazon_us import constants, report_parser, reports_api, sp_api, store
from amazon_us.dates import (
    date_chunks,
    pacific_day_to_utc,
    parse_utc,
    utc_to_pacific,
    utc_to_pacific_date,
)

logger = logging.getLogger(__name__)

_SHIPPED = "Shipped"
_WATERMARK = "orders_last_updated_after"


_MFN_CHANNEL_VALUES = {"MFN", "Merchant"}  # Orders API says "MFN"; flat-file report says "Merchant"


def _mfn_ship_date(fulfillment_channel: str, order_status: str, last_update_raw: str) -> str | None:
    """MFN ship-date proxy: Amazon exposes no true ship date for
    merchant-fulfilled orders (confirmed against SP-API docs/known gaps —
    the non-PII order reports dropped ship-date columns and never got them
    back). Use last-updated-date at the moment order_status becomes
    Shipped instead — that's Amazon's own timestamp of OUR shipment
    confirmation, the closest available proxy. AFN (Amazon-fulfilled)
    orders keep using the real ship date from the FBA shipments report
    (fba_ship_sync) — unaffected by this.

    Two different vocabularies carry the same concept: the Orders API
    returns literal "AFN"/"MFN", the flat-file orders report returns
    "Amazon"/"Merchant" for the same field. Both are checked here since
    this function is called from both fetch paths."""
    if fulfillment_channel not in _MFN_CHANNEL_VALUES or order_status != _SHIPPED:
        return None
    return utc_to_pacific_date(last_update_raw)


def refresh_sku_map(conn) -> dict[str, str]:
    """Pull OITM.U_Amz_SKU into amazon_us_sku_map, then return the merged
    mapping (overrides win). Ambiguous SKUs are logged, never guessed."""
    from amazon_us.hana_access import fetch_amazon_sku_map

    try:
        raw = fetch_amazon_sku_map()
        stored, ambiguous = store.replace_hana_sku_rows(conn, raw)
        if ambiguous:
            logger.warning("SKUs mapped to multiple SAP items (need override rows): %s", ambiguous)
        logger.info("SKU map refreshed: %s single-code SKUs", stored)
    except Exception as e:
        # Serve the last stored copy through HANA outages.
        logger.warning("SKU map refresh failed, serving stored copy: %s", e)
    return store.get_sku_mapping(conn)


def seed_watermark(conn, when: str | None = None) -> str:
    """Manually set the incremental watermark, skipping the expensive 30-day
    cold-start catch-up a missing watermark triggers (sync_incremental does
    per-order Orders API calls, sized for dozens/hour, not a month of
    backlog — a cold start against this account's throttling tier can burn
    the full 900s Lambda timeout without finishing even the first page of
    /orders/v0/orders). Call once before enabling the incremental schedule,
    and again after recovering from any stuck/aborted incremental run.
    `when` defaults to now; pass an explicit 'YYYY-MM-DD HH:MM:SS' UTC
    string to backdate it instead."""
    value = when or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    store.set_watermark(conn, _WATERMARK, value)
    return value


def sync_incremental(conn) -> dict:
    """Orders API watermark sync. Slow per-order item calls are fine here:
    hourly increments are dozens of orders, not thousands."""
    run_id = store.start_sync_log(conn, "incremental")
    try:
        raw_watermark = store.get_watermark(conn, _WATERMARK)
        if raw_watermark:
            wm_dt = datetime.fromisoformat(raw_watermark.replace(" ", "T"))
            if wm_dt.tzinfo is None:
                wm_dt = wm_dt.replace(tzinfo=timezone.utc)
        else:
            wm_dt = datetime.now(timezone.utc) - timedelta(days=30)
            logger.info("No watermark — 30-day initial catch-up")

        # Re-read a little history every run; late item/status edits happen.
        fetch_from = (wm_dt - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_watermark = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        mapping = refresh_sku_map(conn)
        raw_orders = sp_api.fetch_orders_by_update(fetch_from)
        counts = _stage_from_orders_api(conn, raw_orders, mapping)

        store.set_watermark(conn, _WATERMARK, new_watermark)
        promoted = store.promote_fetched_orders(conn, mapping)
        counts["promoted"] = promoted

        store.finish_sync_log(conn, run_id, "success", counts)
        return {"status": "success", **counts}
    except Exception as e:
        store.finish_sync_log(conn, run_id, "error", error=str(e))
        raise


def sync_via_report(conn, from_date: str, to_date: str, lookback_days: int | None = None) -> dict:
    """Bulk fetch via the flat-file report ('YYYY-MM-DD' Pacific dates).
    Applies the MTR lookback to from_date; chunks windows to <= 30 days."""
    lookback = constants.FETCH_LOOKBACK_DAYS if lookback_days is None else lookback_days
    fetch_from = (
        datetime.strptime(from_date, "%Y-%m-%d") - timedelta(days=lookback)
    ).strftime("%Y-%m-%d")

    run_id = store.start_sync_log(conn, "report", fetch_from, to_date)
    try:
        mapping = refresh_sku_map(conn)
        totals = {"orders": 0, "created": 0, "updated": 0, "refreshed": 0, "ready": 0}
        mfn_ship_dates: dict[str, str] = {}

        for chunk_from, chunk_to in date_chunks(fetch_from, to_date, max_days=30):
            report_id = reports_api.create_report(
                reports_api.ORDERS_REPORT_TYPE,
                constants.MARKETPLACE_ID,
                pacific_day_to_utc(chunk_from),
                pacific_day_to_utc(chunk_to, end_of_day=True),
            )
            logger.info("Orders report %s requested for %s..%s", report_id, chunk_from, chunk_to)
            doc_id = reports_api.poll_report(report_id)
            content = reports_api.download_report(doc_id)

            orders = report_parser.parse_orders_report(content)
            totals["orders"] += len(orders)
            for header in orders.values():
                ship_date = _mfn_ship_date(
                    header.get("fulfillment_channel", ""),
                    header.get("order_status", ""),
                    header.get("last_updated_date", ""),
                )
                if ship_date:
                    mfn_ship_dates[header["amazon_order_id"]] = ship_date

                result = _stage_one(conn, header, mapping)
                totals[result] = totals.get(result, 0) + 1
                if result != "refreshed" and header.get("_staged_ready"):
                    totals["ready"] += 1

        if mfn_ship_dates:
            totals["mfn_ship_dates_set"] = store.set_actual_ship_dates(conn, mfn_ship_dates)

        promoted = store.promote_fetched_orders(conn, mapping)
        totals["promoted"] = promoted

        store.finish_sync_log(conn, run_id, "success", totals)
        return {"status": "success", **totals}
    except Exception as e:
        store.finish_sync_log(conn, run_id, "error", error=str(e))
        raise


def _stage_one(conn, header: dict, mapping: dict[str, str]) -> str:
    lines = header.pop("lines", [])
    skus = [line["seller_sku"] for line in lines]
    unmapped = sorted({s for s in skus if s not in mapping})
    is_shipped = header.get("order_status") == _SHIPPED
    stage = "ready" if (is_shipped and lines and not unmapped) else "fetched"
    header["_staged_ready"] = stage == "ready"

    fields = {
        **{k: v for k, v in header.items() if not k.startswith("_")},
        "stage": stage,
        "order_total_amount": report_parser.order_total({"lines": lines}),
        "last_error": ("Unmapped SKUs: " + ", ".join(unmapped)) if unmapped else None,
        "raw_payload": {},
    }
    return store.upsert_order(conn, fields, lines)


def _stage_from_orders_api(conn, raw_orders: list[dict], mapping: dict[str, str]) -> dict:
    counts = {"orders": len(raw_orders), "created": 0, "updated": 0, "refreshed": 0, "ready": 0}
    mfn_ship_dates: dict[str, str] = {}
    for raw in raw_orders:
        oid = raw.get("AmazonOrderId", "")
        if not oid:
            continue

        if store.get_order_stage(conn, oid) in ("posting", "posted"):
            store.upsert_order(
                conn,
                {"amazon_order_id": oid, "order_status": raw.get("OrderStatus", ""), "stage": "posted",
                 "raw_payload": {"order": raw}},
                [],
            )
            counts["refreshed"] += 1
            continue

        items_raw = sp_api.fetch_order_items(oid)
        lines = _lines_from_items(items_raw)
        skus = [line["seller_sku"] for line in lines]
        unmapped = sorted({s for s in skus if s not in mapping})
        is_shipped = raw.get("OrderStatus", "") == _SHIPPED
        stage = "ready" if (is_shipped and lines and not unmapped) else "fetched"
        if stage == "ready":
            counts["ready"] += 1

        address = raw.get("ShippingAddress") or {}
        fields = {
            "amazon_order_id": oid,
            "order_status": raw.get("OrderStatus", ""),
            "stage": stage,
            "purchase_date": utc_to_pacific(raw.get("PurchaseDate", "")),
            "purchase_date_utc": parse_utc(raw.get("PurchaseDate", "")),
            "order_total_amount": float((raw.get("OrderTotal") or {}).get("Amount") or 0),
            "ship_city": (address.get("City") or "")[:140],
            "ship_state": (address.get("StateOrRegion") or "")[:140],
            "ship_postal_code": (address.get("PostalCode") or "")[:20],
            "ship_country": (address.get("CountryCode") or "")[:10],
            "buyer_name": ((raw.get("BuyerInfo") or {}).get("BuyerName") or "")[:120],
            "fulfillment_channel": raw.get("FulfillmentChannel", ""),
            "last_error": ("Unmapped SKUs: " + ", ".join(unmapped)) if unmapped else None,
            "raw_payload": {"order": raw, "items": items_raw},
        }
        ship_date = _mfn_ship_date(
            fields["fulfillment_channel"], fields["order_status"], raw.get("LastUpdateDate", "")
        )
        if ship_date:
            mfn_ship_dates[oid] = ship_date

        result = store.upsert_order(conn, fields, lines)
        counts[result] = counts.get(result, 0) + 1

    if mfn_ship_dates:
        counts["mfn_ship_dates_set"] = store.set_actual_ship_dates(conn, mfn_ship_dates)
    return counts


def _lines_from_items(items_raw: list[dict]) -> list[dict]:
    lines = []
    for item in items_raw:
        sku = (item.get("SellerSKU") or "").strip()
        if not sku:
            continue
        qty_ordered = float(item.get("QuantityOrdered") or 0)
        qty_shipped = float(item.get("QuantityShipped") or 0)
        qty = qty_shipped if qty_shipped > 0 else qty_ordered
        if qty <= 0:
            continue

        def _amt(key: str) -> float:
            return float((item.get(key) or {}).get("Amount") or 0)

        item_price = _amt("ItemPrice")
        lines.append(
            {
                "order_item_id": (item.get("OrderItemId") or "").strip(),
                "seller_sku": sku,
                "asin": item.get("ASIN", ""),
                "title": (item.get("Title") or "")[:140],
                "quantity": qty,
                "unit_price": round(item_price / qty, 5) if qty else 0.0,  # GROSS
                "item_price_amount": item_price,
                "item_tax_amount": _amt("ItemTax"),
                "shipping_price_amount": _amt("ShippingPrice"),
                "shipping_tax_amount": _amt("ShippingTax"),
            }
        )
    return lines
