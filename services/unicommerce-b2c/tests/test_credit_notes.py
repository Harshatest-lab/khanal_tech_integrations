"""Tests for credit_notes.py's idempotency guard: a failed duplicate
check must propagate (not fall through to a blind post) so the caller's
mark_cn_failed retry path picks it up next cycle -- there is no
dedicated 'verify' poll on the CN side, unlike post_invoices.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import credit_notes, sap_client  # noqa: E402


def test_post_credit_note_raises_when_duplicate_check_fails(monkeypatch):
    def fake_find(uc_return_id, base_doc_entry=None):
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
