"""tax_code_correction: every guard must refuse BEFORE the SAP cancel, and the
apply path must stop on a wrong cancel series. All SAP/HANA/store calls are
monkeypatched."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import geo, hana_access, post_invoices, sap_client, store, tax_code_correction as tcc  # noqa: E402


def _live(**over):
    doc = {
        "Cancelled": "tNO", "DocumentStatus": "bost_Open", "PaidToDate": 0, "DocDate": "2026-10-06", "NumAtCard": "UC1",
        "VatSum": 10.0, "DocTotal": 110.0, "Series": 499, "DocNum": 7001,
        "DocumentLines": [{"TaxCode": "KAIG18"}, {"TaxCode": "KAIG18"}],
    }
    doc.update(over)
    return doc


def _setup(monkeypatch, live=None, rebuild=None, cn=None, cancel_series=500):
    calls = {"cancel": 0, "reset": 0, "post": 0}
    row = {
        "uniware_id": "UC1", "channel_name": "DEL4", "pipeline_status": "posted", "doc_entry": "100",
        "order_date": "2026-10-06", "order_json": "{}",
    }
    state = {"row": row}
    monkeypatch.setattr(tcc, "_get_row", lambda conn, uid: state["row"])
    monkeypatch.setattr(tcc, "get_settings", lambda force_reload=False: object())
    monkeypatch.setattr(geo, "resolve_origin_state_code", lambda ch, s: "HR")
    lives = {100: live or _live(), 200: _live(DocumentLines=[{"TaxCode": "HRIG18"}], DocNum=7002, Series=499)}
    monkeypatch.setattr(sap_client, "get_invoice_by_doc_entry", lambda de: lives[int(de)])
    monkeypatch.setattr(hana_access, "find_credit_note_by_base_invoice", lambda de: cn)
    monkeypatch.setattr(tcc, "_rebuild", lambda r, l, s: rebuild or ({"HRIG18"}, 10.02))

    def cancel(de):
        calls["cancel"] += 1

    monkeypatch.setattr(sap_client, "cancel_invoice", cancel)
    monkeypatch.setattr(
        hana_access, "find_cancellation_document",
        lambda n, de: {"doc_entry": 150, "doc_num": 9, "series": cancel_series, "series_name": "X", "doc_date": "2026-10-13"},
    )

    def reset(conn, uid, de):
        calls["reset"] += 1
        return True

    def post(conn, uid):
        calls["post"] += 1
        state["row"] = {**row, "doc_entry": "200"}
        return {"status": "posted"}

    monkeypatch.setattr(store, "reset_posted_order_for_repost", reset)
    monkeypatch.setattr(post_invoices, "post_one_order", post)
    return calls


def test_dry_run_touches_nothing(monkeypatch):
    calls = _setup(monkeypatch)
    res = tcc.correct_one(None, "UC1", 100, "2026-10", apply=False)
    assert res["status"] == "dry_run_would_correct"
    assert res["new_codes"] == ["HRIG18"]
    assert calls == {"cancel": 0, "reset": 0, "post": 0}


def test_guards_refuse_before_cancel(monkeypatch):
    cases = [
        ({"live": _live(Cancelled="tYES")}, "skipped_already_cancelled"),
        ({"live": _live(DocumentStatus="bost_Close")}, "skipped_not_open"),
        ({"live": _live(PaidToDate=50)}, "skipped_paid"),
        ({"live": _live(DocDate="2026-09-30")}, "skipped_wrong_month"),
        ({"live": _live(NumAtCard="OTHER")}, "skipped_numatcard_mismatch"),
        ({"live": _live(DocumentLines=[{"TaxCode": "HRIG18"}])}, "skipped_not_all_ka"),
        ({"cn": {"doc_entry": 1}}, "skipped_has_credit_note"),
        ({"rebuild": ({"KAIG18"}, 10.0)}, "skipped_rebuild_not_state_coded"),
        ({"rebuild": ({"HRIG18"}, 11.0)}, "skipped_rebuild_tax_mismatch"),
    ]
    for kwargs, expected in cases:
        calls = _setup(monkeypatch, **kwargs)
        res = tcc.correct_one(None, "UC1", 100, "2026-10", apply=True)
        assert res["status"] == expected, (expected, res)
        assert calls["cancel"] == 0, expected


def test_local_state_mismatch_skips(monkeypatch):
    calls = _setup(monkeypatch)
    res = tcc.correct_one(None, "UC1", 999, "2026-10", apply=True)
    assert res["status"] == "skipped_local_state_mismatch"
    assert calls["cancel"] == 0


def test_ka_origin_channel_skips(monkeypatch):
    calls = _setup(monkeypatch)
    monkeypatch.setattr(geo, "resolve_origin_state_code", lambda ch, s: "KA")
    assert tcc.correct_one(None, "UC1", 100, "2026-10", apply=True)["status"] == "skipped_ka_origin_channel"
    assert calls["cancel"] == 0


def test_apply_happy_path(monkeypatch):
    calls = _setup(monkeypatch)
    res = tcc.correct_one(None, "UC1", 100, "2026-10", apply=True)
    assert res["status"] == "corrected", res
    assert res["new_doc_entry"] == 200 and res["cancel_series_ok"] is True
    assert calls == {"cancel": 1, "reset": 1, "post": 1}


def test_wrong_cancel_series_flagged(monkeypatch):
    _setup(monkeypatch, cancel_series=499)
    res = tcc.correct_one(None, "UC1", 100, "2026-10", apply=True)
    assert res["status"] == "corrected_but_cancel_series_differs"
    assert res["cancel_series_ok"] is False


def test_run_stops_on_anomaly_and_limit(monkeypatch):
    _setup(monkeypatch, cancel_series=499)
    items = [{"uniware_id": "UC1", "doc_entry": 100}, {"uniware_id": "UC1", "doc_entry": 100}]
    out = tcc.run(None, items, "2026-10", apply=True, limit=5)
    assert out["processed"] == 1 and out["stopped_on_anomaly"] is not None
    assert len(out["results"]) == 1


def test_run_default_limit_one(monkeypatch):
    _setup(monkeypatch)
    items = [{"uniware_id": "UC1", "doc_entry": 100}, {"uniware_id": "UC1", "doc_entry": 100}]
    out = tcc.run(None, items, "2026-10", apply=False)
    assert out["processed"] == 1 and len(out["results"]) == 1
