"""AMZ_UK_POSTING_CUTOFF_DATE gate on the unattended credit-note posting run."""
import pytest

from amazon_uk import post_credit_notes_to_sap, store


def test_cutoff_is_required(monkeypatch):
    monkeypatch.delenv("AMZ_UK_POSTING_CUTOFF_DATE", raising=False)
    with pytest.raises(RuntimeError, match="AMZ_UK_POSTING_CUTOFF_DATE"):
        post_credit_notes_to_sap.posting_cutoff()


@pytest.mark.parametrize("bad", ["2026-13-45", "01/09/2026", "yesterday", "2026-9-1x"])
def test_cutoff_must_be_iso_date(monkeypatch, bad):
    monkeypatch.setenv("AMZ_UK_POSTING_CUTOFF_DATE", bad)
    with pytest.raises(RuntimeError, match="YYYY-MM-DD"):
        post_credit_notes_to_sap.posting_cutoff()


def test_cutoff_valid(monkeypatch):
    monkeypatch.setenv("AMZ_UK_POSTING_CUTOFF_DATE", "2026-09-01")
    assert post_credit_notes_to_sap.posting_cutoff() == "2026-09-01"


@pytest.mark.parametrize("fn", ["post_all_refunds", "retry_failed_refunds"])
def test_unset_cutoff_stops_run_before_any_db_or_sap_call(monkeypatch, fn):
    monkeypatch.delenv("AMZ_UK_POSTING_CUTOFF_DATE", raising=False)

    def boom(*a, **k):
        raise AssertionError("must not touch DB / SAP when the cutoff is missing")

    monkeypatch.setattr(store, "connect", boom)
    monkeypatch.setattr(post_credit_notes_to_sap.sap_client, "get_invoice", boom)
    with pytest.raises(RuntimeError, match="AMZ_UK_POSTING_CUTOFF_DATE"):
        getattr(post_credit_notes_to_sap, fn)()


class _Cur:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.update(sql=sql, params=params)

    def fetchall(self):
        return [("txn-1",), ("txn-2",)]

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


def test_candidate_query_filters_on_posted_date_cutoff():
    sink = {}
    conn = _Conn(sink)
    assert post_credit_notes_to_sap._refunds_not_posted_all(conn, "2026-09-01") == ["txn-1", "txn-2"]
    assert "re.posted_date::date >= %(cutoff)s" in sink["sql"]
    assert "transaction_status = 'RELEASED'" in sink["sql"]
    assert sink["params"] == {"cutoff": "2026-09-01"}


def test_run_passes_validated_cutoff_to_query(monkeypatch):
    monkeypatch.setenv("AMZ_UK_POSTING_CUTOFF_DATE", "2026-09-01")
    seen = {}
    monkeypatch.setattr(store, "connect", lambda: _Conn({}))
    monkeypatch.setattr(post_credit_notes_to_sap, "get_sku_mapping", lambda: {})
    monkeypatch.setattr(post_credit_notes_to_sap, "_refunds_not_posted_all",
                         lambda conn, c: seen.update(c=c) or [])
    monkeypatch.setattr(post_credit_notes_to_sap, "_post_refund_ids", lambda conn, ids, sku_map: {"total": len(ids)})
    assert post_credit_notes_to_sap.post_all_refunds() == {"total": 0} and seen["c"] == "2026-09-01"
