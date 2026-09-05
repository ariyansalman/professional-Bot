"""Order lifecycle: creation, state transitions, cancellation and completion.

Order creation is a single transaction that either produces a fully consistent
order (pricing snapshot + line items + stock reservation + coupon redemption)
or nothing at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.correlation import get_correlation_id
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.money import quantize_money
from app.core.timeutils import utcnow
from app.db.models.catalog import Product
from app.db.models.order import Order, OrderItem
from app.db.models.user import User
from app.db.repositories.catalog import ProductRepository
from app.db.repositories.orders import CouponRepository, LedgerRepository, OrderRepository
from app.db.repositories.users import UserRepository
from app.domain.coupons.service import CouponService
from app.domain.enums import DeliveryType, LedgerEntryType, OrderStatus, ProductStatus
from app.domain.inventory.service import InventoryService
from app.domain.state_machines import assert_order_transition

log = get_logger(__name__)


@dataclass(slots=True)
class PricingQuote:
    """The immutable price snapshot shown on checkout and stored on the order.

    What the customer sees on the checkout screen is exactly what is written to
    the order row - the total is never recomputed from live product data later.
    """

    product: Product
    quantity: int
    unit_price: Decimal
    subtotal: Decimal
    discount: Decimal
    total: Decimal
    currency: str
    coupon_code: str | None = None
    coupon_id: uuid.UUID | None = None


class OrderService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.orders = OrderRepository(session)
        self.products = ProductRepository(session)
        self.users = UserRepository(session)
        self.coupons = CouponRepository(session)
        self.ledger = LedgerRepository(session)
        self.inventory = InventoryService(session)
        self.coupon_service = CouponService(session)

    # -- pricing -----------------------------------------------------------

    async def quote(
        self,
        *,
        product: Product,
        quantity: int,
        user: User | None = None,
        coupon_code: str | None = None,
        is_reseller: bool = False,
    ) -> PricingQuote:
        """Compute the checkout total. Raises if the purchase is not possible."""
        if product.status is not ProductStatus.ACTIVE:
            raise ValidationError(
                f"product {product.id} is {product.status}",
                safe_message="This product is not available.",
            )
        await self.inventory.assert_purchasable(product, quantity)

        unit_price = self._unit_price(product, is_reseller=is_reseller)
        subtotal = quantize_money(unit_price * quantity)
        discount = Decimal("0")
        coupon_id: uuid.UUID | None = None
        resolved_code: str | None = None

        if coupon_code and user is not None and get_settings().features.coupons_enabled:
            evaluation = await self.coupon_service.evaluate(
                code=coupon_code,
                user_id=user.id,
                subtotal=subtotal,
                currency=product.currency,
                product_id=product.id,
                category_id=product.category_id,
                is_reseller=is_reseller,
            )
            discount = evaluation.discount
            coupon_id = evaluation.coupon.id
            resolved_code = evaluation.coupon.code

        return PricingQuote(
            product=product,
            quantity=quantity,
            unit_price=unit_price,
            subtotal=subtotal,
            discount=discount,
            total=quantize_money(subtotal - discount),
            currency=product.currency,
            coupon_code=resolved_code,
            coupon_id=coupon_id,
        )

    @staticmethod
    def _unit_price(product: Product, *, is_reseller: bool) -> Decimal:
        """Resellers pay the configured wholesale price when one is set."""
        if is_reseller and product.reseller_price is not None:
            return quantize_money(product.reseller_price)
        return quantize_money(product.price)

    # -- creation ----------------------------------------------------------

    async def create_order(
        self,
        *,
        quote: PricingQuote,
        user: User | None = None,
        reseller_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
        customer_reference: str | None = None,
        reseller_reference: str | None = None,
        channel: str = "telegram",
        metadata: dict[str, Any] | None = None,
    ) -> Order:
        """Create an order atomically.

        Reserves stock and redeems the coupon inside the same transaction, so
        an order can never exist with unreserved stock or an unconsumed coupon.
        """
        if user is None and reseller_id is None:
            raise ValidationError("an order needs either a user or a reseller")

        reference = await self._next_reference()
        correlation_id = get_correlation_id()

        order = Order(
            reference=reference,
            user_id=user.id if user else None,
            reseller_id=reseller_id,
            idempotency_key=idempotency_key,
            customer_reference=customer_reference,
            reseller_reference=reseller_reference,
            status=OrderStatus.CREATED,
            currency=quote.currency,
            subtotal=quote.subtotal,
            discount_total=quote.discount,
            total=quote.total,
            coupon_id=quote.coupon_id,
            coupon_code=quote.coupon_code,
            correlation_id=correlation_id,
            channel=channel,
            order_metadata=metadata or {},
        )
        # The line items are attached before the first flush so the ``items``
        # collection is populated in memory: touching it later on a persistent
        # object would trigger a lazy load, which is not permitted in async.
        item = OrderItem(
            product_id=quote.product.id,
            product_name=quote.product.name,
            product_sku=quote.product.sku,
            delivery_type=quote.product.delivery_type.value,
            quantity=quote.quantity,
            unit_price=quote.unit_price,
            line_total=quantize_money(quote.unit_price * quote.quantity),
            currency=quote.currency,
            product_snapshot=self._snapshot(quote.product),
        )
        order.items = [item]
        await self.orders.add(order)

        await self.inventory.reserve(
            product=quote.product,
            quantity=quote.quantity,
            order_id=order.id,
            order_item_id=item.id,
        )

        if quote.coupon_id is not None and user is not None:
            coupon = await self.coupons.get(quote.coupon_id)
            if coupon is not None:
                await self.coupon_service.redeem(
                    coupon=coupon,
                    user_id=user.id,
                    order_id=order.id,
                    discount=quote.discount,
                )

        if user is not None:
            user.orders_count += 1

        await self.ledger.record(
            entry_type=LedgerEntryType.ORDER_CREATED,
            amount=Decimal("0"),
            currency=order.currency,
            dedupe_key=f"order_created:{order.id}",
            order_id=order.id,
            user_id=user.id if user else None,
            description=f"Order {reference} created",
            details={
                "total": str(order.total),
                "quantity": quote.quantity,
                "product": quote.product.sku,
            },
            correlation_id=correlation_id,
        )

        await self.session.flush()
        log.info(
            "order.created",
            order_id=str(order.id),
            reference=reference,
            total=str(order.total),
            currency=order.currency,
            channel=channel,
        )
        return order

    async def _next_reference(self) -> str:
        """Human-readable, collision-checked order reference."""
        prefix = get_settings().payments.order_reference_prefix
        sequence = await self.orders.next_reference_sequence()
        for offset in range(50):
            candidate = f"{prefix}-{10000 + sequence + offset}"
            if await self.orders.get_by_reference(candidate) is None:
                return candidate
        # Fall back to a random suffix rather than blocking the purchase.
        return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"

    @staticmethod
    def _snapshot(product: Product) -> dict[str, Any]:
        """Everything needed to render this line item years from now."""
        return {
            "id": str(product.id),
            "sku": product.sku,
            "name": product.name,
            "short_description": product.short_description,
            "price": str(product.price),
            "currency": product.currency,
            "delivery_type": product.delivery_type.value,
            "category": product.category.name_en if product.category else None,
            "captured_at": utcnow().isoformat(),
        }

    # -- transitions -------------------------------------------------------

    async def transition(
        self,
        order: Order,
        target: OrderStatus,
        *,
        reason: str | None = None,
    ) -> Order:
        """Move an order to a new status through the guarded transition table."""
        assert_order_transition(order.status, target)
        previous = order.status
        order.status = target
        now = utcnow()

        if target is OrderStatus.PAYMENT_VERIFIED and order.paid_at is None:
            order.paid_at = now
        elif target is OrderStatus.DELIVERED and order.delivered_at is None:
            order.delivered_at = now
        elif target is OrderStatus.COMPLETED and order.completed_at is None:
            order.completed_at = now
        elif target is OrderStatus.CANCELLED:
            order.cancelled_at = now
            order.cancellation_reason = (reason or "")[:255] or None
        elif target is OrderStatus.MANUAL_REVIEW:
            order.review_reason = (reason or "")[:255] or None

        await self.session.flush()
        log.info(
            "order.transitioned",
            order_id=str(order.id),
            reference=order.reference,
            **{"from": previous.value, "to": target.value},
            reason=reason,
        )
        return order

    async def cancel(self, order: Order, *, reason: str, actor_id: uuid.UUID | None = None) -> Order:
        """Cancel an unpaid order and give back stock and the coupon.

        A paid order is never cancelled: money that arrived is refunded through
        the refund flow, which keeps its own audit trail.
        """
        if order.status.is_paid:
            raise ConflictError(
                f"order {order.reference} is paid and cannot be cancelled; issue a refund",
                safe_message="This order has already been paid. Please contact support.",
            )
        await self.transition(order, OrderStatus.CANCELLED, reason=reason)
        await self.inventory.release_for_order(order.id, reason="order cancelled")
        await self.coupon_service.revert(order.id)
        log.info(
            "order.cancelled",
            order_id=str(order.id),
            reference=order.reference,
            reason=reason,
            actor_id=str(actor_id) if actor_id else None,
        )
        return order

    async def expire(self, order: Order) -> Order:
        """Expire an unpaid order whose payment window lapsed."""
        if order.status.is_paid or order.status.is_terminal:
            return order
        await self.transition(order, OrderStatus.EXPIRED, reason="payment window expired")
        await self.inventory.release_for_order(order.id, reason="payment expired")
        await self.coupon_service.revert(order.id)
        return order

    async def complete(self, order: Order) -> Order:
        """Finalise a delivered order and update the customer's lifetime stats."""
        if order.status is OrderStatus.COMPLETED:
            return order
        await self.transition(order, OrderStatus.COMPLETED)
        if order.user_id is not None:
            user = await self.users.get(order.user_id)
            if user is not None:
                await self.users.record_purchase(user, order.total)
        for item in order.items:
            if item.product_id:
                await self.products.record_sale(item.product_id, item.quantity)
        return order

    async def get_or_404(self, order_id: uuid.UUID) -> Order:
        order = await self.orders.get_with_items(order_id)
        if order is None:
            raise NotFoundError(f"order {order_id} not found", safe_message="Order not found.")
        return order

    @staticmethod
    def requires_delivery(order: Order) -> bool:
        return any(
            item.delivery_type != DeliveryType.MANUAL.value for item in order.items
        )
