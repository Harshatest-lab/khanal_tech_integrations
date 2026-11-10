"""Freshdesk access settings from SSM.

One SecureString JSON parameter (default ``/freshdesk/credentials``)::

    {
      "domain": "khanalfoods.freshdesk.com",
      "api_key": "...",
      "category_ids": [87000012345]
    }

``category_ids`` are the knowledge base (Solutions) categories the storefront may
show. Keeping them here (not in serverless.yml) means adding a category needs no
redeploy — new containers pick it up. Never commit the API key to git.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

FRESHDESK_CREDENTIALS_PARAM = (
    os.environ.get("FRESHDESK_CREDENTIALS_PARAM") or "/freshdesk/credentials"
).strip()


class FreshdeskConfigError(RuntimeError):
    """SSM parameter missing or incomplete."""


@dataclass(frozen=True)
class FreshdeskConfig:
    domain: str
    api_key: str
    category_ids: tuple[int, ...]


def _ssm() -> Any:
    import boto3

    region = (os.environ.get("AWS_REGION") or "ap-south-1").strip()
    return boto3.client("ssm", region_name=region)


def parse_config(data: dict[str, Any]) -> FreshdeskConfig:
    domain = str(data.get("domain") or "").strip()
    domain = domain.removeprefix("https://").removeprefix("http://").split("/")[0]
    api_key = str(data.get("api_key") or "").strip()

    raw_ids = data.get("category_ids") or []
    if isinstance(raw_ids, (str, int)):
        raw_ids = str(raw_ids).split(",")
    category_ids = tuple(int(str(c).strip()) for c in raw_ids if str(c).strip().isdigit())

    missing = [
        name
        for name, value in (("domain", domain), ("api_key", api_key), ("category_ids", category_ids))
        if not value
    ]
    if missing:
        raise FreshdeskConfigError(
            f"SSM {FRESHDESK_CREDENTIALS_PARAM} is missing: {', '.join(missing)}"
        )
    return FreshdeskConfig(domain=domain, api_key=api_key, category_ids=category_ids)


@lru_cache(maxsize=1)
def get_config() -> FreshdeskConfig:
    from botocore.exceptions import ClientError

    try:
        resp = _ssm().get_parameter(Name=FRESHDESK_CREDENTIALS_PARAM, WithDecryption=True)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ParameterNotFound":
            raise FreshdeskConfigError(f"SSM {FRESHDESK_CREDENTIALS_PARAM} not found") from e
        raise
    data = json.loads(resp["Parameter"]["Value"])
    return parse_config(data if isinstance(data, dict) else {})
