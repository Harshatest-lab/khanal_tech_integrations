"""Cursor-advancement logic ported from
khanal_tech_integrations/unicommerce/workers/fetch.py's _next_cursor.

Advances to the *earliest deferred* timestamp (minus overlap), not "now" --
a detail fetch that failed this run must still be re-tried next run,
not silently skipped by an overly-optimistic cursor advance."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce.fetch_sync import ORDER_CURSOR_OVERLAP_MINUTES, _next_cursor  # noqa: E402


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def test_next_cursor_uses_earliest_deferred_when_present():
    since = "2026-07-01 00:00:00"
    cycle_ended = _dt("2026-07-01 12:00:00")
    seen = [_dt("2026-07-01 10:00:00"), _dt("2026-07-01 11:00:00")]
    deferred = [_dt("2026-07-01 05:00:00"), _dt("2026-07-01 06:00:00")]

    result = _next_cursor(since, cycle_ended, seen, deferred)

    expected = (_dt("2026-07-01 05:00:00") - timedelta(minutes=ORDER_CURSOR_OVERLAP_MINUTES)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert result == expected


def test_next_cursor_uses_max_seen_when_nothing_deferred():
    since = "2026-07-01 00:00:00"
    cycle_ended = _dt("2026-07-01 12:00:00")
    seen = [_dt("2026-07-01 10:00:00"), _dt("2026-07-01 11:00:00")]

    result = _next_cursor(since, cycle_ended, seen, [])

    expected = (_dt("2026-07-01 11:00:00") - timedelta(minutes=ORDER_CURSOR_OVERLAP_MINUTES)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert result == expected


def test_next_cursor_uses_cycle_end_when_nothing_seen_or_deferred():
    since = "2026-07-01 00:00:00"
    cycle_ended = _dt("2026-07-01 12:00:00")

    result = _next_cursor(since, cycle_ended, [], [])

    expected = (cycle_ended - timedelta(minutes=ORDER_CURSOR_OVERLAP_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    assert result == expected


def test_next_cursor_never_goes_before_base_since():
    # Deferred timestamp is earlier than `since` itself -- must clamp to
    # `since`, not walk the cursor backwards past where it already was.
    since = "2026-07-01 09:00:00"
    cycle_ended = _dt("2026-07-01 12:00:00")
    deferred = [_dt("2026-07-01 05:00:00")]

    result = _next_cursor(since, cycle_ended, [], deferred)

    assert result == since
