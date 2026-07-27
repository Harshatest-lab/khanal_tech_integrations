"""Tests for parse_source_datetime: it must always return an aware UTC
datetime (its own docstring's contract), even for UC ISO strings that
carry no offset -- a naive-vs-aware mismatch here crashed fetch_returns
live in production (_next_cursor's min()/comparison against an aware
base_since)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce.dates import parse_source_datetime  # noqa: E402


def test_naive_iso_string_becomes_aware_utc():
    dt = parse_source_datetime("2026-06-20T14:30:00")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timezone.utc.utcoffset(None)


def test_iso_string_with_z_is_aware_utc():
    dt = parse_source_datetime("2026-06-20T14:30:00Z")
    assert dt.tzinfo is not None
    assert dt == datetime(2026, 6, 20, 14, 30, 0, tzinfo=timezone.utc)


def test_iso_string_with_explicit_offset_is_preserved():
    dt = parse_source_datetime("2026-06-20T20:00:00+05:30")
    assert dt.tzinfo is not None
    assert dt.astimezone(timezone.utc) == datetime(2026, 6, 20, 14, 30, 0, tzinfo=timezone.utc)


def test_epoch_millis_is_aware_utc():
    dt = parse_source_datetime(1750000000000)
    assert dt.tzinfo is not None


def test_epoch_seconds_digit_string_is_aware_utc():
    dt = parse_source_datetime("1750000000")
    assert dt.tzinfo is not None


def test_none_and_empty_return_none():
    assert parse_source_datetime(None) is None
    assert parse_source_datetime("") is None


def test_naive_and_aware_results_are_comparable_without_crashing():
    # Reproduces the live crash: a naive-vs-aware TypeError in min()/</
    # comparisons once results from different source formats are mixed.
    naive_source = parse_source_datetime("2026-06-20T14:30:00")
    epoch_source = parse_source_datetime(1750000000000)
    base_since = datetime.now(timezone.utc)
    assert min(naive_source, epoch_source) is not None
    assert (naive_source - base_since) is not None
