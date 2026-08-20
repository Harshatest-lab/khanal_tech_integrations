"""SP-API Finances API for Amazon Flex: list refund events.

Ported from amazon-us-b2c's amazon_us/finances_api.py, including its two
production fixes (ShippingCharge/ShippingTax parsing, PostedBefore clamp)
-- same SP-API operation, same failure modes apply here.

The Finances API reports refunds as MONEY (charge adjustments per SKU),
never a returned quantity -- the credit-note builder must infer quantity
from refunded principal / original unit price, same as amazon-us-b2c.

Known blind spot (documented, by design, carried over): shipping-only
refunds do not appear as Finances refund events at all.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from unicommerce import amazon_sp_api as sp_api

logger = logging.getLogger(__name__)

_FINANCES_BASE = "/finances/v0"


@dataclass
class RefundItemAdjustment:
    seller_sku: str
    principal_amount: float  # negative: money returned to the buyer
    tax_amount: float = 0.0
    shipping_amount: float = 0.0
    shipping_tax_amount: float = 0.0


@dataclass
class RefundEvent:
    """One refund transaction, as Amazon batches it: all SKU-level
    adjustments posted together for one order on one date."""

    refund_id: str  # "{order_id}-{PostedDate}" -- SAP NumAtCard, the idempotency key
    amazon_order_id: str
    posted_date: str  # 'YYYY-MM-DD'
    item_adjustments: list[RefundItemAdjustment] = field(default_factory=list)


def clamp_posted_before(posted_before: str) -> str:
    limit = datetime.now(timezone.utc) - timedelta(minutes=5)
    try:
        requested = datetime.fromisoformat(posted_before.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return posted_before
    if requested.tzinfo is None:
        requested = requested.replace(tzinfo=timezone.utc)
    if requested > limit:
        return limit.strftime("%Y-%m-%dT%H:%M:%SZ")
    return posted_before


def list_refund_events(posted_after: str, posted_before: str | None = None) -> list[RefundEvent]:
    """posted_after/posted_before: UTC ISO 8601. Amazon's PostedDate lags the
    return date by up to 1-2 weeks -- callers should use a window wider than
    the target period."""
    if posted_before:
        posted_before = clamp_posted_before(posted_before)

    events: list[RefundEvent] = []
    next_token = None
    _PAGE_RETRY_ATTEMPTS = 3

    while True:
        if next_token:
            params = {"NextToken": next_token}
        else:
            params = {"PostedAfter": posted_after, "MaxResultsPerPage": 100}
            if posted_before:
                params["PostedBefore"] = posted_before

        # financialEvents returns EVERY event type, not just refunds, so a
        # failing page must be retried rather than aborting the whole scan
        # and discarding events already collected -- see amazon_us's
        # finances_api.py for the production incident this guards against.
        resp = None
        for attempt in range(_PAGE_RETRY_ATTEMPTS):
            resp = sp_api.get(f"{_FINANCES_BASE}/financialEvents", params)
            if resp.status_code < 400:
                break
            logger.warning(
                "Flex financialEvents page failed (attempt %s/%s): HTTP %s: %s",
                attempt + 1, _PAGE_RETRY_ATTEMPTS, resp.status_code, resp.text[:500],
            )
            if attempt + 1 < _PAGE_RETRY_ATTEMPTS:
                time.sleep(2**attempt)
        if resp.status_code >= 400:
            logger.error(
                "Flex financialEvents page permanently failed after %s attempts, returning %s events collected so far",
                _PAGE_RETRY_ATTEMPTS, len(events),
            )
            return events

        payload = resp.json().get("payload", {})
        financial_events = payload.get("FinancialEvents", {})

        for raw in financial_events.get("RefundEventList") or []:
            event = _parse_refund_event(raw)
            if event:
                events.append(event)

        next_token = payload.get("NextToken")
        if not next_token:
            return events


def _parse_refund_event(raw: dict) -> RefundEvent | None:
    order_id = raw.get("AmazonOrderId", "")
    posted_date_raw = raw.get("PostedDate", "")
    if not order_id or not posted_date_raw:
        return None

    adjustments = []
    for item in raw.get("ShipmentItemAdjustmentList") or []:
        sku = (item.get("SellerSKU") or "").strip()
        if not sku:
            continue

        principal = tax = shipping = shipping_tax = 0.0
        for charge in item.get("ItemChargeAdjustmentList") or []:
            amount = float((charge.get("ChargeAmount") or {}).get("CurrencyAmount") or 0)
            charge_type = charge.get("ChargeType", "")
            if charge_type == "Principal":
                principal += amount
            elif charge_type == "Tax":
                tax += amount
            elif charge_type == "ShippingCharge":
                shipping += amount
            elif charge_type == "ShippingTax":
                shipping_tax += amount

        if principal == 0 and tax == 0 and shipping == 0 and shipping_tax == 0:
            continue
        adjustments.append(
            RefundItemAdjustment(
                seller_sku=sku,
                principal_amount=principal,
                tax_amount=tax,
                shipping_amount=shipping,
                shipping_tax_amount=shipping_tax,
            )
        )

    if not adjustments:
        return None
    return RefundEvent(
        refund_id=f"{order_id}-{posted_date_raw}",
        amazon_order_id=order_id,
        posted_date=posted_date_raw[:10],
        item_adjustments=adjustments,
    )
