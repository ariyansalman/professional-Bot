"""Reseller, API key, webhook and idempotency repositories."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.security import (
    generate_api_key,
    generate_webhook_secret,
    get_secret_box,
    hash_api_key,
)
from app.core.timeutils import utcnow
from app.db.models.reseller import (
    ApiKey,
    IdempotencyRecord,
    ResellerAccount,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.db.repositories.base import BaseRepository, Page
from app.domain.enums import ApiScope, ResellerStatus, WebhookDeliveryStatus


class ResellerRepository(BaseRepository[ResellerAccount]):
    model = ResellerAccount

    async def get_for_user(self, user_id: uuid.UUID) -> ResellerAccount | None:
        stmt = (
            select(ResellerAccount)
            .where(ResellerAccount.user_id == user_id)
            .options(selectinload(ResellerAccount.user))
        )
        return await self.session.scalar(stmt)

    async def list_all(
        self, *, status: ResellerStatus | None = None, page: int = 1, per_page: int = 8
    ) -> Page[ResellerAccount]:
        stmt = (
            select(ResellerAccount)
            .options(selectinload(ResellerAccount.user))
            .order_by(ResellerAccount.created_at.desc())
        )
        if status is not None:
            stmt = stmt.where(ResellerAccount.status == status)
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def record_api_request(self, reseller_id: uuid.UUID) -> None:
        await self.session.execute(
            update(ResellerAccount)
            .where(ResellerAccount.id == reseller_id)
            .values(api_requests_count=ResellerAccount.api_requests_count + 1)
        )

    async def record_sale(self, reseller_id: uuid.UUID, amount: Any) -> None:
        await self.session.execute(
            update(ResellerAccount)
            .where(ResellerAccount.id == reseller_id)
            .values(
                orders_count=ResellerAccount.orders_count + 1,
                sales_total=ResellerAccount.sales_total + amount,
            )
        )


class ApiKeyRepository(BaseRepository[ApiKey]):
    model = ApiKey

    async def authenticate(self, plaintext: str) -> ApiKey | None:
        """Look a key up by its hash. Constant-time by construction: the hash
        is the index key, so no plaintext comparison happens at all."""
        stmt = (
            select(ApiKey)
            .where(ApiKey.key_hash == hash_api_key(plaintext))
            .options(selectinload(ApiKey.reseller).selectinload(ResellerAccount.user))
        )
        return await self.session.scalar(stmt)

    async def list_for_reseller(self, reseller_id: uuid.UUID) -> list[ApiKey]:
        stmt = (
            select(ApiKey)
            .where(ApiKey.reseller_id == reseller_id)
            .order_by(ApiKey.created_at.desc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def find_by_public_id(self, public_id: str) -> ApiKey | None:
        """Look a key up by its short public identifier.

        Admin search never touches key material: only the non-secret public id
        is searchable, because the plaintext key is not stored at all.
        """
        stmt = (
            select(ApiKey)
            .where(ApiKey.public_id == public_id.strip())
            .options(selectinload(ApiKey.reseller).selectinload(ResellerAccount.user))
        )
        return await self.session.scalar(stmt)

    async def active_count(self, reseller_id: uuid.UUID) -> int:
        stmt = select(func.count(ApiKey.id)).where(
            ApiKey.reseller_id == reseller_id, ApiKey.revoked_at.is_(None)
        )
        return int((await self.session.scalar(stmt)) or 0)

    async def create(
        self,
        *,
        reseller_id: uuid.UUID,
        name: str,
        scopes: list[ApiScope],
        live: bool = True,
        expires_in_days: int | None = None,
    ) -> tuple[ApiKey, str]:
        """Mint a key. The plaintext is returned once and never stored."""
        generated = generate_api_key(live=live)
        record = ApiKey(
            reseller_id=reseller_id,
            name=name[:64],
            public_id=generated.public_id,
            prefix=generated.prefix,
            key_hash=generated.hashed,
            scopes=[scope.value for scope in scopes],
            is_live=live,
            expires_at=utcnow() + timedelta(days=expires_in_days) if expires_in_days else None,
        )
        await self.add(record)
        return record, generated.plaintext

    async def revoke(self, key: ApiKey, revoked_by_id: uuid.UUID | None) -> None:
        key.revoked_at = utcnow()
        key.revoked_by_id = revoked_by_id
        await self.session.flush()

    async def touch(self, key_id: uuid.UUID, ip: str | None = None) -> None:
        await self.session.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id)
            .values(
                last_used_at=utcnow(),
                last_used_ip=(ip or "")[:64] or None,
                requests_count=ApiKey.requests_count + 1,
            )
        )


class WebhookRepository(BaseRepository[WebhookEndpoint]):
    model = WebhookEndpoint

    async def list_for_reseller(self, reseller_id: uuid.UUID) -> list[WebhookEndpoint]:
        stmt = (
            select(WebhookEndpoint)
            .where(WebhookEndpoint.reseller_id == reseller_id)
            .order_by(WebhookEndpoint.created_at)
        )
        return list((await self.session.scalars(stmt)).all())

    async def active_for_event(
        self, reseller_id: uuid.UUID, event: str
    ) -> list[WebhookEndpoint]:
        stmt = select(WebhookEndpoint).where(
            WebhookEndpoint.reseller_id == reseller_id,
            WebhookEndpoint.is_active.is_(True),
        )
        endpoints = list((await self.session.scalars(stmt)).all())
        # An empty events list means "subscribe to everything".
        return [e for e in endpoints if not e.events or event in e.events]

    async def create(
        self,
        *,
        reseller_id: uuid.UUID,
        url: str,
        events: list[str] | None = None,
        description: str | None = None,
    ) -> tuple[WebhookEndpoint, str]:
        secret = generate_webhook_secret()
        endpoint = WebhookEndpoint(
            reseller_id=reseller_id,
            url=url,
            encrypted_secret=get_secret_box().encrypt(secret),
            secret_hint=secret[:12],
            events=events or [],
            description=description,
        )
        await self.add(endpoint)
        return endpoint, secret

    def reveal_secret(self, endpoint: WebhookEndpoint) -> str:
        return get_secret_box().decrypt(endpoint.encrypted_secret)

    async def record_success(self, endpoint: WebhookEndpoint, status_code: int) -> None:
        endpoint.consecutive_failures = 0
        endpoint.last_success_at = utcnow()
        endpoint.last_status_code = status_code
        await self.session.flush()

    async def record_failure(
        self, endpoint: WebhookEndpoint, status_code: int | None, *, disable_after: int = 20
    ) -> None:
        endpoint.consecutive_failures += 1
        endpoint.last_failure_at = utcnow()
        endpoint.last_status_code = status_code
        if endpoint.consecutive_failures >= disable_after:
            endpoint.is_active = False
            endpoint.disabled_at = utcnow()
        await self.session.flush()


class WebhookDeliveryRepository(BaseRepository[WebhookDelivery]):
    model = WebhookDelivery

    async def enqueue(
        self,
        *,
        endpoint_id: uuid.UUID,
        event_id: str,
        event_type: str,
        payload: dict[str, Any],
        order_id: uuid.UUID | None = None,
    ) -> WebhookDelivery | None:
        """Queue one delivery. Unique on (endpoint, event) so a replayed
        internal event never produces a duplicate outbound call."""
        delivery = WebhookDelivery(
            endpoint_id=endpoint_id,
            event_id=event_id,
            event_type=event_type,
            payload=payload,
            order_id=order_id,
            next_attempt_at=utcnow(),
        )
        savepoint = await self.session.begin_nested()
        self.session.add(delivery)
        try:
            await self.session.flush()
        except IntegrityError:
            await savepoint.rollback()
            return None
        await savepoint.commit()
        return delivery

    async def due(self, *, limit: int = 50) -> list[WebhookDelivery]:
        stmt = (
            select(WebhookDelivery)
            .where(
                WebhookDelivery.status.in_(
                    [WebhookDeliveryStatus.PENDING, WebhookDeliveryStatus.FAILED]
                ),
                WebhookDelivery.next_attempt_at <= utcnow(),
            )
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def recent_for_reseller(
        self, reseller_id: uuid.UUID, *, limit: int = 10
    ) -> list[WebhookDelivery]:
        stmt = (
            select(WebhookDelivery)
            .join(WebhookEndpoint, WebhookDelivery.endpoint_id == WebhookEndpoint.id)
            .where(WebhookEndpoint.reseller_id == reseller_id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def failure_count(self, reseller_id: uuid.UUID) -> int:
        stmt = (
            select(func.count(WebhookDelivery.id))
            .join(WebhookEndpoint, WebhookDelivery.endpoint_id == WebhookEndpoint.id)
            .where(
                WebhookEndpoint.reseller_id == reseller_id,
                WebhookDelivery.status == WebhookDeliveryStatus.EXHAUSTED,
            )
        )
        return int((await self.session.scalar(stmt)) or 0)


class IdempotencyRepository(BaseRepository[IdempotencyRecord]):
    """Durable idempotency for reseller API writes."""

    model = IdempotencyRecord

    @staticmethod
    def fingerprint(payload: Any) -> str:
        import json

        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    async def get(self, scope: str, key: str) -> IdempotencyRecord | None:
        stmt = select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope, IdempotencyRecord.key == key
        )
        return await self.session.scalar(stmt)

    async def store(
        self,
        *,
        scope: str,
        key: str,
        request_fingerprint: str,
        response_status: int,
        response_body: dict[str, Any],
    ) -> IdempotencyRecord | None:
        ttl = get_settings().api.idempotency_ttl_seconds
        record = IdempotencyRecord(
            scope=scope,
            key=key,
            request_fingerprint=request_fingerprint,
            response_status=response_status,
            response_body=response_body,
            expires_at=utcnow() + timedelta(seconds=ttl),
        )
        savepoint = await self.session.begin_nested()
        self.session.add(record)
        try:
            await self.session.flush()
        except IntegrityError:
            await savepoint.rollback()
            return None
        await savepoint.commit()
        return record

    async def purge_expired(self) -> int:
        result = await self.session.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.expires_at <= utcnow())
        )
        return result.rowcount or 0
