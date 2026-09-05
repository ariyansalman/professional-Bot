"""Admin authentication, permission checks and confirmation tokens."""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.permissions.rbac import Permissions, permissions_for, require_permission
from app.core.config import get_settings
from app.core.exceptions import PermissionDeniedError
from app.core.logging import get_logger
from app.core.redis import get_redis, namespaced
from app.db.models.user import User
from app.db.repositories.support import AuditRepository
from app.db.repositories.users import RoleRepository
from app.domain.enums import AuditAction, RoleName

log = get_logger(__name__)


@dataclass(slots=True)
class AdminContext:
    """Who the operator is and what they may do."""

    user: User
    roles: frozenset[RoleName]
    permissions: frozenset[Permissions]

    def can(self, permission: Permissions) -> bool:
        return permission in self.permissions

    def require(self, permission: Permissions) -> None:
        if permission not in self.permissions:
            raise PermissionDeniedError(
                f"user {self.user.id} lacks {permission}",
                context={"required": permission.value},
            )

    @property
    def label(self) -> str:
        return f"{self.user.display_name} ({self.user.telegram_id})"


async def load_admin_context(session: AsyncSession, user: User) -> AdminContext | None:
    """Resolve an operator's roles.

    Bootstrap admins from configuration are granted SUPER_ADMIN so a fresh
    deployment has a way in; everyone else must hold a database-assigned role.
    """
    settings = get_settings()
    role_names = {RoleName(role.name) for role in user.roles}

    if user.telegram_id in settings.telegram.bootstrap_admin_ids:
        role_names.add(RoleName.SUPER_ADMIN)
        # Persist the bootstrap grant so it is auditable and revocable.
        if not any(role.name is RoleName.SUPER_ADMIN for role in user.roles):
            roles_repo = RoleRepository(session)
            role = await roles_repo.get_by_name(RoleName.SUPER_ADMIN)
            if role is not None:
                await roles_repo.assign(user, role)
                log.info("admin.bootstrap_role_granted", user_id=str(user.id))

    if not role_names:
        return None
    return AdminContext(
        user=user, roles=frozenset(role_names), permissions=permissions_for(role_names)
    )


async def audit(
    session: AsyncSession,
    context: AdminContext,
    action: AuditAction,
    *,
    target_type: str | None = None,
    target_id: Any = None,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Record a high-risk admin action.

    Secrets are stripped before anything is written: the audit trail records
    that a credential changed, never the credential itself.
    """
    await AuditRepository(session).record(
        action=action,
        actor_id=context.user.id,
        actor_label=context.label,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        reason=reason,
        details=_redact(details or {}),
    )
    log.info(
        "admin.action",
        action=action.value,
        actor_id=str(context.user.id),
        target_type=target_type,
        target_id=str(target_id) if target_id else None,
    )


_SECRET_FIELDS = {
    "api_key",
    "api_secret",
    "secret",
    "passphrase",
    "token",
    "password",
    "encrypted_api_key",
    "encrypted_api_secret",
    "encrypted_passphrase",
    "plaintext",
}


def _redact(details: dict[str, Any]) -> dict[str, Any]:
    return {
        key: ("***" if key.lower() in _SECRET_FIELDS else value)
        for key, value in details.items()
    }


# --------------------------------------------------------------------------
# Confirmation tokens for destructive actions
# --------------------------------------------------------------------------


async def create_confirmation(
    *, actor_id: uuid.UUID, action: str, payload: dict[str, Any], ttl: int | None = None
) -> str:
    """Store a pending high-risk action and return its one-time token.

    The token is bound to the operator, so a leaked callback cannot be replayed
    by anyone else, and it expires so a stale button cannot fire later.
    """
    token = secrets.token_urlsafe(12)
    body = json.dumps(
        {"actor_id": str(actor_id), "action": action, "payload": payload},
        default=str,
    )
    ttl = ttl or get_settings().security.admin_confirmation_ttl
    await get_redis().set(namespaced("confirm", token), body, ex=ttl)
    return token


async def consume_confirmation(
    *, token: str, actor_id: uuid.UUID
) -> tuple[str, dict[str, Any]] | None:
    """Atomically fetch and delete a pending action.

    Deleting before acting means a double-tap cannot execute the action twice.
    """
    redis = get_redis()
    key = namespaced("confirm", token)
    raw = await redis.get(key)
    if raw is None:
        return None
    await redis.delete(key)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:  # pragma: no cover - corrupt entry
        return None
    if data.get("actor_id") != str(actor_id):
        log.warning("admin.confirmation_actor_mismatch", token=token[:6], actor=str(actor_id))
        return None
    return data.get("action", ""), data.get("payload", {})
