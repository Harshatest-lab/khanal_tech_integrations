"""SAP SL JSON parsing tolerates unescaped control chars in string values."""

from __future__ import annotations

import json

from khanal_shared.sap_sl.http import _escape_raw_controls_in_json_strings, loads_sap_json


def test_loads_valid_json_unchanged() -> None:
    payload = {"DocEntry": 1, "Address": "MS USA"}
    assert loads_sap_json(json.dumps(payload)) == payload


def test_loads_escaped_cr_already_valid() -> None:
    raw = '{"Address":"MS\\rUSA","DocEntry":32250}'
    assert loads_sap_json(raw) == {"Address": "MS\rUSA", "DocEntry": 32250}


def test_loads_raw_cr_in_address_like_sap() -> None:
    # SAP sometimes embeds a literal CR between state and country.
    raw = '{"odata.etag":"W/\\"abc\\"","Address":"MS\rUSA","DocEntry":32250}'
    try:
        json.loads(raw)
        raise AssertionError("expected strict json.loads to reject raw CR")
    except json.JSONDecodeError:
        pass
    parsed = loads_sap_json(raw)
    assert parsed["DocEntry"] == 32250
    assert parsed["Address"] == "MS\rUSA"


def test_escape_does_not_touch_structure_whitespace() -> None:
    raw = '{\n  "DocEntry": 1\n}'
    assert _escape_raw_controls_in_json_strings(raw) == raw
    assert loads_sap_json(raw) == {"DocEntry": 1}
