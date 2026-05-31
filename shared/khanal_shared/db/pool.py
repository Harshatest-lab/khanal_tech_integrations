"""Lazy psycopg connection pool suitable for Lambda (small max size).

Requires optional dependency: psycopg[pool] — add to service requirements.txt
when you enable DB access in Lambda.
"""

from __future__ import annotations

import logging
import os
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
        kwargs={
            # Shorter hangs on bad networks; keepalives help drop half-open sessions after Lambda freeze.
            "connect_timeout": 10,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        },
    )
    return _pool


def ensure_warmed_for_lambda() -> None:
    """Open one pooled connection during cold start (INIT), not inside the first request.

    Lambda REPORT Duration includes work done after INIT. Moving TCP/TLS + pool
    checkout to import time reduces wall-clock latency on the first webhook after
    a new execution environment is created (including under provisioned concurrency).

    No-op outside Lambda (``AWS_EXECUTION_ENV`` unset) or when ``DATABASE_URL`` is missing.
    """
    if not os.environ.get("AWS_EXECUTION_ENV"):
        return
    pool = get_pool()
    if pool is None:
        return
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        logger.warning("Lambda DB warm failed (first request will retry connect): %s", exc)
