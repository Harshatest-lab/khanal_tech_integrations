"""Unit tests for SAP SSM session helpers (no boto3 / network)."""

from __future__ import annotations

from datetime import datetime, timezone

from khanal_shared.sap_sl.ssm_session import _host_from_base, _parse_expires


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
