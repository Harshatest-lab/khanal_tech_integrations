"""Login With Amazon (LWA) authentication for SP-API.

Exchanges the long-lived refresh token for a short-lived access token
(1 hour) and caches it, so API calls reuse it until shortly before expiry.
"""

import time

import requests

from amazon_uk import credentials

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
REFRESH_MARGIN = 300  # refresh 5 minutes before actual expiry

_access_token = None
_expires_at = 0.0


def get_access_token(force_refresh: bool = False) -> str:
    """Return a valid access token, refreshing and caching as needed."""
    global _access_token, _expires_at

    if (not force_refresh and _access_token
            and time.time() < _expires_at - REFRESH_MARGIN):
        return _access_token  # cached, still valid

    lwa = credentials.get_lwa_credentials()
    resp = requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": lwa.refresh_token,
            "client_id": lwa.client_id,
            "client_secret": lwa.client_secret,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"LWA token exchange failed (HTTP {resp.status_code}): {resp.text}"
        )

    data = resp.json()
    _access_token = data["access_token"]
    _expires_at = time.time() + int(data.get("expires_in", 3600))
    return _access_token
