"""Structured logging helpers for Lambda."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging for Lambda and local runs.

    AWS Lambda pre-installs a handler on the root logger. An early return when
    handlers exist left the default WARNING level, so INFO ``log_json`` lines never
    reached CloudWatch.
    """
    lvl = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(lvl)
    if not root.handlers:
        logging.basicConfig(
            level=lvl,
            stream=sys.stdout,
            format="%(message)s",
            force=True,
        )
    for handler in root.handlers:
        handler.setLevel(lvl)


def log_json(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    payload = {"message": message, **fields}
    logger.log(level, json.dumps(payload, default=str))
