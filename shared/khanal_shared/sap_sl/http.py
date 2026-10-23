"""Shared urllib cookie jar + JSON helpers for SAP Business One Service Layer."""

from __future__ import annotations

import functools
import hashlib
import http.client
import http.cookiejar
import json
import logging
import os
import re
import ssl
from http.cookiejar import Cookie
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener


def _sap_sl_ssl_context() -> ssl.SSLContext:
    """Default: verify server cert. Set ``SAP_SL_VERIFY_SSL=false`` for self-signed SAP SL (dev only)."""
    v = (os.environ.get("SAP_SL_VERIFY_SSL") or "true").strip().lower()
    insecure = v in ("0", "false", "no", "off")
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _pinned_fingerprints() -> frozenset[str]:
    """SHA-256 fingerprints (hex; colons, spaces and case ignored; comma-separated for a renewal overlap) the
    Service Layer's certificate must match. Empty = not pinned.

    A malformed value raises rather than being ignored: a typo that silently left the connection unpinned
    would look exactly like a working pin."""
    raw = os.environ.get("SAP_SL_PINNED_SHA256") or ""
    pins: set[str] = set()
    for part in raw.split(","):
        pin = re.sub(r"[\s:]", "", part).lower()
        if not pin:
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", pin):
            raise ValueError("SAP_SL_PINNED_SHA256 must be comma-separated SHA-256 fingerprints (64 hex characters each)")
        pins.add(pin)
    return frozenset(pins)


logger = logging.getLogger(__name__)

# Fingerprints already reported by observe-only mode, so a busy Lambda logs each certificate once, not per request.
_observed_fingerprints: set[str] = set()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Completes the TLS handshake, then refuses the connection unless the server's certificate is one we pinned.

    With no pins it only observes: it lets the connection through (verification is already off in that mode) and
    logs the certificate's fingerprint once. The Service Layer only answers from AWS, so this is how the fingerprint
    to pin gets read from the real network path."""

    def __init__(self, *args: Any, pins: frozenset[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pins = pins

    def connect(self) -> None:
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        digest = hashlib.sha256(der).hexdigest() if der else ""
        if not self._pins:
            if digest and digest not in _observed_fingerprints:
                _observed_fingerprints.add(digest)
                logger.warning(
                    "SAP Service Layer certificate sha256=%s -- not pinned. Set SAP_SL_PINNED_SHA256 to this value "
                    "(after checking it against the certificate on the SAP server) to refuse any other certificate.",
                    digest,
                )
            return
        if digest not in self._pins:
            self.close()
            raise ssl.SSLError(f"SAP Service Layer certificate does not match the pinned fingerprint (got {digest[:16] or 'none'}...)")


class _PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, context: ssl.SSLContext, pins: frozenset[str]) -> None:
        super().__init__(context=context)
        self._pins = pins

    def https_open(self, req: Request) -> Any:
        return self.do_open(functools.partial(_PinnedHTTPSConnection, pins=self._pins), req, context=self._context)


def build_sap_cookie_opener_with_jar() -> tuple[Any, http.cookiejar.CookieJar]:
    pins = _pinned_fingerprints()
    jar = http.cookiejar.CookieJar()
    if pins:
        # The on-prem Service Layer serves a self-signed certificate, so CA validation (and a hostname match
        # against an IP address) can't work. Trust exactly the pinned certificate instead of any certificate.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handler: HTTPSHandler = _PinnedHTTPSHandler(ctx, pins)
    else:
        ctx = _sap_sl_ssl_context()
        # Verification off and nothing pinned: observe the certificate so it can be pinned. A normal verified
        # connection needs no such help.
        handler = _PinnedHTTPSHandler(ctx, frozenset()) if ctx.verify_mode == ssl.CERT_NONE else HTTPSHandler(context=ctx)
    opener = build_opener(HTTPCookieProcessor(jar), handler)
    return opener, jar


def build_sap_cookie_opener() -> Any:
    opener, _ = build_sap_cookie_opener_with_jar()
    return opener


def _escape_raw_controls_in_json_strings(text: str) -> str:
    """Escape raw U+0000–U+001F chars inside JSON strings.

    SAP B1 Service Layer sometimes returns Address (and similar) fields with a
    literal CR/LF between state and country, e.g. ``"Address":"MS\\rUSA"`` where
    ``\\r`` is an unescaped 0x0D. Strict ``json.loads`` rejects that even though
    the document was created successfully.
    """
    out: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if escape:
            out.append(ch)
            escape = False
            continue
        if in_string and ch == "\\":
            out.append(ch)
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string:
            code = ord(ch)
            if code < 0x20:
                if ch == "\r":
                    out.append("\\r")
                elif ch == "\n":
                    out.append("\\n")
                elif ch == "\t":
                    out.append("\\t")
                else:
                    out.append(f"\\u{code:04x}")
                continue
        out.append(ch)
    return "".join(out)


def loads_sap_json(text: str) -> Any:
    """Parse SAP SL JSON; tolerate unescaped control characters in string values."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_escape_raw_controls_in_json_strings(text))


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
            return status, loads_sap_json(out.decode("utf-8"))
    except HTTPError as e:
        err_raw = e.read()
        if not err_raw:
            return e.code, None
        try:
            return e.code, loads_sap_json(err_raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return e.code, {"raw": err_raw.decode("utf-8", errors="replace")[:800]}


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
