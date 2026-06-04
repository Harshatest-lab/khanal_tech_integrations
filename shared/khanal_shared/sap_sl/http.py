"""Shared urllib cookie jar + JSON helpers for SAP Business One Service Layer."""

from __future__ import annotations

import http.cookiejar
import json
import ssl
from http.cookiejar import Cookie
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener


def build_sap_cookie_opener_with_jar() -> tuple[Any, http.cookiejar.CookieJar]:
    ctx = ssl.create_default_context()
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar), HTTPSHandler(context=ctx))
    return opener, jar


def build_sap_cookie_opener() -> Any:
    opener, _ = build_sap_cookie_opener_with_jar()
    return opener


def sap_sl_request_json(
    opener: Any,
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | list[Any] | None]:
    """Return ``(http_status, parsed_json_or_none)``. PATCH often returns 204 with empty body."""
    raw = None if body is None else json.dumps(body).encode("utf-8")
    headers: dict[str, str] = {}
    if raw is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=raw, method=method, headers=headers)
    try:
        with opener.open(req, timeout=90) as resp:
            status = resp.status
            out = resp.read()
            if not out:
                return status, None
            return status, json.loads(out.decode("utf-8"))
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"SAP SL HTTP {e.code}: {err_body[:800]}") from e


def inject_sap_sl_cookies(jar: http.cookiejar.CookieJar, host: str, b1session: str, routeid: str) -> None:
    """Attach Service Layer session cookies to ``jar`` for ``host`` (hostname or IP, no port)."""

    def _one(name: str, value: str) -> None:
        if not value:
            return
        c = Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=host,
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        jar.set_cookie(c)

    _one("B1SESSION", b1session.strip())
    _one("ROUTEID", routeid.strip())
