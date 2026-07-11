"""Load SAP HANA connection settings from SSM Parameter Store.

Parameters (SecureString unless noted)::

    Default (India / legacy)::

        /sap/hana_config       — host, port, optional database, schema, max_pool_size
        /sap/hana_credentials  — username, password

    US INC profile (``HANA_PROFILE=inc``)::

        /sap/inc/hana_config
        /sap/inc/hana_credentials

Set ``HANA_USE_SSM=1`` so ``get_hana_params()`` reads these at runtime.
"""

from __future__ import annotations

import json
import os
from typing import Any


def hana_use_ssm() -> bool:
    v = (os.environ.get("HANA_USE_SSM") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def hana_ssm_prefix() -> str:
    """SSM path prefix from ``HANA_PROFILE`` (e.g. ``inc`` → ``/sap/inc``)."""
    profile = (os.environ.get("HANA_PROFILE") or "").strip().lower()
    if not profile or profile in ("default", "sap"):
        return "/sap"
    return f"/sap/{profile}"


def param_hana_config() -> str:
    return f"{hana_ssm_prefix()}/hana_config"


def param_hana_credentials() -> str:
    return f"{hana_ssm_prefix()}/hana_credentials"


# Backward-compatible module constants (default profile paths).
PARAM_HANA_CONFIG = "/sap/hana_config"
PARAM_HANA_CREDENTIALS = "/sap/hana_credentials"


def _ssm() -> Any:
    import boto3

    region = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "").strip()
    if not region:
        region = "us-east-1"
    return boto3.client("ssm", region_name=region)


def _get_param_json(name: str) -> dict[str, Any]:
    from botocore.exceptions import ClientError

    try:
        resp = _ssm().get_parameter(Name=name, WithDecryption=True)
        raw = resp["Parameter"]["Value"]
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        return data
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return {}
        raise


def load_hana_config_from_ssm() -> dict[str, Any]:
    return _get_param_json(param_hana_config())


def load_hana_credentials_from_ssm() -> dict[str, Any]:
    return _get_param_json(param_hana_credentials())
