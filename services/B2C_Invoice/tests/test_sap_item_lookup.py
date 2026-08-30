"""Tests for Walmart SKU → SAP ItemCode resolution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from walmart.sap_item_lookup import resolve_item_code


def _mock_conn(fetchone_results: list) -> MagicMock:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.side_effect = fetchone_results
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn


def test_resolve_item_code_prefers_alias_over_cache_and_hana() -> None:
    conn = _mock_conn([("FGDC0835",)])

    with patch("walmart.sap_item_lookup.lookup_item_by_wm_sku") as hana_lookup:
        item_code = resolve_item_code(conn, "Turmeric Medium")

    assert item_code == "FGDC0835"
    hana_lookup.assert_not_called()


def test_resolve_item_code_uses_sap_fg_items_when_alias_misses() -> None:
    conn = _mock_conn([None, ("FGDC0835", "DC_L_1P", "FBA_DC_TM_1P")])

    with patch("walmart.sap_item_lookup.lookup_item_by_wm_sku") as hana_lookup:
        item_code = resolve_item_code(conn, "FBA_DC_TM_1P")

    assert item_code == "FGDC0835"
    hana_lookup.assert_not_called()


def test_resolve_item_code_falls_back_to_hana() -> None:
    conn = _mock_conn([None, None])

    with patch(
        "walmart.sap_item_lookup.lookup_item_by_wm_sku",
        return_value={"item_code": "FGDC0999", "wm_sku": "NEW-SKU", "amz_sku": None},
    ), patch("walmart.sap_item_lookup._persist_fg_item_cache") as persist:
        item_code = resolve_item_code(conn, "NEW-SKU")

    assert item_code == "FGDC0999"
    persist.assert_called_once()


def test_resolve_item_code_raises_when_unmapped() -> None:
    conn = _mock_conn([None, None])

    with patch("walmart.sap_item_lookup.lookup_item_by_wm_sku", return_value=None):
        with pytest.raises(ValueError, match="No SAP item mapping for Walmart SKU 'UNKNOWN'"):
            resolve_item_code(conn, "UNKNOWN")


def test_resolve_item_code_skips_hana_when_disabled() -> None:
    conn = _mock_conn([None, None])

    with patch("walmart.sap_item_lookup.lookup_item_by_wm_sku") as hana_lookup:
        with pytest.raises(ValueError, match="sap_wm_sku_aliases or sap_fg_items"):
            resolve_item_code(conn, "UNKNOWN", allow_hana_lookup=False)

    hana_lookup.assert_not_called()
