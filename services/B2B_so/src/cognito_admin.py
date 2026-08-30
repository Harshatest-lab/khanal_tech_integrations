"""Admin-only Cognito user create / password set for the Team page."""

from __future__ import annotations

import os
import re
from typing import Any

_POOL = (os.environ.get("COGNITO_USER_POOL_ID") or "").strip()
_GROUP = {"admin": "B2BAdmin", "reviewer": "B2BReviewer", "viewer": "B2BViewer"}
_HAS_UPPER = re.compile(r"[A-Z]")
_HAS_LOWER = re.compile(r"[a-z]")
_HAS_DIGIT = re.compile(r"[0-9]")


def password_ok(password: str) -> str:
    if len(password) < 10:
        return "Password must be at least 10 characters"
    if not _HAS_UPPER.search(password):
        return "Password needs an uppercase letter"
    if not _HAS_LOWER.search(password):
        return "Password needs a lowercase letter"
    if not _HAS_DIGIT.search(password):
        return "Password needs a number"
    return ""


def has_login(email: str) -> bool:
    import boto3
    from botocore.exceptions import ClientError

    if not _POOL:
        raise RuntimeError("COGNITO_USER_POOL_ID is not set")
    client = boto3.client("cognito-idp")
    try:
        client.admin_get_user(UserPoolId=_POOL, Username=email.strip().lower())
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "UserNotFoundException":
            return False
        raise


def ensure_login(*, email: str, password: str, role: str) -> dict[str, str]:
    """Create Cognito user if missing and set a permanent password. Admin-only."""
    if not _POOL:
        raise RuntimeError("COGNITO_USER_POOL_ID is not set")
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("A valid email is required")
    pwd_err = password_ok(password)
    if pwd_err:
        raise ValueError(pwd_err)

    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("cognito-idp")
    created = False
    try:
        client.admin_get_user(UserPoolId=_POOL, Username=email)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "UserNotFoundException":
            raise
        try:
            client.admin_create_user(
                UserPoolId=_POOL,
                Username=email,
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "email_verified", "Value": "true"},
                ],
                TemporaryPassword=password,
                MessageAction="SUPPRESS",
            )
            created = True
        except ClientError as create_exc:
            raise ValueError(_cognito_message(create_exc)) from create_exc

    try:
        client.admin_set_user_password(
            UserPoolId=_POOL,
            Username=email,
            Password=password,
            Permanent=True,
        )
    except ClientError as exc:
        raise ValueError(_cognito_message(exc)) from exc

    group = _GROUP.get(role) or "B2BViewer"
    try:
        client.admin_add_user_to_group(UserPoolId=_POOL, Username=email, GroupName=group)
    except ClientError:
        pass
    return {"email": email, "created": created}


def set_login_enabled(email: str, enabled: bool) -> None:
    import boto3
    from botocore.exceptions import ClientError

    if not _POOL:
        raise RuntimeError("COGNITO_USER_POOL_ID is not set")
    client = boto3.client("cognito-idp")
    email = email.strip().lower()
    try:
        if enabled:
            client.admin_enable_user(UserPoolId=_POOL, Username=email)
        else:
            client.admin_disable_user(UserPoolId=_POOL, Username=email)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "UserNotFoundException":
            return
        raise ValueError(_cognito_message(exc)) from exc


def delete_login(email: str) -> None:
    import boto3
    from botocore.exceptions import ClientError

    if not _POOL:
        raise RuntimeError("COGNITO_USER_POOL_ID is not set")
    client = boto3.client("cognito-idp")
    try:
        client.admin_delete_user(UserPoolId=_POOL, Username=email.strip().lower())
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "UserNotFoundException":
            return
        raise ValueError(_cognito_message(exc)) from exc


def _cognito_message(exc: Any) -> str:
    msg = str((exc.response.get("Error") or {}).get("Message") or exc)
    if "Password" in msg or "password" in msg:
        return "Password was rejected by Cognito (use 10+ chars with upper, lower, and a number)"
    return msg or "Cognito user update failed"
