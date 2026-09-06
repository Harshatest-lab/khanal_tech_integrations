"""HTTP API response helpers for the logistics portal Lambda."""

from __future__ import annotations

import json
from typing import Any


def json_response(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def method(event: dict[str, Any]) -> str:
    http = (event.get("requestContext") or {}).get("http") or {}
    return str(http.get("method") or event.get("httpMethod") or "GET").upper()


def path(event: dict[str, Any]) -> str:
    return str(event.get("rawPath") or event.get("path") or "")


def query(event: dict[str, Any]) -> dict[str, str]:
    params = event.get("queryStringParameters") or {}
    if not isinstance(params, dict):
        return {}
    return {str(k): str(v) for k, v in params.items() if v is not None}


def body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}
