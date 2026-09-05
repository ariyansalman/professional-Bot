"""Payment verification, expiry and provider health workers."""

from __future__ import annotations

from aiogram import Bot

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis import distributed_lock
from app.db.repositories.payments import PaymentIntentRepository, PaymentProviderRepository
from app.db.session import session_scope
from app.domain.payments.registry import build_adapter
from app.domain.payments.service import PaymentService
from app.workers.base import PeriodicWorker

log = get_logger(__name__)


class PaymentVerificationWorker(PeriodicWorker):
    """Polls open payment intents and verifies them against their provider.

    Concurrency safety: each intent is processed under an advisory Redis lock so
    replicas do not duplicate provider calls. Correctness does not depend on
    that lock - the ``payment_consumptions`` unique constraint is what actually
    prevents a transaction being credited twice.
    """

    name = "payment_verification"

    def __init__(self, bot: Bot | None = None) -> None:
        super().__init__(interval=get_settings().payments.verification_poll_interval)
        self.bot = bot

    async def run_once(self) -> int:
        processed = 0
        async with session_scope() as session:
            intents = await PaymentIntentRepository(session).due_for_polling(limit=25)
            intent_ids = [intent.id for intent in intents]

        # Each intent gets its own transaction so one failure cannot roll back
        # a payment that was legitimately verified in the same batch.
        for intent_id in intent_ids:
            if self.is_stopping:
                break
            processed += await self._verify_one(intent_id)
        return processed

    async def _verify_one(self, intent_id) -> int:
        async with distributed_lock(f"payment:{intent_id}", ttl=120) as acquired:
            if not acquired:
                return 0
            try:
                async with session_scope() as session:
                    payments = PaymentService(session)
                    intent = await payments.intents.get_full(intent_id)
                    if intent is None or not intent.status.is_open:
                        return 0

                    result = await payments.verify(intent, triggered_by="worker")

                    if result.newly_verified:
                        from app.workers.delivery.dispatcher import enqueue_delivery

                        await enqueue_delivery(session, intent.order_id)
                    if self.bot is not None and (
                        result.newly_verified or result.needs_review
                    ):
                        from app.workers.notifications.dispatcher import notify_payment_result

                        await notify_payment_result(session, self.bot, intent, result)
                    return 1
            except Exception:
                log.exception("worker.verification_failed", intent_id=str(intent_id))
                return 0


class PaymentExpiryWorker(PeriodicWorker):
    """Expires payment windows and releases the stock they were holding."""

    name = "payment_expiry"
    interval = 60.0

    def __init__(self, bot: Bot | None = None) -> None:
        super().__init__()
        self.bot = bot

    async def run_once(self) -> int:
        async with session_scope() as session:
            payments = PaymentService(session)
            expired = await payments.expire_due(limit=50)

            # Orders whose last chance to pay has lapsed release their stock and
            # give the coupon back. The money side stays untouched: nothing was
            # ever credited.
            from app.db.repositories.orders import OrderRepository
            from app.domain.orders.service import OrderService

            orders = OrderRepository(session)
            service = OrderService(session)
            released = 0
            for order in await orders.expired_pending(limit=50):
                active = await payments.intents.active_for_order(order.id)
                if active is not None:
                    continue  # a newer intent is still payable
                await service.expire(order)
                released += 1

            if expired or released:
                log.info("worker.payments_expired", intents=expired, orders=released)
            return expired + released


class ReservationReaperWorker(PeriodicWorker):
    """Returns lapsed inventory reservations to available stock."""

    name = "reservation_reaper"
    interval = 120.0

    async def run_once(self) -> int:
        async with session_scope() as session:
            from app.domain.inventory.service import InventoryService

            return await InventoryService(session).reap_expired(limit=200)


class ProviderHealthWorker(PeriodicWorker):
    """Probes every enabled provider so the admin panel shows live health."""

    name = "provider_health"
    interval = 300.0

    async def run_once(self) -> int:
        checked = 0
        async with session_scope() as session:
            repo = PaymentProviderRepository(session)
            providers = await repo.list_enabled()

            from app.db.repositories.payments import PaymentMethodRepository

            methods = await PaymentMethodRepository(session).list_all()

            for provider in providers:
                if self.is_stopping:
                    break
                method = next((m for m in methods if m.provider_id == provider.id), None)
                adapter = None
                try:
                    adapter = build_adapter(provider, method)
                    health = await adapter.health_check()
                    await repo.record_health(
                        provider,
                        healthy=health.healthy,
                        latency_ms=health.latency_ms,
                        message=health.message,
                    )
                except Exception as exc:
                    await repo.record_health(
                        provider, healthy=False, latency_ms=0, message=str(exc)[:200]
                    )
                    log.warning(
                        "worker.provider_health_failed",
                        provider=provider.code.value,
                        error=str(exc)[:200],
                    )
                finally:
                    if adapter is not None:
                        await adapter.aclose()
                checked += 1
        return checked
