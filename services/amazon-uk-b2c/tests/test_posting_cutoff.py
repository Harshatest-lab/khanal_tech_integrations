"""AMZ_UK_POSTING_CUTOFF_DATE gate on the unattended posting run."""
import pytest

from amazon_uk import post_to_sap, store


def test_cutoff_is_required(monkeypatch):
    monkeypatch.delenv("AMZ_UK_POSTING_CUTOFF_DATE", raising=False)
    with pytest.raises(RuntimeError, match="AMZ_UK_POSTING_CUTOFF_DATE"):
        post_to_sap.posting_cutoff()


@pytest.mark.parametrize("bad", ["2026-13-45", "01/09/2026", "yesterday", "2026-9-1x"])
def test_cutoff_must_be_iso_date(monkeypatch, bad):
    monkeypatch.setenv("AMZ_UK_POSTING_CUTOFF_DATE", bad)
    with pytest.raises(RuntimeError, match="YYYY-MM-DD"):
        post_to_sap.posting_cutoff()


def test_cutoff_valid(monkeypatch):
    monkeypatch.setenv("AMZ_UK_POSTING_CUTOFF_DATE", "2026-09-01")
    assert post_to_sap.posting_cutoff() == "2026-09-01"


@pytest.mark.parametrize("fn", ["post_all_shipped_orders", "retry_failed_orders"])
def test_unset_cutoff_stops_run_before_any_db_or_sap_call(monkeypatch, fn):
    monkeypatch.delenv("AMZ_UK_POSTING_CUTOFF_DATE", raising=False)

    def boom(*a, **k):
        raise AssertionError("must not touch DB / HANA / SAP when the cutoff is missing")

    monkeypatch.setattr(store, "connect", boom)
    monkeypatch.setattr(post_to_sap.hana_client, "open_connection", boom)
    monkeypatch.setattr(post_to_sap.sap_client, "find_invoice_by_order_id", boom)
    with pytest.raises(RuntimeError, match="AMZ_UK_POSTING_CUTOFF_DATE"):
        getattr(post_to_sap, fn)()


class _Cur:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.update(sql=sql, params=params)

    def fetchall(self):
        return [("203-1",), ("203-2",)]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, sink):
        self.sink = sink

    def cursor(self):
        return _Cur(self.sink)

    def close(self):
        pass


def test_candidate_query_filters_on_ship_date_cutoff(monkeypatch):
    sink = {}
    monkeypatch.setattr(store, "connect", lambda: _Conn(sink))
    assert post_to_sap._shipped_orders_not_posted_all("2026-09-01") == ["203-1", "203-2"]
    assert "oi.shipment_date::date >= %(cutoff)s" in sink["sql"]
    assert "order_status = 'Shipped'" in sink["sql"]
    assert sink["params"] == {"cutoff": "2026-09-01"}


def test_retry_query_filters_on_cutoff(monkeypatch):
    sink = {}
    monkeypatch.setattr(store, "connect", lambda: _Conn(sink))
    post_to_sap._failed_order_ids("2026-09-01")
    assert "status = 'failed'" in sink["sql"] and "shipment_date::date >= %(cutoff)s" in sink["sql"]
    assert sink["params"] == {"cutoff": "2026-09-01"}


def test_run_passes_validated_cutoff_to_query(monkeypatch):
    monkeypatch.setenv("AMZ_UK_POSTING_CUTOFF_DATE", "2026-09-01")
    seen = {}
    monkeypatch.setattr(post_to_sap, "_shipped_orders_not_posted_all", lambda c: seen.update(c=c) or [])
    monkeypatch.setattr(post_to_sap, "_post_order_ids", lambda ids: {"total": len(ids)})
    assert post_to_sap.post_all_shipped_orders() == {"total": 0} and seen["c"] == "2026-09-01"
