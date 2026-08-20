"""Tests for infra_status's pure-logic helpers (type classification,
metric summing). fetch_infra_status itself is boto3 I/O, verified live
against prod, not mocked here -- same pattern as alerts.py/credentials.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce import infra_status  # noqa: E402


def test_function_type_scheduled():
    assert infra_status._function_type("unicommercePoGrn") == "scheduled"
    assert infra_status._function_type("unicommerceRecon") == "scheduled"


def test_function_type_debug_when_listed():
    # No debug functions currently deployed (DEBUG_FUNCTIONS is empty) --
    # exercise the classification branch directly instead.
    assert "somefn" not in infra_status.DEBUG_FUNCTIONS
    infra_status.DEBUG_FUNCTIONS.append("somefn")
    try:
        assert infra_status._function_type("somefn") == "debug"
    finally:
        infra_status.DEBUG_FUNCTIONS.remove("somefn")


def test_function_type_defaults_to_http():
    assert infra_status._function_type("unicommerceDashboardApi") == "http"
    assert infra_status._function_type("somethingUnlisted") == "http"


def test_sum_datapoints_empty():
    assert infra_status._sum_datapoints([]) == 0


def test_sum_datapoints_sums_sum_field():
    assert infra_status._sum_datapoints([{"Sum": 3.0}, {"Sum": 4.0}]) == 7
