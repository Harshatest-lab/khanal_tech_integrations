"""Amazon Flex (PRCJ) fetch handler.

Pulls the SP-API fulfilled-shipments report, filters to PRCJ (the real
FBA discriminator -- see amazon_reports_api.py / the design doc), resolves
items via amazon_flex_items, and builds a synthetic saleOrderDTO-shaped
dict per shipment -- shaped to match what a REAL Unicommerce order looks
like (same field names: itemSku, sellingPriceWithoutTaxesAndDiscount,
taxPercentage, shippingAddress.state, ...) so store.upsert_order(),
mappers.order_ready_for_invoice(), and sap_payload.ar_invoice_payload()'s
saleOrderItems-normalization branch all handle it completely unmodified --
confirmed against real code (2026-08-20), not assumed. State-name
normalization ("KERALA" -> "KL") and tax-bucket classification (intra vs
inter-state) are already generic in geo.py; nothing Flex-specific needed
there either.

Volume is ~15-20/day (confirmed against a real 7-day pull: 136 PRCJ rows /
7 days), so a single daily report-mode pull is sufficient -- unlike
amazon-us-b2c's hourly-incremental + daily-report dual mode, which exists
for ~500-1000x higher volume.

CFG (bundle) expansion and refund/credit-note fetch are NOT in this file --
see amazon_flex_items.py's docstring (CFG never observed in this service's
history) and the design doc's phase 6 (refunds, same dry-run-then-wire
pattern, not yet built).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from unicommerce import amazon_flex_items as flex_items
from unicommerce import amazon_reports_api as reports
from unicommerce import store
from unicommerce.credentials import get_flex_lwa_credentials
from unicommerce.settings import get_settings

logger = logging.getLogger(__name__)

CHANNEL_NAME = "AMAZON_FLEX"
PRCJ_FULFILLMENT_CENTER = "PRCJ"
_GST_SLABS = (0, 5, 12, 18, 28)


def _to_float(value) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _parse_shipments_report(content: str) -> list[dict]:
    lines = content.splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    rows = []
    for raw in lines[1:]:
        cols = raw.split("\t")
        if len(cols) != len(header):
            continue
        rows.append(dict(zip(header, cols)))
    return rows


def _build_synthetic_order(shipment_rows: list[dict], sku_map: dict, settings) -> tuple[str, dict] | None:
    """One PRCJ shipment (one row per SKU on it) -> (uniware_id, synthetic
    saleOrderDTO). Returns None if the shipment has no resolvable lines --
    caller counts that as an error to investigate, never silently drops it."""
    first = shipment_rows[0]
    amazon_order_id = (first.get("amazon-order-id") or "").strip()
    shipment_id = (first.get("shipment-id") or "").strip()
    if not amazon_order_id or not shipment_id:
        return None
    # Per-shipment, not per-order: an order can split across a PRCJ
    # shipment and a real-FBA shipment (khanal-oms measured ~10/month), or
    # rarely across two PRCJ shipments -- uniware_id must be unique per
    # shipment for NumAtCard dedup to work (see sap_payload.py's
    # AMAZON_FLEX branch and sap_client.find_invoice_by_num_at_card).
    uniware_id = f"FLEX-{amazon_order_id}-{shipment_id}"

    items = []
    for row in shipment_rows:
        seller_sku = (row.get("sku") or "").strip()
        qty = _to_float(row.get("quantity-shipped"))
        if not seller_sku or qty <= 0:
            continue
        try:
            item_code = flex_items.resolve_seller_sku(sku_map, settings, seller_sku)
        except flex_items.SkuUnresolved:
            logger.warning("flex_fetch(%s): unresolved seller SKU %r, skipping line", uniware_id, seller_sku)
            continue

        item_price = _to_float(row.get("item-price"))
        item_tax = _to_float(row.get("item-tax"))
        raw_tax_percent = (item_tax / item_price) * 100 if item_price > 0 else 0.0
        # GST only has fixed slabs -- settings.tax_code_map has no entry
        # for anything else, so a raw item-tax/item-price ratio (rounding
        # noise in Amazon's own reported split, e.g. 16.99 -> "17") fails
        # to resolve a tax code entirely (confirmed live 2026-08-20: "No
        # tax code mapped for key: 17_intra"). Snap to the nearest real
        # slab instead of trusting the raw ratio.
        tax_percent = min(_GST_SLABS, key=lambda slab: abs(slab - raw_tax_percent))

        # created/updated must be a real value, not absent: mappers.
        # build_line_items() falls back to safe_str(item.get("created")) --
        # "" for a missing key -- straight into unicommerce_order_lines'
        # timestamp-with-time-zone columns, which Postgres rejects outright
        # (confirmed live 2026-08-20, first real write attempt). Real UC
        # orders never hit this because UC always sends item-level
        # created/updated; shipment-date is the closest equivalent Amazon's
        # report actually has.
        line_timestamp = row.get("shipment-date") or row.get("purchase-date") or ""
        item_code_field = (row.get("amazon-order-item-id") or "").strip()
        channel_product_id = (row.get("asin") or "").strip()

        if not flex_items.is_cfg_parent(item_code):
            items.append(
                {
                    "itemSku": item_code,
                    "sellerSkuCode": seller_sku,
                    "channelProductId": channel_product_id,
                    "quantity": qty,
                    "totalPrice": item_price,
                    "sellingPrice": item_price,
                    "sellingPriceWithoutTaxesAndDiscount": round(item_price / qty, 4) if qty else 0.0,
                    "taxPercentage": tax_percent,
                    "statusCode": "DISPATCHED",
                    "cancellationReason": "",
                    "onHold": False,
                    "bundleSkuCode": "",
                    "code": item_code_field,
                    "facilityCode": "",
                    "created": line_timestamp,
                    "updated": line_timestamp,
                }
            )
            continue

        # CFG (Sales BOM) parent: expand into child rows tagged with
        # bundleSkuCode -- sap_payload.py's existing bundle-grouping picks
        # these up and auto-generates the iSalesTree parent line itself
        # (do not add that line here). Pricing follows the MTR script's
        # proven pattern (verified live against real SAP behavior): SAP
        # zeroes the parent's own price, so the whole line's taxable value
        # rides on the FIRST child; every other child gets UnitPrice=0 but
        # still carries its own real quantity, so stock/batch consumption
        # for every component is still correct.
        try:
            children = flex_items.get_product_tree_children(item_code)
        except flex_items.CfgExpansionFailed as e:
            logger.error("flex_fetch(%s): %s -- skipping line", uniware_id, e)
            continue

        remaining_value = item_price
        for child_idx, child in enumerate(children, start=1):
            child_qty = qty * float(child["quantity"])
            if child_qty <= 0:
                continue
            child_unit_price = round(remaining_value / child_qty, 4) if remaining_value and child_qty else 0.0
            remaining_value = 0.0
            items.append(
                {
                    "itemSku": child["item_code"],
                    "sellerSkuCode": seller_sku,
                    "channelProductId": channel_product_id,
                    "quantity": child_qty,
                    "totalPrice": child_unit_price * child_qty,
                    "sellingPrice": child_unit_price * child_qty,
                    "sellingPriceWithoutTaxesAndDiscount": child_unit_price,
                    "taxPercentage": tax_percent,
                    "statusCode": "DISPATCHED",
                    "cancellationReason": "",
                    "onHold": False,
                    "bundleSkuCode": item_code,
                    # Indexed, not the bare order-item-id: a SAP ProductTree
                    # can list the same child item code as more than one
                    # separate line (e.g. CFGHN0006 -> 2x FGHN0058 as two
                    # distinct ProductTreeLines, not one line qty=2) --
                    # every child of one bundle line shares the same
                    # amazon-order-item-id, and unicommerce_order_lines has
                    # a UNIQUE(uniware_id, code) constraint, so reusing the
                    # bare id for every child collides and silently fails
                    # to ever write (confirmed live 2026-08-21 on a real
                    # order: the shipment was fetched every cycle but never
                    # actually persisted, leaving stale pre-fix data
                    # in place indefinitely).
                    "code": f"{item_code_field}-{child_idx}",
                    "facilityCode": "",
                    "created": line_timestamp,
                    "updated": line_timestamp,
                }
            )

    if not items:
        return None

    order = {
        # NumAtCard (not this "code") is the dedup key for AMAZON_FLEX --
        # see sap_payload.py -- so this can safely be the Amazon order id
        # (shared across a split order's shipments) without reintroducing
        # a dedup collision.
        "code": amazon_order_id,
        "channel": CHANNEL_NAME,
        "channelName": CHANNEL_NAME,
        # The shipments report only lists items Amazon has actually
        # dispatched -- every row here is inherently "complete", there is
        # no separate order-status field to read from this report.
        "status": "COMPLETE",
        "created": first.get("purchase-date") or "",
        "updated": first.get("shipment-date") or "",
        "shippingAddress": {
            "name": (first.get("recipient-name") or "").strip(),
            "addressLine1": (first.get("ship-address-1") or "").strip(),
            "addressLine2": (first.get("ship-address-2") or "").strip(),
            "city": (first.get("ship-city") or "").strip(),
            "state": (first.get("ship-state") or "").strip(),
            "pincode": (first.get("ship-postal-code") or "").strip(),
            "country": (first.get("ship-country") or "IN").strip(),
        },
        "saleOrderItems": items,
    }
    return uniware_id, order


def fetch_flex_shipments(lookback_days: int = 2) -> dict:
    """Pull PRCJ shipments for the last `lookback_days` days and stage them
    into unicommerce_orders/unicommerce_order_lines with
    channel_name='AMAZON_FLEX', pipeline_status='fetched' -- the exact same
    tables/columns every other channel uses, so promote_fetched_to_ready
    and post_ready_orders pick these up unmodified on the next cycle."""
    settings = get_settings(force_reload=True)
    creds = get_flex_lwa_credentials()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    report_id = reports.create_report(
        reports.FLEX_SHIPMENTS_REPORT_TYPE,
        creds.marketplace_id,
        start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    doc_id = reports.poll_report(report_id)
    content = reports.download_report(doc_id)
    rows = _parse_shipments_report(content)

    prcj_rows = [r for r in rows if (r.get("fulfillment-center-id") or "") == PRCJ_FULFILLMENT_CENTER]
    logger.info("flex_fetch: %s total shipment rows, %s PRCJ rows", len(rows), len(prcj_rows))

    by_shipment: dict[str, list[dict]] = {}
    for row in prcj_rows:
        shipment_id = row.get("shipment-id") or ""
        if shipment_id:
            by_shipment.setdefault(shipment_id, []).append(row)

    conn = store.connect()
    try:
        sku_map = flex_items.build_sku_map(conn)

        created = updated = skipped_posted = errors = 0
        for shipment_id, shipment_rows in by_shipment.items():
            try:
                result = _build_synthetic_order(shipment_rows, sku_map, settings)
                if result is None:
                    logger.error("flex_fetch: shipment %s produced no resolvable lines", shipment_id)
                    errors += 1
                    continue
                uniware_id, order = result
                # store.upsert_order commits its own work (this codebase's
                # convention -- every store.py mutator self-commits, not
                # designed to be wrapped in an outer transaction). If a call
                # fails before its internal commit, the connection is left
                # aborted -- roll back explicitly so one bad row doesn't
                # poison every subsequent upsert in the run (confirmed live
                # 2026-08-20: without this, "current transaction is
                # aborted" cascaded through the rest of the batch).
                outcome = store.upsert_order(conn, uniware_id, order)
                if outcome == "created":
                    created += 1
                elif outcome == "updated":
                    updated += 1
                elif outcome == "skipped_posted":
                    skipped_posted += 1
            except Exception:
                logger.exception("flex_fetch: failed to process shipment %s", shipment_id)
                conn.rollback()
                errors += 1
    finally:
        conn.close()

    return {
        "total_rows": len(rows),
        "prcj_rows": len(prcj_rows),
        "shipments": len(by_shipment),
        "created": created,
        "updated": updated,
        "skipped_posted": skipped_posted,
        "errors": errors,
    }
