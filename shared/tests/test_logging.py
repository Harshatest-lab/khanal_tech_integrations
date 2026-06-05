"""Logging configuration (Lambda pre-installed handlers)."""

from __future__ import annotations

import io
import logging

from khanal_shared.logging import configure_logging, log_json


def test_configure_logging_emits_info_when_root_already_has_handler() -> None:
    """Mimics AWS Lambda: root logger already has a handler before configure_logging."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    stream = io.StringIO()
    root.addHandler(logging.StreamHandler(stream))
    root.handlers[0].setLevel(logging.WARNING)
    root.setLevel(logging.WARNING)

    configure_logging("INFO")
    log = logging.getLogger("test.lambda_mimic")
    log_json(log, logging.INFO, "hello_event", key="value")

    out = stream.getvalue()
    assert "hello_event" in out
    assert "key" in out

    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.WARNING)
