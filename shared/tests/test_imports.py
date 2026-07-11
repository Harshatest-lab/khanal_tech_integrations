from khanal_shared import __version__
from khanal_shared.hana import (
    get_hana_params,
    hana_connection,
    hana_enabled,
    hana_use_ssm,
)
from khanal_shared.sap_sl import (
    build_opener_and_base_from_env,
    build_opener_and_base_via_ssm,
    sap_sl_enabled,
    sap_sl_use_ssm,
)


def test_version_is_defined() -> None:
    assert __version__


def test_sap_sl_exports() -> None:
    assert callable(build_opener_and_base_via_ssm)
    assert callable(sap_sl_use_ssm)
    assert callable(sap_sl_enabled)
    assert callable(build_opener_and_base_from_env)


def test_hana_exports() -> None:
    assert callable(get_hana_params)
    assert callable(hana_use_ssm)
    assert callable(hana_enabled)
    assert callable(hana_connection)
