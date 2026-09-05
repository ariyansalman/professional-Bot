"""Delivery: turning a verified payment into a fulfilled order.

Guarantees:

* Delivery only ever runs for an order whose payment is verified. The check is
  made against the payment record, not against the order status alone.
* Delivery is idempotent per order item, enforced by the UNIQUE constraint on
  ``deliveries.order_item_id``. A retried worker re-sends the same payload; it
  never allocates new stock.
* A delivery failure never reverses a payment. The money stays credited and the
  delivery retries with backoff, escalating to manual review if it exhausts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.security import get_secret_box
from app.core.timeutils import utcnow
from app.db.models.order import Delivery, Order, OrderItem
from app.db.repositories.catalog import ProductRepository
from app.db.repositories.orders import DeliveryRepository, OrderRepository
from app.db.repositories.payments import PaymentIntentRepository
from app.domain.enums import DeliveryStatus, DeliveryType, OrderStatus
from app.domain.inventory.service import InventoryService

log = get_logger(__name__)


@dataclass(slots=True)
class DeliveredPayload:
    """What the customer receives. Never logged."""

    delivery_type: DeliveryType
    items: list[str]
    file_id: str | None = None
    instructions: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.items and not self.file_id


class DeliveryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.deliveries = DeliveryRepository(session)
        self.orders = OrderRepository(session)
        self.products = ProductRepository(session)
        self.intents = PaymentIntentRepository(session)
        self.inventory = InventoryService(session)

    async def assert_paid(self, order: Order) -> None:
        """Refuse to fulfil anything without an independently verified payment.

        This is checked against ``payment_intents`` rather than the order
        status, so a corrupted or manually edited order status alone can never
        cause an unpaid order to be delivered.
        """
        verified = await self.intents.verified_for_order(order.id)
        if verified is None:
            raise ConflictError(
                f"order {order.reference} has no verified payment; delivery refused",
                safe_message="This order is not paid yet.",
            )

    async def prepare(self, order: Order) -> list[Delivery]:
        """Create the delivery rows for a paid order. Idempotent."""
        await self.assert_paid(order)

        deliveries: list[Delivery] = []
        for item in order.items:
            existing = await self.deliveries.get_for_order_item(item.id)
            if existing is not None:
                deliveries.append(existing)
                continue
            delivery = Delivery(
                order_id=order.id,
                order_item_id=item.id,
                delivery_type=item.delivery_type,
                status=DeliveryStatus.PENDING,
                next_attempt_at=utcnow(),
                correlation_id=order.correlation_id,
            )
            self.session.add(delivery)
            try:
                await self.session.flush()
            except IntegrityError:
                # Another worker created it first: adopt theirs.
                await self.session.rollback()
                existing = await self.deliveries.get_for_order_item(item.id)
                if existing is not None:
                    deliveries.append(existing)
                    continue
                raise
            deliveries.append(delivery)

        if order.status is OrderStatus.PAYMENT_VERIFIED:
            from app.domain.orders.service import OrderService

            await OrderService(self.session).transition(order, OrderStatus.FULFILLING)
        return deliveries

    async def fulfil(self, delivery: Delivery) -> DeliveredPayload:
        """Allocate and materialise the payload for one delivery.

        Idempotent: an already-completed delivery returns its stored payload
        rather than allocating again.
        """
        if delivery.status is DeliveryStatus.COMPLETED:
            return self._decode(delivery)

        order = await self.orders.get_with_items(delivery.order_id)
        if order is None:
            raise NotFoundError(f"order {delivery.order_id} missing for delivery {delivery.id}")
        await self.assert_paid(order)

        item = next((i for i in order.items if i.id == delivery.order_item_id), None)
        if item is None:
            raise NotFoundError(f"order item {delivery.order_item_id} missing")

        delivery.status = DeliveryStatus.PROCESSING
        delivery.attempts += 1
        await self.session.flush()

        payload = await self._build_payload(order, item, delivery)

        box = get_secret_box()
        delivery.encrypted_payload = box.encrypt("\n".join(payload.items)) if payload.items else None
        delivery.file_id = payload.file_id
        delivery.status = DeliveryStatus.COMPLETED
        delivery.delivered_at = utcnow()
        delivery.last_error = None
        await self.session.flush()

        log.info(
            "delivery.completed",
            delivery_id=str(delivery.id),
            order=order.reference,
            delivery_type=payload.delivery_type.value,
            item_count=len(payload.items),
        )
        return payload

    async def _build_payload(
        self, order: Order, item: OrderItem, delivery: Delivery
    ) -> DeliveredPayload:
        delivery_type = DeliveryType(item.delivery_type)
        product = await self.products.get_with_media(item.product_id) if item.product_id else None

        if delivery_type is DeliveryType.STOCK_ITEM:
            if product is None:
                raise NotFoundError(
                    f"product {item.product_id} was removed; order {order.reference} "
                    "needs manual fulfilment"
                )
            allocated = await self.inventory.allocate_for_order_item(
                order_id=order.id,
                order_item_id=item.id,
                quantity=item.quantity,
                product=product,
            )
            delivery.inventory_item_ids = [str(i.id) for i in allocated]
            payloads = [self.inventory.reveal_payload(i) for i in allocated]
            return DeliveredPayload(
                delivery_type=delivery_type,
                items=payloads,
                instructions=product.delivery_instructions,
            )

        if delivery_type is DeliveryType.STATIC_PAYLOAD:
            body = (product.delivery_payload if product else None) or ""
            return DeliveredPayload(
                delivery_type=delivery_type,
                items=[body] * item.quantity if body else [],
                instructions=product.delivery_instructions if product else None,
            )

        if delivery_type is DeliveryType.FILE:
            return DeliveredPayload(
                delivery_type=delivery_type,
                items=[],
                file_id=product.delivery_file_id if product else None,
                instructions=product.delivery_instructions if product else None,
            )

        # MANUAL: an operator fulfils it from the admin panel.
        return DeliveredPayload(
            delivery_type=DeliveryType.MANUAL,
            items=[],
            instructions=(product.delivery_instructions if product else None)
            or "Our team will complete this order shortly.",
        )

    def _decode(self, delivery: Delivery) -> DeliveredPayload:
        items: list[str] = []
        if delivery.encrypted_payload:
            items = get_secret_box().decrypt(delivery.encrypted_payload).split("\n")
        return DeliveredPayload(
            delivery_type=DeliveryType(delivery.delivery_type),
            items=items,
            file_id=delivery.file_id,
        )

    def reveal(self, delivery: Delivery) -> DeliveredPayload:
        """Re-read a delivered payload for the customer's product screen."""
        if delivery.status is not DeliveryStatus.COMPLETED:
            raise ConflictError(
                f"delivery {delivery.id} is {delivery.status}",
                safe_message="Your product is not ready yet.",
            )
        if delivery.first_viewed_at is None:
            delivery.first_viewed_at = utcnow()
        return self._decode(delivery)

    async def record_failure(self, delivery: Delivery, error: str) -> None:
        """Back off and retry. The payment is never reversed for this.

        Backoff is exponential and capped; once attempts are exhausted the
        order goes to manual review with the money still credited.
        """
        settings = get_settings()
        delivery.status = DeliveryStatus.FAILED
        delivery.last_error = error[:512]
        max_attempts = settings.payments.delivery_max_attempts

        if delivery.attempts >= max_attempts:
            delivery.next_attempt_at = None
            order = await self.orders.get_with_items(delivery.order_id)
            if order is not None and order.status is not OrderStatus.MANUAL_REVIEW:
                from app.domain.orders.service import OrderService

                service = OrderService(self.session)
                target = (
                    OrderStatus.DELIVERY_FAILED
                    if order.status is OrderStatus.FULFILLING
                    else OrderStatus.MANUAL_REVIEW
                )
                await service.transition(order, target, reason=f"delivery failed: {error[:120]}")
            log.error(
                "delivery.exhausted",
                delivery_id=str(delivery.id),
                order_id=str(delivery.order_id),
                attempts=delivery.attempts,
                error=error[:200],
            )
        else:
            backoff = min(2**delivery.attempts * 30, 3600)
            delivery.next_attempt_at = utcnow() + timedelta(seconds=backoff)
            log.warning(
                "delivery.retry_scheduled",
                delivery_id=str(delivery.id),
                attempts=delivery.attempts,
                retry_in_seconds=backoff,
                error=error[:200],
            )
        await self.session.flush()

    async def finalise_order(self, order: Order) -> bool:
        """Mark the order delivered/completed once every line item is done."""
        deliveries = await self.deliveries.list_for_order(order.id)
        if not deliveries:
            return False
        if any(d.status is not DeliveryStatus.COMPLETED for d in deliveries):
            return False

        from app.domain.orders.service import OrderService

        service = OrderService(self.session)
        if order.status in (OrderStatus.FULFILLING, OrderStatus.DELIVERY_FAILED):
            await service.transition(order, OrderStatus.DELIVERED)
        if order.status is OrderStatus.DELIVERED:
            await service.complete(order)
        return True

    async def status_summary(self, order_id: uuid.UUID) -> dict[str, Any]:
        deliveries = await self.deliveries.list_for_order(order_id)
        if not deliveries:
            return {"total": 0, "completed": 0, "failed": 0, "ready": False}
        completed = sum(1 for d in deliveries if d.status is DeliveryStatus.COMPLETED)
        failed = sum(1 for d in deliveries if d.status is DeliveryStatus.FAILED)
        return {
            "total": len(deliveries),
            "completed": completed,
            "failed": failed,
            "ready": completed == len(deliveries),
        }
