"""Shared HTTP helper for SP-API calls: auth header + 401/429 retry."""

import re
import time

import requests

from amazon_uk import amazon_auth

BASE_URL = "https://sellingpartnerapi-eu.amazon.com"
MARKETPLACE_ID = "A1F83G8C2ARO7P"  # Amazon UK

# Amazon only sends x-amzn-RateLimit-Limit on 200s, never on the 429 that
# actually needs it (confirmed live: GetOrders 429s carry no rate header at
# all). So the rate is learned from successful calls and reused for backoff
# on later 429s against the same operation. Path is normalized (dynamic id
# segments collapsed to "*") so e.g. every per-order detail call shares one
# cache entry instead of one each.
_ID_SEGMENT_RE = re.compile(r"[^/]*\d[^/]*")
_known_rates: dict[str, float] = {}


def _rate_cache_key(path: str) -> str:
    return _ID_SEGMENT_RE.sub("*", path)


def request(method: str, path: str, params: dict | None = None,
            json_body: dict | None = None, max_retries: int = 8) -> dict:
    """Call one SP-API endpoint. Refreshes the token once on 401, backs off on 429."""
    force_refresh = False
    last_error = None
    cache_key = _rate_cache_key(path)

    for attempt in range(max_retries):
        token = amazon_auth.get_access_token(force_refresh=force_refresh)
        try:
            resp = requests.request(
                method, BASE_URL + path,
                params=params, json=json_body,
                headers={"x-amz-access-token": token, "Content-Type": "application/json"},
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            last_error = e  # transient network error (DNS, timeout, reset) — retry
            time.sleep(2 ** attempt)
            continue

        rate = resp.headers.get("x-amzn-RateLimit-Limit")
        if rate:
            _known_rates[cache_key] = float(rate)

        if resp.status_code == 401:
            force_refresh = True
            continue
        if resp.status_code == 429:
            known_rate = _known_rates.get(cache_key)
            wait = (1 / known_rate) if known_rate else 65  # 65s: safe default even for GetOrders' 1/60s
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            # Amazon's own server error — confirmed live on page 2 of a
            # paginated bulk orders call (page 1 had just succeeded moments
            # before), so this is transient on Amazon's side, not something
            # wrong with the request. Same exponential backoff as a network
            # error, not the full rate-limit wait (this isn't a quota issue).
            last_error = requests.exceptions.HTTPError(f"{resp.status_code} server error", response=resp)
            time.sleep(2 ** attempt)
            continue

        resp.raise_for_status()
        return resp.json()

    raise RuntimeError(f"SP-API request failed after {max_retries} retries: {method} {path}") from last_error
