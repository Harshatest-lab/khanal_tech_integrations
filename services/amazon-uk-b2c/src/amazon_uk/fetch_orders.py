"""Fetch orders and their line items via ONE endpoint (bulk, paginated).

Replaces the previous 2-call design (v0 order list + a per-order detail
call) — confirmed live that /orders/2026-01-01/orders (the plural, no-id
form) returns the exact same rich per-order shape as the single-order
detail endpoint, already embedding orderItems with full proceeds/tax/
discount/recipient detail, across a paginated bulk list. That eliminates
the entire per-order loop (was 1214 individual calls for a full month).
"""

import time

from amazon_uk import applog
from amazon_uk import sp_api

log = applog.get_logger("amz_sync")

INCLUDED_DATA = (
    "BUYER,RECIPIENT,PROCEEDS,EXPENSE,PROMOTION,CANCELLATION,"
    "FULFILLMENT,PACKAGES,TAX,PAYMENT,FULFILLMENT_ORDERS"
)

# Confirmed live: 0.0056 req/sec (~178s/request) — much slower per call than
# the old v0 list endpoint, but each page already carries full item detail,
# so there's no per-order follow-up call to also pace.
PAGE_PACING_SECONDS = 180

# A page needs PAGE_PACING_SECONDS of waiting plus the request itself; this is
# the slack kept on top so a page is only started if it can really finish.
PAGE_REQUEST_MARGIN_SECONDS = 15

_last_request_at: float | None = None


class FetchBudgetExceeded(Exception):
    """The time budget cannot fit another page. ``partial`` holds the orders
    fetched so far — safe to upsert (idempotent), but the window they came
    from is NOT complete, so the watermark must not move past it."""

    def __init__(self, partial: list[dict]):
        super().__init__(f"time budget exhausted after {len(partial)} orders")
        self.partial = partial


def _wait_for_next_page(deadline: float | None, partial: list[dict]) -> None:
    """Pace requests to PAGE_PACING_SECONDS apart — across slices too, not
    just across pages of one query. Raises FetchBudgetExceeded instead of
    starting a page the deadline (time.monotonic() based) cannot cover."""
    global _last_request_at
    wait = 0.0
    if _last_request_at is not None:
        wait = max(0.0, _last_request_at + PAGE_PACING_SECONDS - time.monotonic())
    if deadline is not None and time.monotonic() + wait + PAGE_REQUEST_MARGIN_SECONDS > deadline:
        raise FetchBudgetExceeded(partial)
    if wait:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _paginate(params: dict, deadline: float | None = None) -> list[dict]:
    orders = []
    page = 1
    while True:
        _wait_for_next_page(deadline, orders)
        payload = sp_api.request("GET", "/orders/2026-01-01/orders", params)
        page_orders = payload.get("orders", [])
        orders += page_orders
        # Logged per page (not just before/after the whole fetch) since a
        # single fetch can run for many minutes at PAGE_PACING_SECONDS/page
        # — without this a redirected run looks identical to a hang for the
        # entire duration. applog's StreamHandler writes straight to stdout
        # unbuffered, so the old print(..., flush=True) workaround is no
        # longer needed.
        log.info(f"page {page}: {len(page_orders)} orders (total so far: {len(orders)})")

        next_token = payload.get("pagination", {}).get("nextToken")
        if not next_token:
            return orders

        # Confirmed live: unlike the old v0 endpoint, this one requires the
        # full original params again on every page, not just the token
        # (a token-only request 400s: "One and only one of createdAfter or
        # lastUpdatedAfter must be provided").
        #
        # Request param name is "paginationToken", NOT "nextToken" — that's
        # only the name of the field in the *response*. Real bug found live
        # 2026-09-16: sending it back as "nextToken" isn't rejected (Amazon
        # just silently ignores the unrecognized param and reruns the query
        # fresh), so every page came back identical to page 1 — a request
        # spanning >100 matching orders looked like it was returning
        # thousands of "new" orders while actually just looping page 1
        # forever. Confirmed fix live: paginationToken gives 0% overlap
        # between pages. Root cause per Amazon's own error message format,
        # see github.com/amzn/selling-partner-api-models/discussions/5222.
        params = {**params, "paginationToken": next_token}
        page += 1


def fetch_orders(created_after: str, created_before: str) -> list[dict]:
    """One dict per order, each with 'orderItems' already embedded.
    Manual/backfill mode — orders CREATED in [created_after, created_before).
    Misses status changes (e.g. Pending -> Shipped) on orders created before
    created_after; use fetch_orders_by_update for that."""
    return _paginate({
        "marketplaceIds": sp_api.MARKETPLACE_ID,
        "createdAfter": created_after,
        "createdBefore": created_before,
        "includedData": INCLUDED_DATA,
    })


def fetch_orders_by_update(last_updated_after: str, last_updated_before: str | None = None,
                           deadline: float | None = None) -> list[dict]:
    """One dict per order, each with 'orderItems' already embedded.
    Incremental mode — every order UPDATED since last_updated_after,
    regardless of when it was created. This is what catches an order
    purchased days ago flipping to Shipped today. createdAfter/Before and
    lastUpdatedAfter are mutually exclusive on this endpoint (confirmed
    live: a token-only follow-up request 400s naming both).

    last_updated_before (optional) bounds the window — confirmed live
    2026-09-21: accepted, and every returned order's lastUpdatedTime falls
    inside [after, before]. sync_amazonuk.sync_incremental uses it to cap
    one run's work so a long gap is drained over several runs instead of
    timing out the Lambda in one.

    deadline (time.monotonic() value, optional): raise FetchBudgetExceeded
    rather than start a page that would run past it."""
    params = {
        "marketplaceIds": sp_api.MARKETPLACE_ID,
        "lastUpdatedAfter": last_updated_after,
        "includedData": INCLUDED_DATA,
    }
    if last_updated_before:
        params["lastUpdatedBefore"] = last_updated_before
    return _paginate(params, deadline)
