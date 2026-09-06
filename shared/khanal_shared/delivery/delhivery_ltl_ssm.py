"""Delhivery LTL track API settings from SSM Parameter Store or Lambda env."""

from __future__ import annotations

import json
import os
from typing import Any

PARAM_DELHIVERY_LTL_CONFIG = "/delivery/delhivery_ltl_config"
PARAM_DELHIVERY_LTL_CREDENTIALS = "/delivery/delhivery_ltl_credentials"


def delhivery_ltl_use_ssm() -> bool:
    v = (os.environ.get("DELHIVERY_LTL_USE_SSM") or "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    # Default: prefer SSM when classic env creds are not all set.
    base = (os.environ.get("DELHIVERY_LTL_TRACK_BASE_URL") or "").strip()
    user = (os.environ.get("DELHIVERY_LTL_USERNAME") or "").strip()
    pw = (os.environ.get("DELHIVERY_LTL_PASSWORD") or "").strip()
    return not (base and user and pw)


def _ssm() -> Any:
    import boto3

    region = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "").strip()
    if not region:
        region = "ap-south-1"
    return boto3.client("ssm", region_name=region)


def _get_param_json(name: str) -> dict[str, Any]:
    from botocore.exceptions import ClientError

    try:
        resp = _ssm().get_parameter(Name=name, WithDecryption=True)
        raw = resp["Parameter"]["Value"]
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return {}
        raise


def load_delhivery_ltl_config_from_ssm() -> dict[str, Any]:
    return _get_param_json(PARAM_DELHIVERY_LTL_CONFIG)


def load_delhivery_ltl_credentials_from_ssm() -> dict[str, Any]:
    return _get_param_json(PARAM_DELHIVERY_LTL_CREDENTIALS)


def resolve_delhivery_ltl_settings() -> tuple[str, str, str]:
    """
    Return ``(track_base_url, username, password)``.

    When ``DELHIVERY_LTL_USE_SSM`` is enabled (default if env creds missing), read SSM.
  Otherwise use ``DELHIVERY_LTL_*`` Lambda environment variables.
    """
    if delhivery_ltl_use_ssm():
        cfg = load_delhivery_ltl_config_from_ssm()
        cred = load_delhivery_ltl_credentials_from_ssm()
        base = str(cfg.get("track_base_url") or cfg.get("base_url") or "").strip().rstrip("/")
        user = str(cred.get("username") or cred.get("user") or "").strip()
        password = str(cred.get("password") or "").strip()
        if base and user and password:
            return base, user, password
        raise RuntimeError(
            "Delhivery LTL SSM is enabled but parameters are incomplete. "
            f"Set {PARAM_DELHIVERY_LTL_CONFIG} (track_base_url) and "
            f"{PARAM_DELHIVERY_LTL_CREDENTIALS} (username, password)."
        )

    base = (os.environ.get("DELHIVERY_LTL_TRACK_BASE_URL") or "").strip().rstrip("/")
    user = (os.environ.get("DELHIVERY_LTL_USERNAME") or "").strip()
    password = (os.environ.get("DELHIVERY_LTL_PASSWORD") or "").strip()
    if not base or not user or not password:
        raise RuntimeError(
            "Delhivery LTL is not configured. Set DELHIVERY_LTL_USE_SSM=1 and SSM parameters, "
            "or set DELHIVERY_LTL_TRACK_BASE_URL / USERNAME / PASSWORD on the portal Lambda."
        )
    return base, user, password
