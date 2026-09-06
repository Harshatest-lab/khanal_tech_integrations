"""Portal auth: Cognito JWT claims + b2b.users perm_delivery + logistics channel."""

from __future__ import annotations

from typing import Any

PERM_NONE = "none"
PERM_VIEW = "view"
PERM_EDIT = "edit"
PERM_DELETE = "delete"

CHANNEL_LOGISTICS = "logistics"

_LEVEL_RANK = {PERM_NONE: 0, PERM_VIEW: 1, PERM_EDIT: 2, PERM_DELETE: 3}

ROLE_DEFAULTS = {
    "admin": PERM_DELETE,
    "reviewer": PERM_EDIT,
    "viewer": PERM_NONE,
}

GROUP_DELIVERY = "B2BDelivery"
GROUP_ADMIN = "B2BAdmin"


def _claims(event: dict[str, Any]) -> dict[str, Any]:
    auth = (event.get("requestContext") or {}).get("authorizer") or {}
    jwt = auth.get("jwt") or {}
    return jwt.get("claims") or {}


def actor_email(event: dict[str, Any]) -> str:
    claims = _claims(event)
    return str(claims.get("email") or claims.get("cognito:username") or "").strip()


def cognito_groups(event: dict[str, Any]) -> list[str]:
    claims = _claims(event)
    raw = claims.get("cognito:groups") or ""
    if isinstance(raw, list):
        return [str(g) for g in raw]
    text = str(raw).strip()
    if not text:
        return []
    return [p for p in text.replace("[", " ").replace("]", " ").replace(",", " ").split() if p]


def _normalize_level(raw: Any) -> str:
    level = str(raw or "").strip().lower()
    return level if level in _LEVEL_RANK else ""


def _channels(user: dict[str, Any] | None) -> list[str]:
    raw = (user or {}).get("channels") or []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(c).strip().lower() for c in raw if str(c).strip()]


def user_has_logistics_module(user: dict[str, Any] | None) -> bool:
    """Delivery is assigned like a marketplace channel (channels includes 'logistics')."""
    if not user:
        return False
    if user.get("can_manage_users"):
        return True
    return CHANNEL_LOGISTICS in _channels(user)


def resolve_delivery_level(user: dict[str, Any] | None, groups: list[str]) -> str:
    if GROUP_ADMIN in groups:
        return PERM_DELETE
    if user and user.get("can_manage_users"):
        return PERM_DELETE
    if user and user.get("is_active"):
        if not user_has_logistics_module(user):
            # Cognito bootstrap group still works without the Team checkbox.
            if GROUP_DELIVERY in groups:
                return PERM_EDIT
            return PERM_NONE
        explicit = _normalize_level(user.get("perm_delivery"))
        if explicit:
            return explicit
        role = str(user.get("role") or "viewer").strip().lower()
        return ROLE_DEFAULTS.get(role, PERM_NONE)
    if GROUP_DELIVERY in groups:
        return PERM_EDIT
    return PERM_NONE


def has_delivery_perm(level: str, need: str) -> bool:
    have = _LEVEL_RANK.get(level, 0)
    want = _LEVEL_RANK.get(need, 0)
    return have >= want


def ensure_portal_schema(conn: Any) -> None:
    """Idempotent column used by portal RBAC (same as B2B dashboard migrate)."""
    try:
        conn.execute(
            "ALTER TABLE b2b.users ADD COLUMN IF NOT EXISTS perm_delivery text NOT NULL DEFAULT ''"
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def load_user_row(conn: Any, email: str) -> dict[str, Any] | None:
    if not email:
        return None
    ensure_portal_schema(conn)
    cur = conn.execute(
        """
        SELECT u.email, u.full_name, u.role, u.is_active,
               COALESCE(u.perm_delivery, '') AS perm_delivery,
               r.can_manage_users,
               COALESCE(u.channels, ARRAY[]::text[]) AS channels
        FROM b2b.users u
        JOIN b2b.roles r ON r.role = u.role
        WHERE lower(u.email) = lower(%s)
        """,
        (email.strip(),),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "email": row[0],
        "full_name": row[1],
        "role": row[2],
        "is_active": bool(row[3]),
        "perm_delivery": row[4],
        "can_manage_users": bool(row[5]),
        "channels": list(row[6] or []),
    }


def portal_access(event: dict[str, Any], conn: Any, need: str) -> tuple[str | None, str | None]:
    """Return (email, error_message). error_message set when denied."""
    email = actor_email(event)
    if not email:
        return None, "Not signed in"
    groups = cognito_groups(event)
    user = load_user_row(conn, email)
    level = resolve_delivery_level(user, groups)
    if not has_delivery_perm(level, need):
        return email, f"Not allowed — need {need} access for logistics delivery"
    return email, None
