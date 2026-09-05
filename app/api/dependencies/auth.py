"""API key authentication, scope enforcement and rate limiting.

Authentication chain for every reseller request:

1. Bearer token is parsed and hashed; the hash is the lookup key, so the
   plaintext is never compared or stored.
2. The key must not be revoked or expired.
3. The reseller account must be ACTIVE.
4. The caller's IP must be in the account's allowlist, when one is configured.
5. The endpoint's required scope must be granted to that key.
6. A per-key rate limit is applied.

Failures are deliberately uniform: an unknown key, a revoked key and a
suspended account all return the same 401, so the API cannot be used to probe
which keys exist.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError, PermissionDeniedError, RateLimitedError
from app.core.logging import get_logger
from app.core.redis import rate_limit
from app.core.security import parse_api_key
from app.core.timeutils import utcnow
from app.db.models.reseller import ApiKey, ResellerAccount
from app.db.repositories.resellers import ApiKeyRepository
from app.db.session import session_scope
from app.domain.enums import ApiScope

log = get_logger(__name__)

_GENERIC_AUTH_FAILURE = "Invalid or inactive API key."


@dataclass(slots=True)
class APIPrincipal:
    """The authenticated caller."""

    api_key: ApiKey
    reseller: ResellerAccount
    scopes: frozenset[ApiScope]
    ip: str | None

    def require(self, scope: ApiScope) -> None:
        if scope not in self.scopes:
            raise PermissionDeniedError(
                f"api key {self.api_key.public_id} lacks scope {scope.value}",
                safe_message=f"This API key does not have the '{scope.value}' scope.",
            )


async def get_session() -> AsyncSession:
    """Request-scoped transactional session."""
    async with session_scope() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def client_ip(request: Request) -> str | None:
    """Resolve the caller's IP.

    ``X-Forwarded-For`` is only trusted for the left-most entry because the
    platform runs behind a single known proxy; anything further left is
    attacker-controlled.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def authenticate(
    request: Request,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> APIPrincipal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError(
            "missing bearer token", safe_message="Provide an API key as 'Authorization: Bearer ...'."
        )
    token = authorization[7:].strip()
    try:
        parse_api_key(token)
    except Exception as exc:  # noqa: BLE001 - malformed keys are just invalid
        raise AuthenticationError(
            f"malformed api key: {exc}", safe_message=_GENERIC_AUTH_FAILURE
        ) from exc

    repo = ApiKeyRepository(session)
    api_key = await repo.authenticate(token)
    if api_key is None:
        log.info("api.auth_failed", reason="unknown_key")
        raise AuthenticationError("unknown api key", safe_message=_GENERIC_AUTH_FAILURE)
    if api_key.revoked_at is not None:
        log.info("api.auth_failed", reason="revoked", key=api_key.public_id)
        raise AuthenticationError("revoked api key", safe_message=_GENERIC_AUTH_FAILURE)
    if api_key.expires_at is not None and api_key.expires_at <= utcnow():
        log.info("api.auth_failed", reason="expired", key=api_key.public_id)
        raise AuthenticationError("expired api key", safe_message=_GENERIC_AUTH_FAILURE)

    reseller = api_key.reseller
    if reseller is None or not reseller.is_active:
        log.info("api.auth_failed", reason="inactive_reseller", key=api_key.public_id)
        raise AuthenticationError("inactive reseller", safe_message=_GENERIC_AUTH_FAILURE)

    ip = client_ip(request)
    if reseller.ip_allowlist and not _ip_allowed(ip, reseller.ip_allowlist):
        log.warning(
            "api.ip_not_allowed", key=api_key.public_id, reseller=str(reseller.id), ip=ip
        )
        raise PermissionDeniedError(
            f"ip {ip} not in allowlist", safe_message="Your IP address is not allowed."
        )

    limit = api_key.rate_limit_per_minute or reseller.rate_limit_per_minute or (
        get_settings().api.default_rate_limit_per_minute
    )
    allowed, retry_after = await rate_limit(
        f"api:{api_key.id}", limit, 60, raise_on_limit=False
    )
    if not allowed:
        log.info("api.rate_limited", key=api_key.public_id, limit=limit)
        raise RateLimitedError(
            f"rate limit {limit}/min exceeded",
            safe_message="Rate limit exceeded. Please slow down.",
            retry_after=retry_after,
        )

    await repo.touch(api_key.id, ip)
    from app.db.repositories.resellers import ResellerRepository

    await ResellerRepository(session).record_api_request(reseller.id)

    scopes = frozenset(
        scope for scope in (_parse_scope(value) for value in api_key.scopes) if scope is not None
    )
    request.state.principal_id = api_key.public_id
    return APIPrincipal(api_key=api_key, reseller=reseller, scopes=scopes, ip=ip)


def _parse_scope(value: str) -> ApiScope | None:
    try:
        return ApiScope(value)
    except ValueError:
        return None


def _ip_allowed(ip: str | None, allowlist: list) -> bool:
    if not ip:
        return False
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if "/" in str(entry):
                if address in ipaddress.ip_network(str(entry), strict=False):
                    return True
            elif address == ipaddress.ip_address(str(entry)):
                return True
        except ValueError:
            continue
    return False


PrincipalDep = Annotated[APIPrincipal, Depends(authenticate)]


def require_scope(scope: ApiScope):
    """Dependency factory enforcing a scope on an endpoint."""

    async def _dependency(principal: PrincipalDep) -> APIPrincipal:
        principal.require(scope)
        return principal

    return _dependency
