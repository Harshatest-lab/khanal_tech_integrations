"""Tests for filtered order totals on the dashboard orders API."""

from unittest.mock import MagicMock, patch

from walmart.dashboard_api import _order_filter_clauses, fetch_orders


def test_order_filter_clauses_includes_order_date_range() -> None:
    clauses, params = _order_filter_clauses(
        order_date_from="2026-08-01",
        order_date_to="2026-08-31",
    )
    assert any("h.order_date_ms >=" in c for c in clauses)
    assert any("h.order_date_ms <" in c for c in clauses)
    assert len(params) == 2


def test_fetch_orders_returns_total_lines(monkeypatch) -> None:
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.side_effect = [(42,), (806,)]
    cursor.description = [("purchase_order_id",)]
    cursor.fetchall.return_value = []

    monkeypatch.setattr(
        "walmart.dashboard_api._group_rows_by_po",
        lambda rows: rows,
    )

    result = fetch_orders(conn, limit=10, offset=0, order_date_from="2026-08-01")

    assert result["total"] == 42
    assert result["total_lines"] == 806
    assert cursor.execute.call_count == 3
