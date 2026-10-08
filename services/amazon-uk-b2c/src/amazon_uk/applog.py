"""Shared logger — writes every line to amazonuk.sync_log in RDS (not a
local file: this pipeline's storage is Postgres/RDS).

Every line also goes to stdout, so a manual run still shows live progress
and a cron redirect to a file still works if wanted — that's just stdout
capture, not this module's job.

Logger setup is idempotent — repeated get_logger("amz_sync") calls (sync
entrypoint, fetch pagination, posting) all share one handler.
"""

import logging
import sys

import psycopg

from amazon_uk import credentials

_FORMAT = "%(asctime)s [%(name)s] %(message)s"
_configured = False


class _RdsLogHandler(logging.Handler):
    """Inserts each log record into amazonuk.sync_log. One connection is
    opened lazily and reused for the handler's whole lifetime — NOT
    reopened per emit() (confirmed live 2026-09-17: a 1099-order batch's
    per-order result logging opened 1000+ separate Postgres connections
    just to write log lines, real risk of hitting RDS's connection limit
    on a large run). Reconnects automatically if the cached connection has
    gone stale/closed. Failures here must never crash the pipeline (a DB
    hiccup logging a message is not worth losing the actual sync/post run
    over) — swallowed via handleError after one reconnect attempt."""

    def __init__(self):
        super().__init__()
        self._conn = None

    def _get_conn(self):
        if self._conn is None or self._conn.closed:
            # autocommit: each log line is its own statement, and psycopg 3's
            # "with conn:" would CLOSE the cached connection on exit.
            db = credentials.get_db_credentials()
            self._conn = psycopg.connect(
                host=db.host,
                port=db.port,
                dbname=db.dbname,
                user=db.user,
                password=db.password,
                connect_timeout=15,
                autocommit=True,
            )
        return self._conn

    def emit(self, record: logging.LogRecord) -> None:
        for attempt in range(2):  # one retry with a fresh connection if the cached one is stale
            try:
                conn = self._get_conn()
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO amazonuk.sync_log (level, message) VALUES (%s, %s)",
                        (record.levelname, record.getMessage()),
                    )
                return
            except Exception:
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                if attempt == 1:
                    self.handleError(record)


def get_logger(name: str = "amz_sync") -> logging.Logger:
    global _configured
    if not _configured:
        root_name = __name__.split(".")[0]
        root = logging.getLogger(root_name)
        root.setLevel(logging.INFO)
        root.propagate = False

        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(stdout_handler)
        # No formatter on the RDS handler — it stores level/message as
        # separate columns (created_at is its own timestamptz column), so
        # baking the "%(asctime)s [%(name)s]" prefix into the message text
        # would just duplicate what the row's own columns already give you.
        root.addHandler(_RdsLogHandler())

        _configured = True
    return logging.getLogger(f"{__name__.split('.')[0]}.{name}")
