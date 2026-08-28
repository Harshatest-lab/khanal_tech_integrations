import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from safexpress.exceptions import SafexpressAPIError, SafexpressWaybillNotFoundError  # noqa: E402


def test_waybill_not_found_is_never_retryable():
    assert SafexpressWaybillNotFoundError("no waybill").is_retryable() is False


def test_gateway_timeout_is_retryable():
    assert SafexpressAPIError("timeout", status_code=504).is_retryable() is True


def test_server_error_is_retryable():
    assert SafexpressAPIError("boom", status_code=500).is_retryable() is True


def test_client_error_other_than_404_is_not_retryable():
    assert SafexpressAPIError("bad request", status_code=400).is_retryable() is False


def test_unknown_status_defaults_to_retryable():
    assert SafexpressAPIError("network blip").is_retryable() is True


def test_no_waybill_message_short_circuits_regardless_of_status():
    assert SafexpressAPIError("No waybill found for invoice X", status_code=500).is_retryable() is False
