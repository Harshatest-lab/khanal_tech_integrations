"""hana_access.find_credit_note_by_base_invoice must be able to scope the
duplicate check to specific invoice lines (RIN1.BaseLine), so a second
return against a different line of the same invoice is not mistaken for
the first return's credit note."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import hana_access  # noqa: E402


def _fake_hana(monkeypatch, row):
    executed = []

    class _Cursor:
        def execute(self, sql, params):
            executed.append((" ".join(sql.split()), params))

        def fetchone(self):
            return row

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cursor()

    @contextmanager
    def fake_connection():
        yield _Conn()

    monkeypatch.setattr(hana_access, "hana_connection", fake_connection)
    return executed


def test_without_base_lines_it_matches_on_the_invoice_alone(monkeypatch):
    executed = _fake_hana(monkeypatch, (12, 34, 1.5))

    result = hana_access.find_credit_note_by_base_invoice(500)

    sql, params = executed[0]
    assert result == {"doc_entry": 12, "doc_num": 34, "doc_tax_amount": 1.5}
    assert "BaseLine" not in sql
    assert params == [500]


def test_with_base_lines_it_only_matches_credit_notes_on_those_lines(monkeypatch):
    executed = _fake_hana(monkeypatch, None)

    result = hana_access.find_credit_note_by_base_invoice(500, base_lines=[1, 3])

    sql, params = executed[0]
    assert result is None
    assert 'l."BaseLine" IN (?, ?)' in sql
    assert params == [500, 1, 3]


def test_non_integer_base_lines_are_dropped_not_interpolated(monkeypatch):
    executed = _fake_hana(monkeypatch, None)

    hana_access.find_credit_note_by_base_invoice(500, base_lines=[2, "x'; DROP", None])

    sql, params = executed[0]
    assert "DROP" not in sql
    assert params == [500, 2]
