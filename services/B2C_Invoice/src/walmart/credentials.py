"""Walmart API + DB settings from environment (Lambda / local).

For local-only secrets, copy values into ``credentials_local.py`` (gitignored).
"""

from __future__ import annotations

import os


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


DATABASE_URL = _env("DATABASE_URL")
WM_API_BASE = _env("WM_API_BASE", "https://marketplace.walmartapis.com/v3")
WM_SVC_NAME = _env("WM_SVC_NAME", "Walmart Marketplace")
WM_BASIC_AUTH = _env("WM_BASIC_AUTH")

try:
    from walmart.credentials_local import (  # type: ignore[import-not-found]
        DATABASE_URL as _LOCAL_DATABASE_URL,
        WM_BASIC_AUTH as _LOCAL_WM_BASIC_AUTH,
    )

    if _LOCAL_DATABASE_URL:
        DATABASE_URL = _LOCAL_DATABASE_URL.strip()
    if _LOCAL_WM_BASIC_AUTH:
        WM_BASIC_AUTH = _LOCAL_WM_BASIC_AUTH.strip()
except ImportError:
    pass
