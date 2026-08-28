"""SafeExpress authentication: SSM-cached OAuth token, renewed on expiry.

Ported from safexpress2/auth.py's SafexpressAuth, replacing the Frappe
"Safexpress Settings" doctype with SSM parameters (see credentials.py).
Deliberately no cross-invocation lock — see credentials.py's docstring for
why that's an acceptable simplification here.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .config import DEFAULT_TIMEOUT_SECONDS
from .credentials import SAFEXPRESS_CREDENTIALS_PARAM, SAFEXPRESS_TOKEN_PARAM
from .exceptions import SafexpressAuthenticationError

logger = logging.getLogger(__name__)


def _ssm() -> Any:
    import boto3

    region = (__import__("os").environ.get("AWS_REGION") or "ap-south-1").strip()
    return boto3.client("ssm", region_name=region)


def _get_param_json(name: str) -> dict[str, Any]:
    from botocore.exceptions import ClientError

    try:
        resp = _ssm().get_parameter(Name=name, WithDecryption=True)
        data = json.loads(resp["Parameter"]["Value"])
        return data if isinstance(data, dict) else {}
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return {}
        raise


def _put_param_json(name: str, data: dict[str, Any]) -> None:
    _ssm().put_parameter(Name=name, Value=json.dumps(data), Type="SecureString", Overwrite=True)


def get_credentials() -> dict[str, str]:
    creds = _get_param_json(SAFEXPRESS_CREDENTIALS_PARAM)
    required = ("login_url", "username", "password", "client_basic_auth", "api_key", "identifier", "pod_url")
    missing = [k for k in required if not creds.get(k)]
    if missing:
        raise SafexpressAuthenticationError(
            f"SSM {SAFEXPRESS_CREDENTIALS_PARAM} is missing: {', '.join(missing)}"
        )
    return creds


def _token_expired(token: dict[str, Any]) -> bool:
    expires_on = (token.get("expires_on") or "").strip()
    if not expires_on:
        return True
    try:
        exp = datetime.fromisoformat(expires_on.replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) >= exp


def _renew_token(creds: dict[str, str]) -> dict[str, Any]:
    payload = {
        "grant_type": "client_credentials",
        "scope": "server/waybillapps",
        "username": creds["username"],
        "password": creds["password"],
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "Authorization": f"Basic {creds['client_basic_auth']}",
    }
    try:
        resp = requests.post(
            creds["login_url"], headers=headers, data=payload,
            timeout=DEFAULT_TIMEOUT_SECONDS, verify=False,
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.exceptions.RequestException as e:
        raise SafexpressAuthenticationError(f"SafeExpress token renewal failed: {e}") from e
    except (ValueError, KeyError) as e:
        raise SafexpressAuthenticationError(f"SafeExpress token response parsing failed: {e}") from e

    expires_on = (datetime.now(timezone.utc) + timedelta(seconds=int(body["expires_in"]))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    token = {"access_token": body["access_token"], "expires_on": expires_on}
    _put_param_json(SAFEXPRESS_TOKEN_PARAM, token)
    logger.info("SafeExpress token renewed, expires_on=%s", expires_on)
    return token


def get_pod_auth_headers() -> dict[str, str]:
    """Headers required for the POD document fetch (x-api-key, authorization,
    Identifier). Tracking-by-invoice/waybill calls need no auth headers at
    all — ported as-is from safexpress2/api_client.py, which never attaches
    auth_headers to those two calls."""
    creds = get_credentials()
    token = _get_param_json(SAFEXPRESS_TOKEN_PARAM)
    if _token_expired(token):
        token = _renew_token(creds)
    return {
        "x-api-key": creds["api_key"],
        "authorization": token["access_token"],
        "Identifier": creds["identifier"],
    }


def get_pod_base_url() -> str:
    return get_credentials()["pod_url"]
