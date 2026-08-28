import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from safexpress.store import _split_fairness_limit  # noqa: E402


@pytest.mark.parametrize(
    "limit,oldest_fraction,expected",
    [
        (0, 0.1, (0, 0)),
        (1, 0.1, (0, 1)),  # always reserve at least 1 slot for the oldest side
        (10, 0.1, (9, 1)),
        (10, 0.5, (5, 5)),
        (3, 0.1, (2, 1)),
    ],
)
def test_split_fairness_limit(limit, oldest_fraction, expected):
    assert _split_fairness_limit(limit, oldest_fraction) == expected


def test_split_fairness_limit_never_exceeds_total():
    for limit in range(0, 50):
        newest_n, oldest_n = _split_fairness_limit(limit, 0.1)
        assert newest_n + oldest_n == limit
        assert newest_n >= 0
        assert oldest_n >= 0
