"""Ensure B2B_so/src wins over other services' top-level module names (e.g. handlers)."""

from __future__ import annotations

import sys
from pathlib import Path

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
