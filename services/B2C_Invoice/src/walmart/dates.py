"""US Pacific date helpers for Walmart order/ship dates and SAP DocDate/TaxDate."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


def ms_to_pacific_date(ms: int) -> date:
    """Calendar date in America/Los_Angeles for a Walmart epoch-ms timestamp."""
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone(PACIFIC)
    return dt.date()


def ms_to_pacific_date_str(ms: int) -> str:
    """YYYY-MM-DD in US Pacific (for SAP DocDate / TaxDate)."""
    return ms_to_pacific_date(ms).isoformat()
