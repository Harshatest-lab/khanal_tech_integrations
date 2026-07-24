"""Tests for tax code resolution and state/tax-bucket classification,
ported from khanal_tech_integrations/unicommerce/{geo,mappers/tax}.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce.geo import classify_tax_bucket, normalize_state_code, resolve_origin_state_code  # noqa: E402
from unicommerce.settings import UnicommerceSettings  # noqa: E402
from unicommerce.tax import compute_tax_from_line, get_tax_code  # noqa: E402


def test_normalize_state_code_handles_aliases_and_raw_codes():
    assert normalize_state_code("Karnataka") == "KA"
    assert normalize_state_code("karnataka") == "KA"
    assert normalize_state_code("KA") == "KA"
    assert normalize_state_code("Tamil Nadu") == "TN"
    assert normalize_state_code("") == ""


def test_classify_tax_bucket_same_state_is_intra():
    assert classify_tax_bucket("Karnataka", None, "KA") == "cgst_sgst"


def test_classify_tax_bucket_different_state_is_inter():
    assert classify_tax_bucket("Maharashtra", None, "KA") == "igst"


def test_classify_tax_bucket_no_ship_state_falls_back_to_payload_hint():
    assert classify_tax_bucket("", "some IGST reference", "KA") == "igst"
    assert classify_tax_bucket("", "some CGST reference", "KA") == "cgst_sgst"
    assert classify_tax_bucket("", "", "KA") == "unknown"


def test_resolve_origin_state_code_channel_override_wins():
    settings = UnicommerceSettings(origin_state_code="KA")
    # AMAZON_FBA_IN_MAA4 has a hardcoded override to TN even with no
    # explicit channel_origin_state_map entry.
    assert resolve_origin_state_code("AMAZON_FBA_IN_MAA4", settings) == "TN"


def test_resolve_origin_state_code_settings_map_beats_hardcoded_override():
    settings = UnicommerceSettings(
        origin_state_code="KA",
        channel_origin_state_map={"AMAZON_FBA_IN_MAA4": "KL"},
    )
    assert resolve_origin_state_code("AMAZON_FBA_IN_MAA4", settings) == "KL"


def test_resolve_origin_state_code_default_channel_uses_settings_origin():
    settings = UnicommerceSettings(origin_state_code="KA")
    assert resolve_origin_state_code("FLIPKART", settings) == "KA"


def test_get_tax_code_intra_vs_inter():
    settings = UnicommerceSettings(tax_code_map={"18_intra": "KACS18", "18_inter": "KAIG18"})
    assert get_tax_code(18, "Karnataka", "KA", settings) == "KACS18"
    assert get_tax_code(18, "Maharashtra", "KA", settings) == "KAIG18"


def test_get_tax_code_zero_percent_uses_directional_key_first():
    settings = UnicommerceSettings(tax_code_map={"0_intra": "KAEX0", "0_exempt": "EXEMPT"})
    assert get_tax_code(0, "Karnataka", "KA", settings) == "KAEX0"


def test_get_tax_code_zero_percent_falls_back_to_exempt():
    settings = UnicommerceSettings(tax_code_map={"0_exempt": "EXEMPT"})
    assert get_tax_code(0, "Karnataka", "KA", settings) == "EXEMPT"


def test_get_tax_code_raises_when_unmapped():
    settings = UnicommerceSettings(tax_code_map={})
    try:
        get_tax_code(18, "Karnataka", "KA", settings)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "18_intra" in str(e)


def test_compute_tax_from_line_exclusive():
    # 100 * 2 qty at 18% ex-tax = 200 + 36 tax
    assert compute_tax_from_line(100, 2, 18, tax_inclusive=False) == 36.0


def test_compute_tax_from_line_inclusive():
    # 118 gross including 18% tax -> tax = 118 - (118/1.18) = 18
    assert compute_tax_from_line(118, 1, 18, tax_inclusive=True) == 18.0
