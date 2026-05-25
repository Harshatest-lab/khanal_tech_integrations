"""Lazy psycopg connection pool suitable for Lambda (small max size).

Requires optional dependency: psycopg[pool] — add to service requirements.txt
when you enable DB access in Lambda.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from khanal_shared.config import get_settings

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)
_pool: "ConnectionPool | None" = None


def get_pool() -> "ConnectionPool | None":
    """Return a module-scoped pool, or None if DATABASE_URL is not configured."""
    global _pool
    settings = get_settings()
    if not settings.database_url:
        logger.warning("DATABASE_URL not set; DB pool disabled")
        return None

    if _pool is not None:
        return _pool

    try:
        from psycopg_pool import ConnectionPool
    except ImportError as exc:  # pragma: no cover - optional in minimal installs
        raise RuntimeError(
            "psycopg pool extras not installed; add psycopg[pool] to requirements.txt"
        ) from exc

    # Tune per workload; start conservative for Lambda + RDS Proxy.
    _pool = ConnectionPool(
        conninfo=settings.database_url,
        min_size=1,
        max_size=5,
        timeout=5.0,
        max_idle=55.0,
    )
    return _pool
