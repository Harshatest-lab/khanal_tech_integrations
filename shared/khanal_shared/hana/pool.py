"""Lazy SAP HANA connection pool suitable for Lambda warm starts.

Requires optional dependency: hdbcli — add to the Lambda layer build when HANA is used.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from queue import Empty, Queue
from typing import TYPE_CHECKING, Any, Iterator

from khanal_shared.hana.config import HanaConnectionParams, get_hana_params, quote_hana_identifier

if TYPE_CHECKING:
    from hdbcli import dbapi

logger = logging.getLogger(__name__)
_pool: "HANAConnectionPool | None" = None


class PoolableConnection:
    """Wraps hdbcli connection; ``close()`` returns the connection to the pool."""

    def __init__(self, connection: Any, pool: "HANAConnectionPool") -> None:
        self._conn = connection
        self._pool = pool
        self._returned = False

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        return self._conn.cursor(*args, **kwargs)

    def close(self) -> None:
        if self._returned:
            return
        self._returned = True
        self._pool.release_connection(self._conn)

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._conn, attr)

    def __enter__(self) -> "PoolableConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class HANAConnectionPool:
    def __init__(self, params: HanaConnectionParams) -> None:
        self._params = params
        self._queue: Queue[PoolableConnection | Any] = Queue(maxsize=params.max_pool_size)
        for _ in range(params.max_pool_size):
            self._queue.put(self._wrap(self._create_connection()))

    def _wrap(self, conn: Any) -> PoolableConnection:
        return PoolableConnection(conn, self)

    def _create_connection(self) -> Any:
        from hdbcli import dbapi

        kwargs: dict[str, Any] = {
            "address": self._params.host,
            "port": self._params.port,
            "user": self._params.user,
            "password": self._params.password,
        }
        if self._params.database:
            kwargs["databaseName"] = self._params.database
        conn = dbapi.connect(**kwargs)
        if self._params.schema:
            cur = conn.cursor()
            try:
                cur.execute(f"SET SCHEMA {quote_hana_identifier(self._params.schema)}")
            finally:
                cur.close()
        return conn

    def get_connection(self) -> PoolableConnection:
        try:
            conn = self._queue.get_nowait()
        except Empty:
            return self._wrap(self._create_connection())
        if isinstance(conn, PoolableConnection):
            return conn
        return self._wrap(conn)

    def release_connection(self, conn: Any) -> None:
        try:
            if conn.isconnected():
                self._queue.put(self._wrap(conn))
            else:
                self._queue.put(self._wrap(self._create_connection()))
        except Exception:
            logger.warning("HANA pool: replacing bad connection", exc_info=True)
            try:
                self._queue.put(self._wrap(self._create_connection()))
            except Exception:
                pass

    def close_all(self) -> None:
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                raw = item._conn if isinstance(item, PoolableConnection) else item
                if raw.isconnected():
                    raw.close()
            except Empty:
                break
            except Exception:
                logger.warning("HANA pool: error closing connection", exc_info=True)


def get_pool() -> HANAConnectionPool | None:
    """Return a module-scoped pool, or None if HANA is not configured."""
    global _pool
    params = get_hana_params()
    if params is None:
        logger.warning("HANA not configured; pool disabled")
        return None

    if _pool is not None:
        return _pool

    try:
        from hdbcli import dbapi  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "hdbcli not installed; add hdbcli to the Lambda layer (see scripts/build_shared_layer.sh)"
        ) from exc

    _pool = HANAConnectionPool(params)
    return _pool


@contextmanager
def hana_connection() -> Iterator[PoolableConnection]:
    """Check out a pooled HANA connection; ``close()`` returns it to the pool."""
    pool = get_pool()
    if pool is None:
        raise RuntimeError("HANA not configured (set HANA_HOST/HANA_USER/HANA_PASSWORD or SSM)")
    conn = pool.get_connection()
    try:
        yield conn
    finally:
        conn.close()


def ensure_warmed_for_lambda() -> None:
    """Prime the pool during Lambda INIT (no-op outside Lambda or when HANA is unset)."""
    if not os.environ.get("AWS_EXECUTION_ENV"):
        return
    pool = get_pool()
    if pool is None:
        return
    try:
        with hana_connection():
            pass
    except Exception as exc:
        logger.warning("Lambda HANA warm failed (first request will retry connect): %s", exc)
