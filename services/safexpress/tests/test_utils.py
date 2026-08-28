import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from safexpress.utils import SafexpressDateParsingError, parse_delivery_date  # noqa: E402


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("25-Nov-2024", "2024-11-25"),
        ("25/11/2024", "2024-11-25"),
        ("2024-11-25", "2024-11-25"),
        ("25-11-2024", "2024-11-25"),
    ],
)
def test_parse_delivery_date_accepts_known_formats(raw, expected):
    assert parse_delivery_date(raw) == expected


def test_parse_delivery_date_rejects_empty():
    with pytest.raises(SafexpressDateParsingError):
        parse_delivery_date("")


def test_parse_delivery_date_rejects_unknown_format():
    with pytest.raises(SafexpressDateParsingError):
        parse_delivery_date("not-a-date")
