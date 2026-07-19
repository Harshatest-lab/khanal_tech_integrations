"""Public auth endpoints for the Walmart dashboard (Cognito USER_PASSWORD_AUTH)."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from walmart.http_response import json_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _http_method(event: dict[str, Any]) -> str:
    ctx = event.get("requestContext") or {}
    http = ctx.get("http") or {}
    return str(http.get("method") or event.get("httpMethod") or "GET").upper()


def _path(event: dict[str, Any]) -> str:
    return str(event.get("rawPath") or event.get("path") or "")


def _parse_json_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _cognito_client() -> Any:
    region = (os.environ.get("COGNITO_REGION") or os.environ.get("AWS_REGION") or "ap-south-1").strip()
    return boto3.client("cognito-idp", region_name=region)


def _client_id() -> str:
    client_id = (os.environ.get("COGNITO_CLIENT_ID") or "").strip()
    if not client_id:
        raise RuntimeError("COGNITO_CLIENT_ID is not set")
    return client_id


def _auth_result_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "id_token": result.get("IdToken"),
        "access_token": result.get("AccessToken"),
        "refresh_token": result.get("RefreshToken"),
        "expires_in": result.get("ExpiresIn"),
        "token_type": result.get("TokenType") or "Bearer",
    }


def _handle_login(body: dict[str, Any]) -> dict[str, Any]:
    email = str(body.get("email") or body.get("username") or "").strip().lower()
    password = str(body.get("password") or "")
    if not email or not password:
        return json_response(400, {"error": "email and password are required"})

    try:
        resp = _cognito_client().initiate_auth(
            ClientId=_client_id(),
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": email,
                "PASSWORD": password,
            },
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        logger.info("walmart_auth_login_failed code=%s", code)
        if code in ("NotAuthorizedException", "UserNotFoundException", "UserNotConfirmedException"):
            return json_response(401, {"error": "Invalid email or password"})
        return json_response(502, {"error": "Login failed", "code": code})

    challenge = resp.get("ChallengeName")
    if challenge:
        if challenge == "NEW_PASSWORD_REQUIRED":
            return json_response(
                403,
                {
                    "error": "Set a permanent password in Cognito: Users → your user → Actions → Reset password → Set as permanent.",
                    "challenge": challenge,
                },
            )
        return json_response(
            403,
            {
                "error": "Additional authentication step required",
                "challenge": challenge,
            },
        )

    auth = resp.get("AuthenticationResult") or {}
    if not auth.get("IdToken"):
        return json_response(502, {"error": "Login failed", "reason": "missing_id_token"})

    return json_response(200, {"ok": True, **_auth_result_payload(auth)})


def _handle_refresh(body: dict[str, Any]) -> dict[str, Any]:
    refresh_token = str(body.get("refresh_token") or "").strip()
    if not refresh_token:
        return json_response(400, {"error": "refresh_token is required"})

    try:
        resp = _cognito_client().initiate_auth(
            ClientId=_client_id(),
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": refresh_token},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        logger.info("walmart_auth_refresh_failed code=%s", code)
        return json_response(401, {"error": "Session expired — sign in again"})

    auth = resp.get("AuthenticationResult") or {}
    if not auth.get("IdToken"):
        return json_response(502, {"error": "Refresh failed", "reason": "missing_id_token"})

    payload = _auth_result_payload(auth)
    if not payload.get("refresh_token"):
        payload.pop("refresh_token", None)
    return json_response(200, {"ok": True, **payload})


def _handle_config() -> dict[str, Any]:
    user_pool_id = (os.environ.get("COGNITO_USER_POOL_ID") or "").strip()
    client_id = (os.environ.get("COGNITO_CLIENT_ID") or "").strip()
    region = (os.environ.get("COGNITO_REGION") or os.environ.get("AWS_REGION") or "ap-south-1").strip()
    if not user_pool_id or not client_id:
        return json_response(503, {"error": "Auth is not configured"})
    return json_response(
        200,
        {
            "region": region,
            "user_pool_id": user_pool_id,
            "client_id": client_id,
        },
    )


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    method = _http_method(event)
    if method == "OPTIONS":
        return json_response(200, {"ok": True})

    path = _path(event)
    body = _parse_json_body(event)

    try:
        if path.endswith("/v1/auth/config") and method == "GET":
            return _handle_config()
        if path.endswith("/v1/auth/login") and method == "POST":
            return _handle_login(body)
        if path.endswith("/v1/auth/refresh") and method == "POST":
            return _handle_refresh(body)
        return json_response(404, {"error": "Not found", "path": path})
    except RuntimeError as exc:
        logger.exception("walmart_auth_config_error")
        return json_response(503, {"error": str(exc)})
    except Exception:
        logger.exception("walmart_auth_error path=%s", path)
        return json_response(500, {"error": "Authentication service error"})
