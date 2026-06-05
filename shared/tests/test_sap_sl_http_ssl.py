"""SAP SL HTTP SSL context (env-controlled)."""

from __future__ import annotations

import os
import ssl

import pytest

from khanal_shared.sap_sl import http as sap_http


@pytest.fixture
def clear_verify_env() -> None:
    old = os.environ.pop("SAP_SL_VERIFY_SSL", None)
    yield
    if old is not None:
        os.environ["SAP_SL_VERIFY_SSL"] = old
    else:
        os.environ.pop("SAP_SL_VERIFY_SSL", None)


def test_sap_sl_ssl_context_verifies_by_default(clear_verify_env: None) -> None:
    os.environ.pop("SAP_SL_VERIFY_SSL", None)
    ctx = sap_http._sap_sl_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("value", ("false", "0", "no", "off"))
def test_sap_sl_ssl_context_insecure_when_disabled(clear_verify_env: None, value: str) -> None:
    os.environ["SAP_SL_VERIFY_SSL"] = value
    ctx = sap_http._sap_sl_ssl_context()
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False
