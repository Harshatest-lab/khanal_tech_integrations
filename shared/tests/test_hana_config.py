import pytest

from khanal_shared.hana.config import get_hana_params, hana_enabled


@pytest.fixture(autouse=True)
def _clear_hana_cache() -> None:
    get_hana_params.cache_clear()
    yield
    get_hana_params.cache_clear()


def test_hana_enabled_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HANA_USE_SSM", raising=False)
    monkeypatch.setenv("HANA_HOST", "103.69.202.108")
    monkeypatch.setenv("HANA_PORT", "30015")
    monkeypatch.setenv("HANA_DATABASE", "NDB")
    monkeypatch.setenv("HANA_SCHEMA", "SBO_COMPANY")
    monkeypatch.setenv("HANA_USER", "readonly_user")
    monkeypatch.setenv("HANA_PASSWORD", "secret")

    params = get_hana_params()
    assert params is not None
    assert params.host == "103.69.202.108"
    assert params.port == 30015
    assert params.database == "NDB"
    assert params.schema == "SBO_COMPANY"
    assert params.user == "readonly_user"
    assert params.password == "secret"
    assert hana_enabled() is True


def test_hana_disabled_when_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HANA_USE_SSM", raising=False)
    for key in ("HANA_HOST", "HANA_USER", "HANA_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    assert get_hana_params() is None
    assert hana_enabled() is False
