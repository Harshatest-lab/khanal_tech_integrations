"""Ensure B2B_so/src wins over other services' top-level module names (e.g. handlers)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_B2B_SRC = str(Path(__file__).resolve().parents[1] / "src")
_B2B_ONLY_MODULES = (
    "handlers",
    "store",
    "blinkit",
    "b2b_sappost",
    "permissions",
    "instamart",
    "zepto",
    "bigbasket",
    "flipkart",
    "sap_hana_sync",
    "hana_lookup",
    "gst_validate",
    "cognito_admin",
)


def _ensure_b2b_src() -> None:
    if _B2B_SRC in sys.path:
        sys.path.remove(_B2B_SRC)
    sys.path.insert(0, _B2B_SRC)
    for name in _B2B_ONLY_MODULES:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        mod_file = (getattr(mod, "__file__", "") or "").replace("\\", "/")
        if _B2B_SRC not in mod_file:
            sys.modules.pop(name, None)


_ensure_b2b_src()


def pytest_collectstart(collector) -> None:
    node_path = getattr(collector, "path", None) or getattr(collector, "fspath", None)
    if node_path is None:
        return
    text = str(node_path).replace("\\", "/")
    if "/B2B_so/tests" in text:
        _ensure_b2b_src()


@pytest.fixture(autouse=True)
def no_real_aws(monkeypatch):
    """Tests must never reach the real AWS account (SSM, S3, the shared SAP session record).

    Without this, a test that falls through to a SAP lookup uses the developer's own AWS login and can
    leave the shared /sap/b1_session record locked for everyone.
    """
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/aws-credentials")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/aws-config")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
