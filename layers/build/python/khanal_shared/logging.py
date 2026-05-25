"""Structured logging helpers for Lambda."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stdout,
        format="%(message)s",
    )


def log_json(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    payload = {"message": message, **fields}
    logger.log(level, json.dumps(payload, default=str))
