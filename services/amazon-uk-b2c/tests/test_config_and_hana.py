import pytest

from amazon_uk import config


def test_get_returns_env_value(monkeypatch):
    monkeypatch.setenv("TEST_KEY_XYZ", "hello")
    assert config.get("TEST_KEY_XYZ") == "hello"


def test_get_default_when_missing(monkeypatch):
    monkeypatch.delenv("TEST_MISSING_KEY", raising=False)
    assert config.get("TEST_MISSING_KEY", "fallback") == "fallback"


def test_get_raises_when_missing(monkeypatch):
    monkeypatch.delenv("TEST_MISSING_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TEST_MISSING_KEY"):
        config.get("TEST_MISSING_KEY")


def test_allocate_batches_fefo_allocation():
    from amazon_uk import hana_client

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return self._rows

    rows = [("B1", 5), ("B2", 10), ("B3", 0)]  # B3 zero-qty still listed
    allocated = hana_client.allocate_batches_fefo(FakeCursor(rows), "ITEM1", 7, "AMZ-FBA")
    assert allocated == [
        {"BatchNumber": "B1", "Quantity": 5.0},
        {"BatchNumber": "B2", "Quantity": 2.0},
    ]


def test_allocate_batches_fefo_insufficient_stock():
    from amazon_uk import hana_client

    class FakeCursor:
        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return [("B1", 3.0)]

    with pytest.raises(ValueError, match="Insufficient stock"):
        hana_client.allocate_batches_fefo(FakeCursor(), "ITEM1", 7, "AMZ-FBA")


def test_allocate_batches_fefo_not_batch_tracked():
    from amazon_uk import hana_client

    class FakeCursor:
        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return []

    assert hana_client.allocate_batches_fefo(FakeCursor(), "ITEM1", 5, "AMZ-FBA") == []


def test_schema_prefers_ssm_over_env(monkeypatch):
    from types import SimpleNamespace

    from amazon_uk import hana_client

    monkeypatch.setenv("HANA_SCHEMA", "KFL_UK")
    monkeypatch.setattr(
        hana_client, "get_hana_params", lambda: SimpleNamespace(schema="UK_TEST_16092026")
    )
    assert hana_client._schema() == "UK_TEST_16092026"


def test_schema_falls_back_to_env(monkeypatch):
    from amazon_uk import hana_client

    monkeypatch.setenv("HANA_SCHEMA", "FROM_ENV")
    monkeypatch.setattr(hana_client, "get_hana_params", lambda: None)
    assert hana_client._schema() == "FROM_ENV"
