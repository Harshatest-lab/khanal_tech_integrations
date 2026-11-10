"""Read-only Freshdesk Solutions API calls (API v2, Basic auth with the API key)."""

from __future__ import annotations

import base64
import json
from typing import Any

from khanal_shared.http import request_json

from .credentials import FreshdeskConfig

TIMEOUT_SECONDS = 8.0


class FreshdeskApiError(RuntimeError):
    """Non-200 response from Freshdesk."""


def get(config: FreshdeskConfig, path: str) -> Any:
    token = base64.b64encode(f"{config.api_key}:X".encode()).decode()
    resp = request_json(
        "GET",
        f"https://{config.domain}/api/v2{path}",
        headers={"Authorization": f"Basic {token}"},
        timeout_sec=TIMEOUT_SECONDS,
    )
    if resp.status != 200:
        raise FreshdeskApiError(f"Freshdesk {resp.status} on {path}")
    return json.loads(resp.body or b"null")


def list_folders(config: FreshdeskConfig, category_id: int) -> list[dict[str, Any]]:
    return get(config, f"/solutions/categories/{category_id}/folders") or []


def list_articles(config: FreshdeskConfig, folder_id: int) -> list[dict[str, Any]]:
    return get(config, f"/solutions/folders/{folder_id}/articles?per_page=100") or []
