import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from safexpress.client import _format_invoice_number, _series_variants  # noqa: E402
from safexpress.config import INVOICE_SERIES_PREFIXES  # noqa: E402


def test_series_variants_empty_returns_all_configured_prefixes():
    assert _series_variants("") == INVOICE_SERIES_PREFIXES
    assert _series_variants("   ") == INVOICE_SERIES_PREFIXES


def test_series_variants_dedupes_case_variants():
    assert _series_variants("kain24") == ["kain24", "KAIN24"]


def test_series_variants_no_dup_when_already_uppercase():
    assert _series_variants("KAIN24") == ["KAIN24", "kain24"]


def test_format_invoice_number_with_series():
    assert _format_invoice_number("KAIN24", "12345") == "KAIN24/12345"


def test_format_invoice_number_without_series_returns_bare_number():
    assert _format_invoice_number("", "12345") == "12345"
