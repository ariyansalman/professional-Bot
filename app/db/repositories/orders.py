"""Order, coupon, delivery, refund and ledger repositories."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core.timeutils import utcnow
from app.db.models.order import (
    Coupon,
    CouponUsage,
    Delivery,
    LedgerEntry,
    Order,
    OrderItem,
    Refund,
)
from app.db.repositories.base import BaseRepository, Page
from app.domain.enums import DeliveryStatus, LedgerEntryType, OrderStatus, RefundStatus


class OrderRepository(BaseRepository[Order]):
    model = Order

    async def get_with_items(self, order_id: uuid.UUID) -> Order | None:
        stmt = (
            select(Order)
            .where(Order.id == order_id)
            .options(selectinload(Order.items), selectinload(Order.user))
        )
        return await self.session.scalar(stmt)

    async def get_by_reference(self, reference: str) -> Order | None:
        stmt = (
            select(Order)
            .where(Order.reference == reference.strip().upper())
            .options(selectinload(Order.items), selectinload(Order.user))
        )
        return await self.session.scalar(stmt)

    async def get_by_idempotency_key(
        self, reseller_id: uuid.UUID, key: str
    ) -> Order | None:
        stmt = (
            select(Order)
            .where(Order.reseller_id == reseller_id, Order.idempotency_key == key)
            .options(selectinload(Order.items))
        )
        return await self.session.scalar(stmt)

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        *,
        statuses: list[OrderStatus] | None = None,
        page: int = 1,
        per_page: int = 5,
    ) -> Page[Order]:
        stmt = (
            select(Order)
            .where(Order.user_id == user_id)
            .options(selectinload(Order.items))
            .order_by(Order.created_at.desc())
        )
        if statuses:
            stmt = stmt.where(Order.status.in_(statuses))
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def list_for_reseller(
        self,
        reseller_id: uuid.UUID,
        *,
        statuses: list[OrderStatus] | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> Page[Order]:
        stmt = (
            select(Order)
            .where(Order.reseller_id == reseller_id)
            .options(selectinload(Order.items))
            .order_by(Order.created_at.desc())
        )
        if statuses:
            stmt = stmt.where(Order.status.in_(statuses))
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def list_for_admin(
        self,
        *,
        statuses: list[OrderStatus] | None = None,
        query: str | None = None,
        page: int = 1,
        per_page: int = 8,
    ) -> Page[Order]:
        stmt = (
            select(Order)
            .options(selectinload(Order.items), selectinload(Order.user))
            .order_by(Order.created_at.desc())
        )
        if statuses:
            stmt = stmt.where(Order.status.in_(statuses))
        if query:
            term = query.strip()
            stmt = stmt.where(
                or_(
                    Order.reference.ilike(f"%{term}%"),
                    Order.customer_reference.ilike(f"%{term}%"),
                    Order.reseller_reference.ilike(f"%{term}%"),
                )
            )
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def active_for_user(self, user_id: uuid.UUID) -> Order | None:
        """The order the customer should be nudged to finish paying."""
        stmt = (
            select(Order)
            .where(
                Order.user_id == user_id,
                Order.status.in_([OrderStatus.CREATED, OrderStatus.PAYMENT_PENDING]),
            )
            .options(selectinload(Order.items))
            .order_by(Order.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def next_reference_sequence(self) -> int:
        """Monotonic sequence backing the human-readable order reference."""
        stmt = select(func.count(Order.id))
        return int((await self.session.scalar(stmt)) or 0) + 1

    async def counts_by_status(self) -> dict[str, int]:
        stmt = select(Order.status, func.count(Order.id)).group_by(Order.status)
        rows = await self.session.execute(stmt)
        return {
            (status.value if hasattr(status, "value") else str(status)): count
            for status, count in rows
        }

    async def revenue_since(self, since: datetime) -> Decimal:
        stmt = select(func.coalesce(func.sum(Order.total), 0)).where(
            Order.paid_at.is_not(None), Order.paid_at >= since
        )
        return Decimal(str((await self.session.scalar(stmt)) or 0))

    async def count_since(self, since: datetime) -> int:
        stmt = select(func.count(Order.id)).where(Order.created_at >= since)
        return int((await self.session.scalar(stmt)) or 0)

    async def expired_pending(self, *, limit: int = 100) -> list[Order]:
        """Orders whose payment window lapsed and were never paid."""
        from app.db.models.payment import PaymentIntent

        stmt = (
            select(Order)
            .join(PaymentIntent, PaymentIntent.order_id == Order.id)
            .where(
                Order.status.in_([OrderStatus.CREATED, OrderStatus.PAYMENT_PENDING]),
                PaymentIntent.expires_at <= utcnow(),
            )
            .options(selectinload(Order.items))
            .distinct()
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def add_item(self, order: Order, **kwargs: Any) -> OrderItem:
        item = OrderItem(order_id=order.id, **kwargs)
        self.session.add(item)
        await self.session.flush()
        return item


class CouponRepository(BaseRepository[Coupon]):
    model = Coupon

    async def get_by_code(self, code: str) -> Coupon | None:
        stmt = select(Coupon).where(Coupon.code == code.strip().upper())
        return await self.session.scalar(stmt)

    async def list_all(self, *, page: int = 1, per_page: int = 8) -> Page[Coupon]:
        stmt = select(Coupon).order_by(Coupon.created_at.desc())
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def usage_count_for_user(self, coupon_id: uuid.UUID, user_id: uuid.UUID) -> int:
        stmt = select(func.count(CouponUsage.id)).where(
            CouponUsage.coupon_id == coupon_id,
            CouponUsage.user_id == user_id,
            CouponUsage.reverted_at.is_(None),
        )
        return int((await self.session.scalar(stmt)) or 0)

    async def redeem(
        self,
        *,
        coupon: Coupon,
        user_id: uuid.UUID,
        order_id: uuid.UUID,
        discount_amount: Decimal,
    ) -> CouponUsage | None:
        """Record a redemption.

        The UNIQUE constraint on ``order_id`` makes this idempotent: a retried
        checkout for the same order cannot consume a second redemption.
        """
        usage = CouponUsage(
            coupon_id=coupon.id,
            user_id=user_id,
            order_id=order_id,
            discount_amount=discount_amount,
        )
        self.session.add(usage)
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            return None
        await self.session.execute(
            update(Coupon)
            .where(Coupon.id == coupon.id)
            .values(redemptions_count=Coupon.redemptions_count + 1)
        )
        return usage

    async def revert(self, order_id: uuid.UUID) -> None:
        """Give a redemption back when an order is cancelled or expires."""
        usage = await self.session.scalar(
            select(CouponUsage).where(
                CouponUsage.order_id == order_id, CouponUsage.reverted_at.is_(None)
            )
        )
        if usage is None:
            return
        usage.reverted_at = utcnow()
        await self.session.execute(
            update(Coupon)
            .where(Coupon.id == usage.coupon_id, Coupon.redemptions_count > 0)
            .values(redemptions_count=Coupon.redemptions_count - 1)
        )
        await self.session.flush()


class DeliveryRepository(BaseRepository[Delivery]):
    model = Delivery

    async def get_for_order_item(self, order_item_id: uuid.UUID) -> Delivery | None:
        stmt = select(Delivery).where(Delivery.order_item_id == order_item_id)
        return await self.session.scalar(stmt)

    async def list_for_order(self, order_id: uuid.UUID) -> list[Delivery]:
        stmt = (
            select(Delivery)
            .where(Delivery.order_id == order_id)
            .order_by(Delivery.created_at)
        )
        return list((await self.session.scalars(stmt)).all())

    async def due(self, *, limit: int = 50) -> list[Delivery]:
        now = utcnow()
        stmt = (
            select(Delivery)
            .where(
                Delivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
                or_(Delivery.next_attempt_at.is_(None), Delivery.next_attempt_at <= now),
            )
            .order_by(Delivery.created_at)
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def failed_count(self) -> int:
        stmt = select(func.count(Delivery.id)).where(Delivery.status == DeliveryStatus.FAILED)
        return int((await self.session.scalar(stmt)) or 0)


class RefundRepository(BaseRepository[Refund]):
    model = Refund

    async def list_for_order(self, order_id: uuid.UUID) -> list[Refund]:
        stmt = select(Refund).where(Refund.order_id == order_id).order_by(Refund.created_at)
        return list((await self.session.scalars(stmt)).all())

    async def list_pending(self, *, page: int = 1, per_page: int = 8) -> Page[Refund]:
        stmt = (
            select(Refund)
            .where(
                Refund.status.in_(
                    [RefundStatus.REQUESTED, RefundStatus.APPROVED, RefundStatus.PROCESSING]
                )
            )
            .order_by(Refund.created_at)
        )
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def refunded_total(self, order_id: uuid.UUID) -> Decimal:
        stmt = select(func.coalesce(func.sum(Refund.amount), 0)).where(
            Refund.order_id == order_id,
            Refund.status.in_([RefundStatus.COMPLETED, RefundStatus.PROCESSING]),
        )
        return Decimal(str((await self.session.scalar(stmt)) or 0))


class LedgerRepository(BaseRepository[LedgerEntry]):
    model = LedgerEntry

    async def record(
        self,
        *,
        entry_type: LedgerEntryType,
        amount: Decimal,
        dedupe_key: str,
        currency: str = "USDT",
        order_id: uuid.UUID | None = None,
        payment_intent_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        description: str = "",
        details: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> LedgerEntry | None:
        """Append a journal entry.

        Returns ``None`` when ``dedupe_key`` already exists, which makes the
        ledger safe to call from a retried worker: the financial event is
        journalled exactly once.
        """
        existing = await self.session.scalar(
            select(LedgerEntry).where(LedgerEntry.dedupe_key == dedupe_key)
        )
        if existing is not None:
            return None
        entry = LedgerEntry(
            entry_type=entry_type,
            amount=amount,
            currency=currency,
            dedupe_key=dedupe_key,
            order_id=order_id,
            payment_intent_id=payment_intent_id,
            user_id=user_id,
            actor_id=actor_id,
            description=description,
            details=details or {},
            correlation_id=correlation_id,
        )
        self.session.add(entry)
        try:
            await self.session.flush()
        except IntegrityError:
            # Lost a concurrent race: the entry now exists, which is the goal.
            await self.session.rollback()
            return None
        return entry

    async def list_for_order(self, order_id: uuid.UUID) -> list[LedgerEntry]:
        stmt = (
            select(LedgerEntry)
            .where(LedgerEntry.order_id == order_id)
            .order_by(LedgerEntry.created_at)
        )
        return list((await self.session.scalars(stmt)).all())

    async def net_total(self, *, since: datetime | None = None) -> Decimal:
        stmt = select(func.coalesce(func.sum(LedgerEntry.amount), 0))
        if since is not None:
            stmt = stmt.where(LedgerEntry.created_at >= since)
        return Decimal(str((await self.session.scalar(stmt)) or 0))
