"""Unit tests for SAP SSM session helpers (no boto3 / network)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from khanal_shared.sap_sl import ssm_session
from khanal_shared.sap_sl.ssm_session import (
    _LOCK_WAIT_SEC,
    _MAX_LOCK_WAITS,
    _STALE_LOCK_SEC,
    _host_from_base,
    _parse_expires,
    _resolve_session_tokens,
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


def test_locked_with_no_locked_at_is_stolen_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """is_locked=True with no/unparseable locked_at must be stolen on the first check.

    A lock set before the ``locked_at`` field existed (or by any write that
    dropped it) has no liveness signal at all -- that must never be read as
    "provably recent, so wait." Confirmed live 2026-08-19: a pre-locked_at
    zombie lock from the original 2026-08-18 incident survived both the TTL
    fix (#69) and a wait-budget fix untouched, because ``lock_age`` was
    always None and the old `lock_age is not None and lock_age > threshold`
    check can never be true for it -- every caller just waited out its full
    budget and gave up, forever.
    """
    session_data = {"is_locked": True, "b1session": "", "routeid": "", "expires_on": ""}

    def fake_get_param_json(name: str) -> dict:
        return dict(session_data)

    def fake_put_param_json(name: str, data: dict) -> None:
        session_data.update(data)

    def fake_read_sl_and_credentials() -> tuple[str, str, str, str]:
        return "https://sap.example.com", "TESTDB", "user", "pass"

    def fake_renew_sap_session_ssm() -> tuple[object, str]:
        session_data.update(
            {
                "b1session": "SESSIONID",
                "routeid": "route",
                "expires_on": "2099-01-01T00:00:00Z",
                "is_locked": False,
                "locked_at": "",
            }
        )
        return object(), "https://sap.example.com"

    def fail_on_sleep(_seconds: float) -> None:
        raise AssertionError("waited on a lock with no locked_at instead of stealing it immediately")

    monkeypatch.setattr(ssm_session, "_get_param_json", fake_get_param_json)
    monkeypatch.setattr(ssm_session, "_put_param_json", fake_put_param_json)
    monkeypatch.setattr(ssm_session, "_read_sl_and_credentials", fake_read_sl_and_credentials)
    monkeypatch.setattr(ssm_session, "renew_sap_session_ssm", fake_renew_sap_session_ssm)
    monkeypatch.setattr(ssm_session.time, "sleep", fail_on_sleep)

    base, b1, route = _resolve_session_tokens()

    assert base == "https://sap.example.com"
    assert b1 == "SESSIONID"
    assert route == "route"
