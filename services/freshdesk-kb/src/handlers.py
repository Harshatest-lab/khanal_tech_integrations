"""Lambda entrypoint. Thin: hands the API Gateway event to freshdesk_kb.api."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from khanal_shared.config import get_settings  # noqa: E402
from khanal_shared.logging import configure_logging  # noqa: E402

from freshdesk_kb import api  # noqa: E402


def kb_handler(event, context):
    configure_logging(get_settings().log_level)
    return api.handle(event)
