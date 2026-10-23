"""Tests for credit_notes.py's idempotency guard and posting-cutoff
wiring. Guard: a failed duplicate check must propagate (not fall through
to a blind post) so the caller's mark_cn_failed retry path picks it up
next cycle -- there is no dedicated 'verify' poll on the CN side, unlike
post_invoices.py. Cutoff: post_credit_notes must always pass
constants.POSTING_CUTOFF_DATE into store.get_pending_credit_notes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import constants, credit_notes, sap_client  # noqa: E402


def test_post_credit_note_raises_when_duplicate_check_fails(monkeypatch):
    def fake_find(uc_return_id, base_doc_entry=None, base_lines=None):
        raise sap_client.SapLookupError("SAP unreachable")

    def fail_if_called(*a, **k):
        raise AssertionError("must not proceed to post when the duplicate check failed")

    monkeypatch.setattr(credit_notes.store, "get_cn", lambda conn, uc_return_id: {"uc_order_id": None})
    monkeypatch.setattr(credit_notes.sap_client, "find_credit_note_by_return_id", fake_find)
    monkeypatch.setattr(credit_notes.store, "mark_cn_posted", fail_if_called)
    monkeypatch.setattr(credit_notes.sap_payload, "credit_note_payload", fail_if_called)
    monkeypatch.setattr(credit_notes.sap_client, "post_credit_note_with_series_fallback", fail_if_called)

    with pytest.raises(sap_client.SapLookupError):
        credit_notes._post_credit_note(conn=None, uc_return_id="R-1", settings=object())


def _stop_after_guard(monkeypatch, seen):
    """Run _post_credit_note only as far as the duplicate guard."""

    def fake_find(uc_return_id, base_doc_entry=None, base_lines=None):
        seen.update(base_doc_entry=base_doc_entry, base_lines=base_lines)
        return {"doc_entry": 9, "doc_num": 9}

    monkeypatch.setattr(credit_notes.sap_client, "find_credit_note_by_return_id", fake_find)
    monkeypatch.setattr(credit_notes.store, "mark_cn_posted", lambda *a, **k: None)


def test_duplicate_guard_is_scoped_to_the_invoice_lines_this_return_reverses(monkeypatch):
    """Two returns of different lines on one invoice both link to the same
    invoice. Matching on the invoice alone made the second return look
    already posted, so it was marked posted with the first CN's DocEntry."""
    seen = {}
    _stop_after_guard(monkeypatch, seen)
    cn = {"uc_order_id": "O1", "items_json": [{"sapBaseLine": 3}, {"sapBaseLine": 1}, {"sapBaseLine": 3}]}
    monkeypatch.setattr(credit_notes.store, "get_cn", lambda conn, rid: cn)
    monkeypatch.setattr(credit_notes.store, "get_order", lambda conn, oid: {"sap_ar_invoice_docentry": 500})

    credit_notes._post_credit_note(conn=None, uc_return_id="R-2", settings=object())

    assert seen == {"base_doc_entry": 500, "base_lines": [1, 3]}


def test_duplicate_guard_reads_items_json_stored_as_a_string(monkeypatch):
    seen = {}
    _stop_after_guard(monkeypatch, seen)
    cn = {"uc_order_id": "O1", "items_json": '[{"sapBaseLine": 0}]'}
    monkeypatch.setattr(credit_notes.store, "get_cn", lambda conn, rid: cn)
    monkeypatch.setattr(credit_notes.store, "get_order", lambda conn, oid: {"sap_ar_invoice_docentry": 500})

    credit_notes._post_credit_note(conn=None, uc_return_id="R-3", settings=object())

    assert seen["base_lines"] == [0]


def test_duplicate_guard_falls_back_to_invoice_only_when_no_line_is_resolved(monkeypatch):
    seen = {}
    _stop_after_guard(monkeypatch, seen)
    cn = {"uc_order_id": "O1", "items_json": [{"itemSku": "X"}]}
    monkeypatch.setattr(credit_notes.store, "get_cn", lambda conn, rid: cn)
    monkeypatch.setattr(credit_notes.store, "get_order", lambda conn, oid: {"sap_ar_invoice_docentry": 500})

    credit_notes._post_credit_note(conn=None, uc_return_id="R-4", settings=object())

    assert seen["base_lines"] is None


def test_post_credit_notes_passes_posting_cutoff_to_get_pending_credit_notes(monkeypatch):
    calls = {}

    monkeypatch.setattr(credit_notes, "get_settings", lambda **kw: object())
    monkeypatch.setattr(credit_notes.sap_client, "validate_active_series", lambda settings: (True, "ok"))
    monkeypatch.setattr(credit_notes.store, "start_sync_log", lambda *a, **k: "run-1")
    monkeypatch.setattr(credit_notes.store, "finish_sync_log", lambda *a, **k: None)

    def fake_get_pending_credit_notes(conn, limit, cutoff_date, **kw):
        calls["cutoff_date"] = cutoff_date
        return []

    monkeypatch.setattr(credit_notes.store, "get_pending_credit_notes", fake_get_pending_credit_notes)

    credit_notes.post_credit_notes(conn=None, limit=10)

    assert calls["cutoff_date"] == constants.POSTING_CUTOFF_DATE
