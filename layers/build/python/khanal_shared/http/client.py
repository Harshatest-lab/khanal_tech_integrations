"""HTTP client utilities — thin wrapper; extend with retries per vendor."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout_sec: float = 10.0,
) -> HttpResponse:
    data = None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url=url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read()
            return HttpResponse(
                status=getattr(resp, "status", resp.getcode()),
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=raw,
            )
    except urllib.error.HTTPError as e:
        raw = e.read()
        logger.warning("http_error status=%s url=%s", e.code, url)
        return HttpResponse(status=e.code, headers={}, body=raw)
