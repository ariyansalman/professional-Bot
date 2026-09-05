"""Delivery worker: fulfils paid orders and hands the goods to the customer."""

from __future__ import annotations

import uuid

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.redis import distributed_lock
from app.db.repositories.orders import DeliveryRepository, OrderRepository
from app.db.session import session_scope
from app.domain.enums import DeliveryStatus, Language, NotificationKind, WebhookEvent
from app.i18n import t
from app.domain.orders.delivery import DeliveryService
from app.workers.base import PeriodicWorker

log = get_logger(__name__)


async def enqueue_delivery(session: AsyncSession, order_id: uuid.UUID) -> int:
    """Create the delivery rows for a freshly paid order.

    Safe to call repeatedly: :meth:`DeliveryService.prepare` is idempotent and
    refuses outright if the order has no verified payment.
    """
    order = await OrderRepository(session).get_with_items(order_id)
    if order is None:
        return 0
    service = DeliveryService(session)
    deliveries = await service.prepare(order)
    log.info("delivery.enqueued", order=order.reference, count=len(deliveries))
    return len(deliveries)


class DeliveryWorker(PeriodicWorker):
    """Fulfils due deliveries and pushes the product to the customer.

    A failure here never touches the payment: the money stays credited, the
    delivery backs off and retries, and after the configured attempts the order
    is escalated to manual review with the customer told their payment is safe.
    """

    name = "delivery"
    interval = 15.0

    def __init__(self, bot: Bot | None = None) -> None:
        super().__init__()
        self.bot = bot

    async def run_once(self) -> int:
        async with session_scope() as session:
            due = await DeliveryRepository(session).due(limit=25)
            delivery_ids = [delivery.id for delivery in due]

        processed = 0
        for delivery_id in delivery_ids:
            if self.is_stopping:
                break
            processed += await self._deliver_one(delivery_id)
        return processed

    async def _deliver_one(self, delivery_id: uuid.UUID) -> int:
        async with distributed_lock(f"delivery:{delivery_id}", ttl=120) as acquired:
            if not acquired:
                return 0

            payload = None
            order_reference = None
            telegram_id = None
            product_name = "-"
            order_id = None
            language = Language.EN

            async with session_scope() as session:
                repo = DeliveryRepository(session)
                delivery = await repo.get(delivery_id)
                if delivery is None or delivery.status is DeliveryStatus.COMPLETED:
                    return 0

                service = DeliveryService(session)
                try:
                    payload = await service.fulfil(delivery)
                except Exception as exc:  # noqa: BLE001 - recorded, never fatal
                    log.warning(
                        "delivery.attempt_failed",
                        delivery_id=str(delivery_id),
                        error=str(exc)[:300],
                    )
                    await service.record_failure(delivery, str(exc))
                    return 0

                order = await OrderRepository(session).get_with_items(delivery.order_id)
                if order is not None:
                    order_id = order.id
                    order_reference = order.reference
                    product_name = order.items[0].product_name if order.items else "-"
                    if order.user is not None and not order.user.is_bot_blocked:
                        telegram_id = order.user.telegram_id
                        language = order.user.language
                    await service.finalise_order(order)

                    from app.domain.referrals.service import ReferralService

                    if order.status.value == "completed":
                        await ReferralService(session).qualify_order(order)

                    from app.db.repositories.users import NotificationRepository

                    if order.user_id is not None:
                        await NotificationRepository(session).create(
                            order.user_id,
                            kind=NotificationKind.DELIVERY,
                            title=f"Order {order.reference} delivered",
                            body="Your product is ready in the bot.",
                            payload={"order_id": str(order.id)},
                        )

            # Notifications happen outside the transaction: a Telegram failure
            # must never roll back a completed delivery.
            if self.bot is not None and telegram_id and payload is not None:
                await self._notify(
                    telegram_id, order_reference, product_name, payload, language
                )

            if order_id is not None:
                await self._dispatch_webhooks(order_id)
            return 1

    async def _notify(
        self,
        telegram_id: int,
        reference,
        product_name: str,
        payload,
        language: Language,
    ) -> None:
        from app.bot.services.formatting import delivered_product, esc

        try:
            if payload.items:
                text = delivered_product(
                    product_name=product_name,
                    payloads=payload.items,
                    order_reference=reference or "-",
                    lang=language,
                )
            else:
                text = "\n".join(
                    [
                        t("delivery.ready_title", language),
                        "",
                        f"{t('payment.order', language)}: #{esc(reference or '-')}",
                        f"{t('checkout.product', language)}: {esc(product_name)}",
                        "",
                        t("product.manual_delivery", language),
                    ]
                )
            await self.bot.send_message(telegram_id, text)
            if payload.file_id:
                await self.bot.send_document(telegram_id, payload.file_id)
        except Exception:  # noqa: BLE001 - delivery is already recorded
            log.warning("delivery.notify_failed", telegram_id=telegram_id)

    async def _dispatch_webhooks(self, order_id: uuid.UUID) -> None:
        async with session_scope() as session:
            order = await OrderRepository(session).get_with_items(order_id)
            if order is None or order.reseller_id is None:
                return
            from app.domain.resellers.service import ResellerService

            service = ResellerService(session)
            body = {
                "order_id": str(order.id),
                "reference": order.reference,
                "status": order.status.value,
            }
            await service.dispatch_event(
                event=WebhookEvent.DELIVERY_COMPLETED, order=order, payload=body
            )
            if order.status.value == "completed":
                await service.dispatch_event(
                    event=WebhookEvent.ORDER_COMPLETED, order=order, payload=body
                )
