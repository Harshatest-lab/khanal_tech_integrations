"""Delhivery LTL track API (portal Check LR)."""

from __future__ import annotations

import json
from typing import Any

from khanal_shared.delivery.delhivery_ltl_ssm import resolve_delhivery_ltl_settings

from src.portal_invoice import PortalValidationError


def _ltl_settings() -> tuple[str, str, str]:
    try:
        return resolve_delhivery_ltl_settings()
    except RuntimeError as exc:
        raise PortalValidationError(str(exc)) from exc


def login_delhivery_ltl(force_refresh: bool = False) -> str:
    """POST /ums/login → JWT. No cross-invoke cache in Lambda v1 (refresh per track if needed)."""
    import requests

    base, username, password = _ltl_settings()
    login_url = f"{base}/ums/login"

    try:
        response = requests.post(
            login_url,
            json={"username": username, "password": password},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=45,
        )
    except requests.RequestException as exc:
        raise PortalValidationError(f"Failed to reach Delhivery login API: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        body = {"raw_text": (response.text or "")[:2000]}

    if not response.ok:
        msg = None
        if isinstance(body, dict):
            msg = body.get("message")
            err = body.get("error")
            if msg is None and isinstance(err, dict):
                msg = err.get("message")
            elif msg is None and isinstance(err, str):
                msg = err
        if not msg:
            msg = (response.text or "")[:500] or "unknown error"
        raise PortalValidationError(f"Delhivery login failed ({response.status_code}): {msg}")

    data = body.get("data") if isinstance(body, dict) else None
    jwt = data.get("jwt") if isinstance(data, dict) else None
    if not jwt and isinstance(body, dict):
        jwt = body.get("jwt")
    if not jwt:
        raise PortalValidationError("Delhivery login did not return a JWT.")
    return str(jwt)


def _is_token_error(status_code: int, body: dict) -> bool:
    if status_code not in (400, 401, 403):
        return False
    text = json.dumps(body).lower()
    return "invalid token" in text or "token" in text


def fetch_delhivery_tracking(lrnum: str) -> dict[str, Any]:
    import requests

    lr = (lrnum or "").strip()
    if not lr:
        raise PortalValidationError("LR number is required.")

    base, _, _ = _ltl_settings()
    url = f"{base}/lrn/track"
    jwt = login_delhivery_ltl()
    response = None
    body: dict | Any = {}

    for attempt in range(2):
        try:
            response = requests.get(
                url,
                params={"lrnum": lr},
                headers={"Accept": "application/json", "Authorization": f"Bearer {jwt}"},
                timeout=45,
            )
        except requests.RequestException as exc:
            raise PortalValidationError(f"Failed to reach Delhivery tracking API: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            body = {"raw_text": (response.text or "")[:2000]}

        if response.ok:
            return body if isinstance(body, dict) else {"data": body}

        if attempt == 0 and _is_token_error(response.status_code, body if isinstance(body, dict) else {}):
            jwt = login_delhivery_ltl(force_refresh=True)
            continue
        break

    raise PortalValidationError(
        _friendly_track_error(response.status_code if response else 0, body if isinstance(body, dict) else {})
    )


def _friendly_track_error(status_code: int, body: dict) -> str:
    msg = body.get("message") if isinstance(body, dict) else None
    err = body.get("error") if isinstance(body, dict) else None
    if msg is None and isinstance(err, dict):
        msg = err.get("message")
    if isinstance(msg, dict):
        msg = msg.get("message") or json.dumps(msg)
    msg_s = str(msg or body or "").lower()
    if status_code == 404 or "data not found" in msg_s:
        return "The LR number was not found in Delhivery. Please check the number and try again."
    if "invalid token" in msg_s:
        return "Delhivery login failed. Please contact your system administrator."
    if "not active" in msg_s or "user not found" in msg_s or "not authenticated" in msg_s:
        return "Delhivery login failed — user not found or inactive for this API host. Check username/password and track_base_url in SSM."
    if isinstance(msg, str) and msg.strip() and len(msg) < 200:
        return msg.strip()
    return "Delhivery tracking could not be completed. Please verify the LR number and try again."


# Re-export parse helpers for existing callers (`from src.portal_delhivery import tracking_summary`).
from src.portal_delhivery_parse import (  # noqa: E402
    envelope_has_lr,
    normalize_delhivery_track_envelope,
    tracking_summary,
)

__all__ = [
    "login_delhivery_ltl",
    "fetch_delhivery_tracking",
    "tracking_summary",
    "normalize_delhivery_track_envelope",
    "envelope_has_lr",
]
