"""SP-API Finances API: list refund events.

The Finances API reports refunds as MONEY (charge adjustments per SKU),
never a returned quantity — the credit-note builder infers quantity from
refunded principal / original unit price.

Production fixes carried over from the Frappe pipeline:
  - ShippingCharge / ShippingTax charge types are parsed (the original
    Principal+Tax-only parser silently dropped the buyer's refunded
    shipping — every CN was short by the freight amount).
  - PostedBefore is clamped to now-5min: Amazon 400s when it is less than
    ~2 minutes in the past, which happens whenever the caller passes
    today's date (its end-of-day is still in the future).

Known blind spot (documented, by design): shipping-only refunds do not
appear as Finances refund events at all — only the MTR shows them. The
monthly MTR reconciliation is the safety net that catches those.
"""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from amazon_us import sp_api

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

    refund_id: str  # "{order_id}-{PostedDate}" — SAP NumAtCard, the idempotency key
    amazon_order_id: str
    posted_date: str  # 'YYYY-MM-DD'
    item_adjustments: list[RefundItemAdjustment] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)  # the event exactly as the Finances API returned it


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
    MTR-displayed refund date by up to 1-2 weeks — callers should always use
    a window wider than the target month."""
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

        # financialEvents returns EVERY event type (fees, shipments,
        # adjustments, not just refunds), so a wide/open-ended window can
        # mean hundreds of pages just to surface a handful of refunds.
        # Confirmed for real 2026-08-14: a page mid-pagination 400'd with
        # an empty reason phrase (likely the NextToken going stale from
        # SP-API rate-limit backoff time accumulating between pages, sp_api
        # itself only retries 429/ConnectionError, not 400) -- that raised
        # straight out of this function, discarding every refund event
        # already collected from earlier pages and forcing a full re-scan
        # from posted_after on the caller's next attempt (which then hit
        # the exact same wall again -- 3 consecutive failed runs before
        # this was root-caused). Retry a failing page a few times with
        # backoff, and if it still won't recover, return what was already
        # collected instead of losing it -- post_refunds is idempotent by
        # refund_id, so a partial page just means the remainder gets
        # picked up on the next run rather than the whole run dying.
        resp = None
        for attempt in range(_PAGE_RETRY_ATTEMPTS):
            resp = sp_api.get(f"{_FINANCES_BASE}/financialEvents", params)
            if resp.status_code < 400:
                break
            logger.warning(
                "financialEvents page failed (attempt %s/%s): HTTP %s: %s",
                attempt + 1, _PAGE_RETRY_ATTEMPTS, resp.status_code, resp.text[:500],
            )
            if attempt + 1 < _PAGE_RETRY_ATTEMPTS:
                time.sleep(2**attempt)
        if resp.status_code >= 400:
            logger.error(
                "financialEvents page permanently failed after %s attempts, returning %s events collected so far",
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
        # The Finances API returns the SKU HTML-escaped ("Foodtopper_S&amp;C")
        # while the Orders API / our staged lines carry it raw ("Foodtopper_S&C"),
        # so the SKU never matched and the refund was skipped "no unit price
        # or SAP mapping" (orders 111-2977129-8568210, 113-1823665-9234661).
        sku = html.unescape(item.get("SellerSKU") or "").strip()
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
        raw=raw,
    )
