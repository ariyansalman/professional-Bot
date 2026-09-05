"""Idempotency for reseller write endpoints.

Guarantees:

* The same ``Idempotency-Key`` with the same body returns the original
  response instead of performing the operation twice.
* The same key with a *different* body is rejected with 409 rather than
  silently returning an unrelated result.
* Records are persisted in PostgreSQL, so idempotency survives a Redis flush
  or a process restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import get_session
from app.core.exceptions import IdempotencyConflictError
from app.core.logging import get_logger
from app.db.repositories.resellers import IdempotencyRepository

log = get_logger(__name__)


@dataclass(slots=True)
class IdempotencyGuard:
    session: AsyncSession
    key: str | None
    scope: str

    async def replay(self, payload: Any) -> dict[str, Any] | None:
        """Return the stored response when this exact request was already made."""
        if not self.key:
            return None
        repo = IdempotencyRepository(self.session)
        record = await repo.get(self.scope, self.key)
        if record is None:
            return None
        fingerprint = repo.fingerprint(payload)
        if record.request_fingerprint != fingerprint:
            raise IdempotencyConflictError(
                f"idempotency key {self.key} reused with a different payload",
                safe_message=(
                    "This Idempotency-Key was already used with a different request body."
                ),
            )
        log.info("api.idempotent_replay", scope=self.scope, key=self.key)
        return record.response_body

    async def store(self, payload: Any, response: dict[str, Any], status: int = 201) -> None:
        if not self.key:
            return
        repo = IdempotencyRepository(self.session)
        await repo.store(
            scope=self.scope,
            key=self.key,
            request_fingerprint=repo.fingerprint(payload),
            response_status=status,
            response_body=response,
        )


def idempotency(scope: str):
    """Dependency factory binding an idempotency scope to an endpoint.

    It depends on the same ``get_session`` callable the route uses, so
    FastAPI's dependency cache hands back the *same* session and the
    idempotency record is committed in the same transaction as the work it
    describes. A stored response can therefore never exist for an operation
    that rolled back.
    """

    async def _dependency(
        session: Annotated[AsyncSession, Depends(get_session)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> IdempotencyGuard:
        key = (idempotency_key or "").strip()[:128] or None
        return IdempotencyGuard(session=session, key=key, scope=scope)

    return _dependency
