"""Shared helpers used across all Lambda handlers in this service."""

from __future__ import annotations

import hmac
import json
import os
from typing import Any


def response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def header_ci(headers: dict[str, Any] | None, name: str) -> str | None:
    """Case-insensitive header lookup."""
    if not headers:
        return None
    want = name.lower()
    for key, val in headers.items():
        if str(key).lower() == want and isinstance(val, str):
            return val
    return None


def delhivery_secret_error_response(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return error response if webhook secret header is wrong; None if OK.

    Read ``DELHIVERY_WEBHOOK_SECRET`` from ``os.environ`` (not ``get_settings()``) so
    updates take effect on every invoke. ``get_settings()`` is LRU-cached and would
    keep a stale secret in warm Lambda execution environments after a console/CI change.
    """
    expected = (os.environ.get("DELHIVERY_WEBHOOK_SECRET") or "").strip()
    if not expected:
        return None
    got = (header_ci(event.get("headers"), "x-delhivery-webhook-secret") or "").strip()
    if not got or not hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8")):
        return response(
            401,
            {"error": {"code": "unauthorized", "message": "Invalid or missing X-Delhivery-Webhook-Secret"}},
        )
    return None
