"""SP-API client for Amazon Flex (PRCJ): LWA token + throttle-safe GET/POST/DELETE.

Ported from amazon-us-b2c's amazon_us/sp_api.py (proven in this same repo/
Lambda layer -- `requests` is already a dependency there, unlike khanal-oms's
httpx, which isn't in this layer at all). Host/marketplace/LWA creds come
from unicommerce.credentials.get_flex_lwa_credentials() (SSM
/amazon-flex/lwa) instead of amazon_us's env-var constants.py, since Flex
reuses one already-working LWA refresh token rather than a per-service app
registration -- see docs/superpowers/specs/
2026-08-20-amazon-flex-sap-pipeline-design.md.

India (A21TJRUUN4KGV) is served by the EUROPE SP-API host, not NA -- baked
into the SSM param's "host" value, not hardcoded here.
"""

from __future__ import annotations

import logging
import time

import requests

from unicommerce.credentials import get_flex_lwa_credentials

logger = logging.getLogger(__name__)

_LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
_LWA_BUFFER_SEC = 60
# See amazon_us/sp_api.py's identical constant for the production incident
# (2026-08-17) that set this: 20 attempts at exponential-capped-64s backoff
# can alone exceed a Lambda's 900s budget for a SINGLE call.
_MAX_ATTEMPTS = 8
_LWA_MAX_ATTEMPTS = 4

# getOrders and createReport are both throttled to ~1 req/60s (SP-API's
# published usage plan) -- generic exponential backoff can't reach within a
# 900s Lambda, so these are paced to the documented rate with a breaker that
# gives up after a handful of attempts instead of all _MAX_ATTEMPTS.
_SLOW_REFILL_PATHS = {
    "/orders/v0/orders": 60.0,
    "/reports/2021-06-30/reports": 60.0,
}
_SLOW_PATH_MAX_ATTEMPTS = 5
_known_rate_per_sec: dict[str, float] = {}


class SpApiAuthError(Exception):
    """SP-API 401 persisted after an LWA token refresh -- credentials invalid."""


class SpApiThrottledError(Exception):
    """Circuit breaker tripped: max_attempts consecutive 429s on one call.
    Not a bug -- the account's quota for this operation genuinely hasn't
    recovered. Callers should persist partial progress and let the next
    scheduled invocation try again, instead of burning the rest of the
    Lambda budget on a bucket that isn't going to refill any faster."""


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
    """Fetch (or reuse the cached) LWA access token. Retries transient
    failures from Amazon's OAuth endpoint itself a few times with backoff --
    this call sits ahead of every SP-API request's own retry loop."""
    global _token, _token_expires_at
    if not force_refresh and _token and time.time() < _token_expires_at:
        return _token

    lwa = get_flex_lwa_credentials()
    for attempt in range(_LWA_MAX_ATTEMPTS):
        try:
            resp = requests.post(
                _LWA_TOKEN_URL,
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
                "Flex LWA token refresh failed, retry in %ss (attempt %s/%s): %s",
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
    See amazon_us/sp_api.py's get() for the pacing/circuit-breaker rationale."""
    url = get_flex_lwa_credentials().host + path
    slow = path in _SLOW_REFILL_PATHS
    max_attempts = _SLOW_PATH_MAX_ATTEMPTS if slow else _MAX_ATTEMPTS
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=30)
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            wait = min(2**attempt, 64)
            logger.warning("Flex SP-API ConnectionError on %s, retry in %ss (%s): %s", path, wait, attempt + 1, exc)
            time.sleep(wait)
            continue
        last_exc = None
        if resp.status_code == 429:
            if slow:
                _record_rate_limit(path, resp)
                wait = _throttle_wait(path)
                logger.warning("Flex SP-API 429 on %s (slow-refill), retry in %ss (attempt %s/%s)", path, wait, attempt + 1, max_attempts)
            else:
                wait = min(2**attempt, 64)
                logger.warning("Flex SP-API 429 on %s, retry in %ss (attempt %s/%s)", path, wait, attempt + 1, max_attempts)
            time.sleep(wait)
            continue
        if slow:
            _record_rate_limit(path, resp)
        if resp.status_code == 401:
            get_lwa_token(force_refresh=True)
            resp = requests.get(url, headers=_headers(), params=params, timeout=30)
            if resp.status_code == 401:
                raise SpApiAuthError(f"Flex SP-API 401 on {path} after token refresh")
        return resp
    if last_exc:
        raise last_exc
    raise SpApiThrottledError(f"GET {path}: still throttled after {max_attempts} attempts")


def post(path: str, body: dict) -> requests.Response:
    """POST with 429 backoff and 401 token refresh. See get() for pacing."""
    url = get_flex_lwa_credentials().host + path
    slow = path in _SLOW_REFILL_PATHS
    max_attempts = _SLOW_PATH_MAX_ATTEMPTS if slow else _MAX_ATTEMPTS
    for attempt in range(max_attempts):
        resp = requests.post(url, headers=_headers(), json=body, timeout=30)
        if resp.status_code == 429:
            if slow:
                _record_rate_limit(path, resp)
                wait = _throttle_wait(path)
                logger.warning("Flex SP-API 429 on POST %s (slow-refill), retry in %ss (attempt %s/%s)", path, wait, attempt + 1, max_attempts)
            else:
                wait = min(2**attempt, 64)
                logger.warning("Flex SP-API 429 on POST %s, retry in %ss (attempt %s/%s)", path, wait, attempt + 1, max_attempts)
            time.sleep(wait)
            continue
        if slow:
            _record_rate_limit(path, resp)
        if resp.status_code == 401:
            get_lwa_token(force_refresh=True)
            resp = requests.post(url, headers=_headers(), json=body, timeout=30)
            if resp.status_code == 401:
                raise SpApiAuthError(f"Flex SP-API 401 on POST {path} after token refresh")
        return resp
    raise SpApiThrottledError(f"POST {path}: still throttled after {max_attempts} attempts")


def delete(path: str) -> requests.Response:
    """DELETE with 429 backoff and 401 token refresh."""
    url = get_flex_lwa_credentials().host + path
    resp: requests.Response | None = None
    for attempt in range(_MAX_ATTEMPTS):
        resp = requests.delete(url, headers=_headers(), timeout=30)
        if resp.status_code == 429:
            wait = min(2**attempt, 64)
            logger.warning("Flex SP-API 429 on DELETE %s, retry in %ss", path, wait)
            time.sleep(wait)
            continue
        if resp.status_code == 401:
            get_lwa_token(force_refresh=True)
            resp = requests.delete(url, headers=_headers(), timeout=30)
            if resp.status_code == 401:
                raise SpApiAuthError(f"Flex SP-API 401 on DELETE {path} after token refresh")
        return resp
    return resp  # type: ignore[return-value]
