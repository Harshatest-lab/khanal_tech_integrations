"""SAP invoice sync retry limits."""

from __future__ import annotations

import os

from walmart.sap_invoice_sync import (
    DEFAULT_MAX_SYNC_ATTEMPTS,
    FETCH_FAILED_FOR_RETRY_BASE,
    _fetch_failed_for_retry_sql,
    _max_sync_attempts,
)


def test_default_max_sync_attempts():
    assert DEFAULT_MAX_SYNC_ATTEMPTS == 3


def test_max_sync_attempts_from_env(monkeypatch):
    monkeypatch.setenv("SAP_SYNC_MAX_ATTEMPTS", "5")
    assert _max_sync_attempts() == 5
    monkeypatch.delenv("SAP_SYNC_MAX_ATTEMPTS")
    assert _max_sync_attempts() == 3


def test_failed_retry_query_excludes_max_attempts():
    assert "attempt_count" in FETCH_FAILED_FOR_RETRY_BASE
    assert "max_attempts" in FETCH_FAILED_FOR_RETRY_BASE

    sql, params = _fetch_failed_for_retry_sql(None, max_attempts=3)
    assert params["max_attempts"] == 3
    assert "LIMIT %(limit)s" in sql
