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
_MAX_ATTEMPTS = 20


class SpApiAuthError(Exception):
    """SP-API 401 persisted after an LWA token refresh — credentials invalid."""


_token: str | None = None
_token_expires_at: float = 0.0


def get_lwa_token(force_refresh: bool = False) -> str:
    global _token, _token_expires_at
    if not force_refresh and _token and time.time() < _token_expires_at:
        return _token

    lwa = get_lwa_credentials()
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
    """GET with 429 backoff, 401 token refresh, ConnectionError retry."""
    url = constants.SP_API_BASE + path
    last_exc: Exception | None = None
    resp: requests.Response | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=30)
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            wait = min(2**attempt, 64)
            logger.warning("SP-API ConnectionError on %s, retry in %ss (%s): %s", path, wait, attempt + 1, exc)
            time.sleep(wait)
            continue
        if resp.status_code == 429:
            wait = min(2**attempt, 64)
            logger.warning("SP-API 429 on %s, retry in %ss (attempt %s)", path, wait, attempt + 1)
            time.sleep(wait)
            continue
        if resp.status_code == 401:
            get_lwa_token(force_refresh=True)
            resp = requests.get(url, headers=_headers(), params=params, timeout=30)
            if resp.status_code == 401:
                raise SpApiAuthError(f"SP-API 401 on {path} after token refresh")
        return resp
    if last_exc:
        raise last_exc
    return resp  # type: ignore[return-value]


def post(path: str, body: dict) -> requests.Response:
    """POST with 429 backoff and 401 token refresh."""
    url = constants.SP_API_BASE + path
    resp: requests.Response | None = None
    for attempt in range(_MAX_ATTEMPTS):
        resp = requests.post(url, headers=_headers(), json=body, timeout=30)
        if resp.status_code == 429:
            wait = min(2**attempt, 64)
            logger.warning("SP-API 429 on POST %s, retry in %ss", path, wait)
            time.sleep(wait)
            continue
        if resp.status_code == 401:
            get_lwa_token(force_refresh=True)
            resp = requests.post(url, headers=_headers(), json=body, timeout=30)
            if resp.status_code == 401:
                raise SpApiAuthError(f"SP-API 401 on POST {path} after token refresh")
        return resp
    return resp  # type: ignore[return-value]


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
