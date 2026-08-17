"""SP-API client: LWA token + throttle-safe GET/POST.

Ported from the proven amazon_us_core client with the production fix
applied: 20 retry attempts (7 was too few for restricted-tier throttling
on bulk pulls). LWA token is cached in-process for the Lambda container
lifetime — no shared cache needed at this call volume.
"""

from __future__ import annotations

import logging
import time

import requests

from amazon_us import constants
from amazon_us.credentials import get_lwa_credentials

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
_LWA_BUFFER_SEC = 60
# 20 attempts at exponential-capped-64s backoff can alone sum to ~960s --
# more than the entire 900s Lambda budget for a SINGLE call. Confirmed live
# 2026-08-17: getOrderItems (documented at a generous 0.5 req/s, not one of
# the known slow-refill paths) got caught in the same extended throttle as
# getOrders and ate the full 900s working through orders one at a time,
# each one individually retrying up to the old cap. 8 attempts (worst case
# ~191s) leaves real budget for the rest of the run.
_MAX_ATTEMPTS = 8
_LWA_MAX_ATTEMPTS = 4

# Operations whose real sustained rate (confirmed against SP-API's published
# default usage plan) is far too slow for generic exponential backoff to
# reach within a 900s Lambda: getOrders and createReport both sit at
# ~0.0167 req/s (1 request per 60s) -- exponential backoff capped at 64s
# across 20 attempts can spend the entire Lambda budget retrying a SINGLE
# call and never succeed. Paced to the documented rate instead, with a
# breaker that gives up after a handful of attempts rather than all 20 --
# confirmed live 2026-08-17: an account already deep in a throttle storm
# stayed 429'd for close to two hours, well past normal burst recovery.
_SLOW_REFILL_PATHS = {
    "/orders/v0/orders": 60.0,           # getOrders (exact match only -- getOrderItems has its own, much faster quota)
    "/reports/2021-06-30/reports": 60.0,  # createReport (exact match only -- polling .../reports/{id} is a different operation)
}
_SLOW_PATH_MAX_ATTEMPTS = 5
_known_rate_per_sec: dict[str, float] = {}


class SpApiAuthError(Exception):
    """SP-API 401 persisted after an LWA token refresh — credentials invalid."""


class SpApiThrottledError(Exception):
    """Circuit breaker tripped: max_attempts consecutive 429s on one call.
    Not a bug -- the account's quota for this operation genuinely hasn't
    recovered. Callers should persist whatever partial progress exists and
    let the next scheduled invocation try again (or, for a per-item call
    inside a loop, skip just this item and move on), instead of burning
    the rest of the Lambda budget on a bucket that isn't going to refill
    any faster for more retrying."""


def _record_rate_limit(path: str, resp: requests.Response) -> None:
    header = resp.headers.get("x-amzn-RateLimit-Limit")
    if not header:
        return
    try:
        rate = float(header)
    except ValueError:
        return
    if rate > 0:
        _known_rate_per_sec[path] = rate


def _throttle_wait(path: str) -> float:
    return max(1.0 / _known_rate_per_sec[path], 1.0) if path in _known_rate_per_sec else _SLOW_REFILL_PATHS[path]


_token: str | None = None
_token_expires_at: float = 0.0


def get_lwa_token(force_refresh: bool = False) -> str:
    """Fetch (or reuse the cached) LWA access token.

    Retries transient failures from Amazon's OAuth endpoint itself
    (connection errors, 5xx, occasional bare 401s) a few times with
    backoff -- this call sits ahead of every SP-API request's own
    retry loop, so without a retry here a single momentary hiccup on
    api.amazon.com kills the whole Lambda invocation instead of just
    this one token fetch."""
    global _token, _token_expires_at
    if not force_refresh and _token and time.time() < _token_expires_at:
        return _token

    lwa = get_lwa_credentials()
    for attempt in range(_LWA_MAX_ATTEMPTS):
        try:
            resp = requests.post(
                constants.LWA_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": lwa.refresh_token,
                    "client_id": lwa.client_id,
                    "client_secret": lwa.client_secret,
                },
                timeout=15,
            )
            resp.raise_for_status()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.HTTPError) as exc:
            if attempt == _LWA_MAX_ATTEMPTS - 1:
                raise
            wait = min(2**attempt, 8)
            logger.warning(
                "LWA token refresh failed, retry in %ss (attempt %s/%s): %s",
                wait, attempt + 1, _LWA_MAX_ATTEMPTS, exc,
            )
            time.sleep(wait)
            continue

        body = resp.json()
        _token = body["access_token"]
        _token_expires_at = time.time() + max(60, int(body.get("expires_in", 3600)) - _LWA_BUFFER_SEC)
        return _token


def _headers() -> dict:
    return {
        "x-amz-access-token": get_lwa_token(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get(path: str, params: dict | None = None) -> requests.Response:
    """GET with 429 backoff, 401 token refresh, ConnectionError retry.
    Slow-refill operations (see _SLOW_REFILL_PATHS) pace to the documented
    sustained rate; every path circuit-breaks (SpApiThrottledError) once
    max_attempts is exhausted rather than silently handing the caller a
    stale 429 response -- confirmed live that a generic path can get caught
    in an extended throttle same as the two known-slow ones, and a caller
    doing resp.json() on an unrecognized 429 body fails in a much less
    obvious way than a named exception."""
    url = constants.SP_API_BASE + path
    slow = path in _SLOW_REFILL_PATHS
    max_attempts = _SLOW_PATH_MAX_ATTEMPTS if slow else _MAX_ATTEMPTS
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=30)
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            wait = min(2**attempt, 64)
            logger.warning("SP-API ConnectionError on %s, retry in %ss (%s): %s", path, wait, attempt + 1, exc)
            time.sleep(wait)
            continue
        last_exc = None
        if resp.status_code == 429:
            if slow:
                _record_rate_limit(path, resp)
                wait = _throttle_wait(path)
                logger.warning("SP-API 429 on %s (slow-refill), retry in %ss (attempt %s/%s)", path, wait, attempt + 1, max_attempts)
            else:
                wait = min(2**attempt, 64)
                logger.warning("SP-API 429 on %s, retry in %ss (attempt %s/%s)", path, wait, attempt + 1, max_attempts)
            time.sleep(wait)
            continue
        if slow:
            _record_rate_limit(path, resp)
        if resp.status_code == 401:
            get_lwa_token(force_refresh=True)
            resp = requests.get(url, headers=_headers(), params=params, timeout=30)
            if resp.status_code == 401:
                raise SpApiAuthError(f"SP-API 401 on {path} after token refresh")
        return resp
    if last_exc:
        raise last_exc
    raise SpApiThrottledError(f"GET {path}: still throttled after {max_attempts} attempts")


def post(path: str, body: dict) -> requests.Response:
    """POST with 429 backoff and 401 token refresh. See get() for the
    pacing/circuit-breaker behavior."""
    url = constants.SP_API_BASE + path
    slow = path in _SLOW_REFILL_PATHS
    max_attempts = _SLOW_PATH_MAX_ATTEMPTS if slow else _MAX_ATTEMPTS
    for attempt in range(max_attempts):
        resp = requests.post(url, headers=_headers(), json=body, timeout=30)
        if resp.status_code == 429:
            if slow:
                _record_rate_limit(path, resp)
                wait = _throttle_wait(path)
                logger.warning("SP-API 429 on POST %s (slow-refill), retry in %ss (attempt %s/%s)", path, wait, attempt + 1, max_attempts)
            else:
                wait = min(2**attempt, 64)
                logger.warning("SP-API 429 on POST %s, retry in %ss (attempt %s/%s)", path, wait, attempt + 1, max_attempts)
            time.sleep(wait)
            continue
        if slow:
            _record_rate_limit(path, resp)
        if resp.status_code == 401:
            get_lwa_token(force_refresh=True)
            resp = requests.post(url, headers=_headers(), json=body, timeout=30)
            if resp.status_code == 401:
                raise SpApiAuthError(f"SP-API 401 on POST {path} after token refresh")
        return resp
    raise SpApiThrottledError(f"POST {path}: still throttled after {max_attempts} attempts")


def delete(path: str) -> requests.Response:
    """DELETE with 429 backoff and 401 token refresh."""
    url = constants.SP_API_BASE + path
    resp: requests.Response | None = None
    for attempt in range(_MAX_ATTEMPTS):
        resp = requests.delete(url, headers=_headers(), timeout=30)
        if resp.status_code == 429:
            wait = min(2**attempt, 64)
            logger.warning("SP-API 429 on DELETE %s, retry in %ss", path, wait)
            time.sleep(wait)
            continue
        if resp.status_code == 401:
            get_lwa_token(force_refresh=True)
            resp = requests.delete(url, headers=_headers(), timeout=30)
            if resp.status_code == 401:
                raise SpApiAuthError(f"SP-API 401 on DELETE {path} after token refresh")
        return resp
    return resp  # type: ignore[return-value]


# ── Orders API ───────────────────────────────────────────────────────────

def fetch_orders(created_after: str, created_before: str | None = None) -> list[dict]:
    """All orders in a CreatedAfter/CreatedBefore window (paginated, UTC ISO)."""
    params: dict = {
        "MarketplaceIds": constants.MARKETPLACE_ID,
        "CreatedAfter": created_after,
        "MaxResultsPerPage": _PAGE_SIZE,
    }
    if created_before:
        params["CreatedBefore"] = created_before
    return _fetch_order_pages(params)


def fetch_orders_by_update(last_updated_after: str) -> list[dict]:
    """Incremental sync: everything updated since the watermark."""
    params = {
        "MarketplaceIds": constants.MARKETPLACE_ID,
        "LastUpdatedAfter": last_updated_after,
        "MaxResultsPerPage": _PAGE_SIZE,
    }
    return _fetch_order_pages(params)


def _fetch_order_pages(params: dict) -> list[dict]:
    orders: list[dict] = []
    next_token = None
    while True:
        page_params = dict(params)
        if next_token:
            page_params = {"MarketplaceIds": params["MarketplaceIds"], "NextToken": next_token}
        resp = get("/orders/v0/orders", page_params)
        resp.raise_for_status()
        payload = resp.json().get("payload", {})
        orders.extend(payload.get("Orders", []))
        next_token = payload.get("NextToken")
        if not next_token:
            return orders


def fetch_order_items(amazon_order_id: str) -> list[dict]:
    items: list[dict] = []
    next_token = None
    while True:
        params = {"NextToken": next_token} if next_token else None
        resp = get(f"/orders/v0/orders/{amazon_order_id}/orderItems", params)
        resp.raise_for_status()
        payload = resp.json().get("payload", {})
        items.extend(payload.get("OrderItems", []))
        next_token = payload.get("NextToken")
        if not next_token:
            return items
