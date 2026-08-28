"""Small pure helpers — kept separate so they're trivially unit-testable."""

from __future__ import annotations

from datetime import datetime

from .config import DATE_FORMATS
from .exceptions import SafexpressError


class SafexpressDateParsingError(SafexpressError):
    pass


def parse_delivery_date(date_string: str) -> str:
    """Returns YYYY-MM-DD. Tries every format SafeExpress is known to send."""
    if not date_string or not date_string.strip():
        raise SafexpressDateParsingError("Empty date string provided")
    s = date_string.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise SafexpressDateParsingError(f"Could not parse date {s!r} with any known format")
