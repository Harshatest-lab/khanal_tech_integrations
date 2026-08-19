"""Unit tests for SAP SSM session helpers (no boto3 / network)."""

from __future__ import annotations

from datetime import datetime, timezone

from khanal_shared.sap_sl.ssm_session import (
    _LOCK_WAIT_SEC,
    _MAX_LOCK_WAITS,
    _STALE_LOCK_SEC,
    _host_from_base,
    _parse_expires,
)


def test_parse_expires_iso_z() -> None:
    dt = _parse_expires("2026-06-03T12:00:00Z")
    assert dt is not None
    assert dt == datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_expires_naive_utc() -> None:
    dt = _parse_expires("2026-06-03 12:00:00")
    assert dt is not None
    assert dt.tzinfo == timezone.utc


def test_host_from_base() -> None:
    assert _host_from_base("https://103.69.202.108:50000/b1s/v1/") == "103.69.202.108"


def test_lock_wait_budget_clears_stale_threshold() -> None:
    """A waiter must be able to survive long enough to see a lock go stale.

    Otherwise the abandoned-lock steal branch in _resolve_session_tokens is
    dead code: every waiter gives up ("exceeded max waits") before any
    single check ever observes lock_age > _STALE_LOCK_SEC. Confirmed live
    2026-08-19: is_locked stuck for 3+ hours with zero renew attempts ever
    logged, because _MAX_LOCK_WAITS * _LOCK_WAIT_SEC (60s) was less than
    _STALE_LOCK_SEC (150s).
    """
    total_wait_budget = _MAX_LOCK_WAITS * _LOCK_WAIT_SEC
    assert total_wait_budget > _STALE_LOCK_SEC, (
        f"wait budget ({total_wait_budget}s) must exceed the stale-lock "
        f"threshold ({_STALE_LOCK_SEC}s), or a genuinely abandoned lock "
        "can never be stolen"
    )
