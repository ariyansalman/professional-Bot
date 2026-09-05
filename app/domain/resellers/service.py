"""Reseller accounts, API keys and webhook fan-out."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, PermissionDeniedError, ValidationError
from app.core.logging import get_logger
from app.core.security import assert_safe_outbound_url
from app.core.timeutils import utcnow
from app.db.models.order import Order
from app.db.models.reseller import ApiKey, ResellerAccount, WebhookEndpoint
from app.db.models.user import User
from app.db.repositories.resellers import (
    ApiKeyRepository,
    ResellerRepository,
    WebhookDeliveryRepository,
    WebhookRepository,
)
from app.domain.enums import ApiScope, ResellerStatus, WebhookEvent

log = get_logger(__name__)

TERMS_VERSION = "1.0"

TERMS_TEXT = """<b>RESELLER TERMS</b>

1. You may resell only products marked as available to resellers.
2. You must not resell below the configured minimum price.
3. API keys are personal to your account and must not be shared.
4. You are responsible for your own customers and their support.
5. Payment verification, delivery and refunds remain governed by this platform.
6. Abuse, fraud or chargebacks may result in suspension without notice.
7. Rate limits apply to all API endpoints and may change.
8. We may revoke reseller access at any time for policy violations."""

#: Scopes a reseller may grant themselves. Administrative and financial scopes
#: are deliberately absent: they can never be self-granted from the bot.
SELF_SERVICE_SCOPES: tuple[ApiScope, ...] = (
    ApiScope.PRODUCTS_READ,
    ApiScope.ORDERS_CREATE,
    ApiScope.ORDERS_READ,
    ApiScope.PAYMENTS_READ,
    ApiScope.DELIVERIES_READ,
    ApiScope.WEBHOOKS_MANAGE,
)


@dataclass(slots=True)
class CreatedApiKey:
    record: ApiKey
    #: Shown exactly once, then discarded.
    plaintext: str


class ResellerService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.resellers = ResellerRepository(session)
        self.keys = ApiKeyRepository(session)
        self.webhooks = WebhookRepository(session)
        self.deliveries = WebhookDeliveryRepository(session)

    # -- activation --------------------------------------------------------

    async def get_account(self, user: User) -> ResellerAccount | None:
        return await self.resellers.get_for_user(user.id)

    async def activate(self, user: User) -> ResellerAccount:
        """Create or approve a reseller account.

        Whether activation is immediate or needs admin approval is a
        configuration decision, never a hard-coded one.
        """
        settings = get_settings()
        if not settings.features.reseller_enabled:
            raise PermissionDeniedError(
                "reseller programme is disabled",
                safe_message="The reseller programme is not available right now.",
            )

        account = await self.resellers.get_for_user(user.id)
        if account is not None:
            if account.status is ResellerStatus.SUSPENDED:
                raise PermissionDeniedError(
                    f"reseller {account.id} is suspended",
                    safe_message="Your reseller account is suspended. Please contact support.",
                )
            if account.status is ResellerStatus.REVOKED:
                raise PermissionDeniedError(
                    f"reseller {account.id} is revoked",
                    safe_message="Your reseller access has been revoked.",
                )
            return account

        status = (
            ResellerStatus.ACTIVE
            if settings.features.reseller_self_activation
            else ResellerStatus.PENDING
        )
        account = ResellerAccount(
            user_id=user.id,
            business_name=user.display_name,
            status=status,
            terms_accepted_at=utcnow(),
            terms_version=TERMS_VERSION,
            rate_limit_per_minute=settings.api.default_rate_limit_per_minute,
        )
        self.session.add(account)
        await self.session.flush()
        log.info(
            "reseller.activated",
            reseller_id=str(account.id),
            user_id=str(user.id),
            status=status.value,
        )
        return account

    async def suspend(
        self, *, account: ResellerAccount, reason: str, actor_id: uuid.UUID | None
    ) -> None:
        account.status = ResellerStatus.SUSPENDED
        account.suspended_reason = reason[:255]
        account.suspended_at = utcnow()
        # Suspension revokes live credentials immediately.
        for key in await self.keys.list_for_reseller(account.id):
            if not key.is_revoked:
                await self.keys.revoke(key, actor_id)
        await self.session.flush()
        log.warning(
            "reseller.suspended", reseller_id=str(account.id), reason=reason, actor=str(actor_id)
        )

    async def reinstate(self, account: ResellerAccount) -> None:
        account.status = ResellerStatus.ACTIVE
        account.suspended_reason = None
        account.suspended_at = None
        await self.session.flush()
        log.info("reseller.reinstated", reseller_id=str(account.id))

    # -- api keys ----------------------------------------------------------

    async def create_api_key(
        self,
        *,
        account: ResellerAccount,
        name: str,
        scopes: list[ApiScope],
        live: bool = True,
    ) -> CreatedApiKey:
        if not account.is_active:
            raise PermissionDeniedError(
                f"reseller {account.id} is {account.status}",
                safe_message="Your reseller account is not active.",
            )
        requested = set(scopes) or {ApiScope.PRODUCTS_READ}
        forbidden = requested - set(SELF_SERVICE_SCOPES)
        if forbidden:
            raise PermissionDeniedError(
                f"scopes not self-grantable: {sorted(s.value for s in forbidden)}",
                safe_message="One or more selected scopes are not available.",
            )
        if await self.keys.active_count(account.id) >= 10:
            raise ConflictError(
                "api key limit reached",
                safe_message="You have reached the maximum number of active API keys.",
            )

        record, plaintext = await self.keys.create(
            reseller_id=account.id, name=name, scopes=sorted(requested, key=lambda s: s.value), live=live
        )
        log.info(
            "reseller.api_key_created",
            reseller_id=str(account.id),
            key_id=str(record.id),
            public_id=record.public_id,
            scopes=[s.value for s in requested],
        )
        return CreatedApiKey(record=record, plaintext=plaintext)

    async def revoke_api_key(
        self, *, account: ResellerAccount, key_id: uuid.UUID, actor_id: uuid.UUID | None
    ) -> ApiKey:
        key = await self.keys.get(key_id)
        if key is None or key.reseller_id != account.id:
            raise PermissionDeniedError(
                "api key does not belong to this reseller",
                safe_message="That API key was not found.",
            )
        if key.is_revoked:
            return key
        await self.keys.revoke(key, actor_id)
        log.info("reseller.api_key_revoked", key_id=str(key.id), reseller_id=str(account.id))
        return key

    # -- webhooks ----------------------------------------------------------

    async def register_webhook(
        self,
        *,
        account: ResellerAccount,
        url: str,
        events: list[str] | None = None,
        description: str | None = None,
    ) -> tuple[WebhookEndpoint, str]:
        # SSRF guard: reject loopback/private/metadata targets before storing.
        assert_safe_outbound_url(url)
        valid = {event.value for event in WebhookEvent}
        selected = [event for event in (events or []) if event in valid]
        if events and not selected:
            raise ValidationError(
                f"no valid events in {events}", safe_message="No valid webhook events selected."
            )
        endpoint, secret = await self.webhooks.create(
            reseller_id=account.id, url=url, events=selected, description=description
        )
        log.info(
            "reseller.webhook_registered",
            reseller_id=str(account.id),
            endpoint_id=str(endpoint.id),
            events=selected or ["*"],
        )
        return endpoint, secret

    async def dispatch_event(
        self,
        *,
        event: WebhookEvent,
        order: Order,
        payload: dict[str, Any],
    ) -> int:
        """Queue an event to every subscribed endpoint of the order's reseller.

        The event id is derived from (event, order, status) so re-processing the
        same state change cannot enqueue a duplicate delivery.
        """
        if order.reseller_id is None:
            return 0
        endpoints = await self.webhooks.active_for_event(order.reseller_id, event.value)
        if not endpoints:
            return 0

        event_id = f"evt_{event.value.replace('.', '_')}_{order.id.hex[:12]}"
        body = {
            "id": event_id,
            "type": event.value,
            "created_at": datetime.now(UTC).isoformat(),
            "data": payload,
        }
        queued = 0
        for endpoint in endpoints:
            delivery = await self.deliveries.enqueue(
                endpoint_id=endpoint.id,
                event_id=event_id,
                event_type=event.value,
                payload=body,
                order_id=order.id,
            )
            if delivery is not None:
                queued += 1
        log.info(
            "reseller.webhook_queued",
            event_type=event.value,
            order=order.reference,
            queued=queued,
            endpoints=len(endpoints),
        )
        return queued

    # -- pricing -----------------------------------------------------------

    @staticmethod
    def pricing_summary(product: Any) -> dict[str, str | None]:
        """The three price points a reseller needs (section 54)."""
        return {
            "wholesale_price": str(product.reseller_price) if product.reseller_price else None,
            "minimum_price": str(product.reseller_min_price)
            if product.reseller_min_price
            else None,
            "recommended_price": str(product.reseller_recommended_price)
            if product.reseller_recommended_price
            else None,
            "currency": product.currency,
        }

    async def dashboard_stats(self, account: ResellerAccount) -> dict[str, Any]:
        keys = await self.keys.list_for_reseller(account.id)
        endpoints = await self.webhooks.list_for_reseller(account.id)
        failures = await self.deliveries.failure_count(account.id)
        healthy = all(e.health == "healthy" for e in endpoints if e.is_active)
        return {
            "status": account.status.value,
            "api_requests": account.api_requests_count,
            "orders": account.orders_count,
            "sales_total": account.sales_total or Decimal("0"),
            "sales_currency": account.sales_currency,
            "active_keys": sum(1 for k in keys if not k.is_revoked),
            "webhook_count": len(endpoints),
            "webhook_health": "healthy" if healthy and endpoints else ("none" if not endpoints else "degraded"),
            "webhook_failures": failures,
        }
