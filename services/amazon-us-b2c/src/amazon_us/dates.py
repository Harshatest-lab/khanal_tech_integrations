"""Pacific-time helpers. SP-API timestamps are UTC; Amazon's MTR and
settlement reporting align to US/Pacific, so staged dates are Pacific."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_PACIFIC = ZoneInfo("America/Los_Angeles")


def utc_to_pacific(raw_utc: str) -> datetime | None:
    """SP-API UTC ISO timestamp -> naive Pacific datetime (MTR-aligned)."""
    if not raw_utc:
        return None
    dt = datetime.fromisoformat(str(raw_utc).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_PACIFIC).replace(tzinfo=None)


def parse_utc(raw_utc: str) -> datetime | None:
    """SP-API UTC ISO timestamp -> aware UTC datetime."""
    if not raw_utc:
        return None
    dt = datetime.fromisoformat(str(raw_utc).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def pacific_day_to_utc(day: str, end_of_day: bool = False) -> str:
    """'YYYY-MM-DD' Pacific day boundary -> UTC ISO string for SP-API params."""
    naive = datetime.fromisoformat(day)
    if end_of_day:
        naive = naive.replace(hour=23, minute=59, second=59)
    aware = naive.replace(tzinfo=_PACIFIC)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_to_pacific_date(raw: str | None) -> str | None:
    """UTC ISO 8601 (or bare date) -> Pacific calendar date 'YYYY-MM-DD'."""
    if not raw:
        return None
    s = str(raw).strip()
    try:
        if "T" in s:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
        else:
            dt = datetime.fromisoformat(s + "T00:00:00+00:00")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_PACIFIC).strftime("%Y-%m-%d")
    except ValueError:
        return None


def date_chunks(from_date: str, to_date: str, max_days: int = 30) -> list[tuple[str, str]]:
    """Split [from_date, to_date] (YYYY-MM-DD) into <= max_days chunks.
    SP-API report requests FATAL on windows longer than ~31 days."""
    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    chunks: list[tuple[str, str]] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=max_days - 1), end)
        chunks.append((cur.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        cur = chunk_end + timedelta(days=1)
    return chunks
