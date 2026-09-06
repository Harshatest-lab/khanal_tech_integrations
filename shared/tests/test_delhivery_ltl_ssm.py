"""Tests for Delhivery LTL SSM / env resolution."""

from __future__ import annotations

from typing import Any

import pytest

from khanal_shared.delivery.delhivery_ltl_ssm import (
    PARAM_DELHIVERY_LTL_CONFIG,
    PARAM_DELHIVERY_LTL_CREDENTIALS,
    delhivery_ltl_use_ssm,
    resolve_delhivery_ltl_settings,
)


def _clear_ltl_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "DELHIVERY_LTL_USE_SSM",
        "DELHIVERY_LTL_TRACK_BASE_URL",
        "DELHIVERY_LTL_USERNAME",
        "DELHIVERY_LTL_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)


def test_delhivery_ltl_use_ssm_defaults_when_env_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ltl_env(monkeypatch)
    assert delhivery_ltl_use_ssm() is True


def test_delhivery_ltl_use_ssm_false_when_env_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ltl_env(monkeypatch)
    monkeypatch.setenv("DELHIVERY_LTL_TRACK_BASE_URL", "https://ltl.example.com")
    monkeypatch.setenv("DELHIVERY_LTL_USERNAME", "u")
    monkeypatch.setenv("DELHIVERY_LTL_PASSWORD", "p")
    assert delhivery_ltl_use_ssm() is False


def test_delhivery_ltl_use_ssm_explicit_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ltl_env(monkeypatch)
    monkeypatch.setenv("DELHIVERY_LTL_USE_SSM", "0")
    assert delhivery_ltl_use_ssm() is False


def test_resolve_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ltl_env(monkeypatch)
    monkeypatch.setenv("DELHIVERY_LTL_USE_SSM", "0")
    monkeypatch.setenv("DELHIVERY_LTL_TRACK_BASE_URL", "https://ltl.example.com/api/")
    monkeypatch.setenv("DELHIVERY_LTL_USERNAME", "user1")
    monkeypatch.setenv("DELHIVERY_LTL_PASSWORD", "pass1")

    base, user, password = resolve_delhivery_ltl_settings()
    assert base == "https://ltl.example.com/api"
    assert user == "user1"
    assert password == "pass1"


def test_resolve_from_ssm(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ltl_env(monkeypatch)
    monkeypatch.setenv("DELHIVERY_LTL_USE_SSM", "1")

    def fake_get(name: str) -> dict[str, Any]:
        if name == PARAM_DELHIVERY_LTL_CONFIG:
            return {"track_base_url": "https://ssm.example.com"}
        if name == PARAM_DELHIVERY_LTL_CREDENTIALS:
            return {"username": "ssm_user", "password": "ssm_pass"}
        return {}

    monkeypatch.setattr(
        "khanal_shared.delivery.delhivery_ltl_ssm._get_param_json",
        fake_get,
    )

    base, user, password = resolve_delhivery_ltl_settings()
    assert base == "https://ssm.example.com"
    assert user == "ssm_user"
    assert password == "ssm_pass"


def test_resolve_ssm_incomplete_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ltl_env(monkeypatch)
    monkeypatch.setenv("DELHIVERY_LTL_USE_SSM", "1")
    monkeypatch.setattr(
        "khanal_shared.delivery.delhivery_ltl_ssm._get_param_json",
        lambda _name: {},
    )

    with pytest.raises(RuntimeError, match="Delhivery LTL SSM"):
        resolve_delhivery_ltl_settings()
