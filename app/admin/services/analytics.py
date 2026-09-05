"""Admin dashboard metrics and analytics aggregation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutils import utcnow
from app.db.models.order import Order
from app.db.models.payment import PaymentIntent, PaymentProvider, VerificationAttempt
from app.db.models.user import User
from app.db.repositories.catalog import InventoryRepository
from app.db.repositories.orders import DeliveryRepository, OrderRepository
from app.db.repositories.payments import (
    PaymentIntentRepository,
    PaymentProviderRepository,
    ReconciliationRepository,
    VerificationAttemptRepository,
)
from app.db.repositories.support import SupportRepository
from app.domain.enums import OrderStatus, PaymentStatus


@dataclass(slots=True)
class DashboardSnapshot:
    """Everything the admin dashboard needs, in one pass (section 56)."""

    revenue_today: Decimal
    orders_today: int
    pending_payments: int
    manual_review: int
    low_stock: list[tuple[Any, int]]
    failed_deliveries: int
    open_tickets: int
    open_reconciliation: int
    providers_healthy: int
    providers_total: int
    new_users_today: int
    order_counts: dict[str, int] = field(default_factory=dict)

    @property
    def needs_attention(self) -> bool:
        return bool(
            self.manual_review
            or self.failed_deliveries
            or self.open_reconciliation
            or self.low_stock
            or self.providers_healthy < self.providers_total
        )


class AnalyticsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.orders = OrderRepository(session)
        self.intents = PaymentIntentRepository(session)
        self.providers = PaymentProviderRepository(session)
        self.deliveries = DeliveryRepository(session)
        self.inventory = InventoryRepository(session)
        self.support = SupportRepository(session)
        self.reconciliation = ReconciliationRepository(session)
        self.verifications = VerificationAttemptRepository(session)

    async def dashboard(self) -> DashboardSnapshot:
        since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        revenue = await self.orders.revenue_since(since)
        orders_today = await self.orders.count_since(since)
        order_counts = await self.orders.counts_by_status()
        payment_counts = await self.intents.counts_by_status()

        pending = sum(
            payment_counts.get(status.value, 0)
            for status in (
                PaymentStatus.AWAITING_PAYMENT,
                PaymentStatus.SUBMITTED,
                PaymentStatus.DETECTING,
                PaymentStatus.DETECTED,
                PaymentStatus.VERIFYING,
                PaymentStatus.PENDING_CONFIRMATION,
            )
        )
        review = payment_counts.get(PaymentStatus.UNDER_REVIEW.value, 0)

        low_stock = await self.inventory.low_stock_products(limit=5)
        failed = await self.deliveries.failed_count()
        tickets = await self.support.open_count()
        reconciliation = await self.reconciliation.open_count()

        providers = await self.providers.list_all()
        enabled = [p for p in providers if p.is_enabled]
        healthy = sum(1 for p in enabled if p.health_status == "healthy")

        new_users = int(
            (
                await self.session.scalar(
                    select(func.count(User.id)).where(User.created_at >= since)
                )
            )
            or 0
        )

        return DashboardSnapshot(
            revenue_today=revenue,
            orders_today=orders_today,
            pending_payments=pending,
            manual_review=review,
            low_stock=low_stock,
            failed_deliveries=failed,
            open_tickets=tickets,
            open_reconciliation=reconciliation,
            providers_healthy=healthy,
            providers_total=len(enabled),
            new_users_today=new_users,
            order_counts=order_counts,
        )

    async def revenue_series(self, days: int = 7) -> list[tuple[str, Decimal]]:
        """Daily paid revenue for the last ``days`` days."""
        series: list[tuple[str, Decimal]] = []
        today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in range(days - 1, -1, -1):
            start = today - timedelta(days=offset)
            end = start + timedelta(days=1)
            total = await self.session.scalar(
                select(func.coalesce(func.sum(Order.total), 0)).where(
                    Order.paid_at.is_not(None), Order.paid_at >= start, Order.paid_at < end
                )
            )
            series.append((start.strftime("%m-%d"), Decimal(str(total or 0))))
        return series

    async def payment_method_breakdown(self, days: int = 30) -> list[tuple[str, int, Decimal]]:
        since = utcnow() - timedelta(days=days)
        stmt = (
            select(
                PaymentIntent.provider_code,
                func.count(PaymentIntent.id),
                func.coalesce(func.sum(PaymentIntent.received_amount), 0),
            )
            .where(
                PaymentIntent.status == PaymentStatus.VERIFIED,
                PaymentIntent.verified_at >= since,
            )
            .group_by(PaymentIntent.provider_code)
        )
        rows = await self.session.execute(stmt)
        return [
            (code.value if hasattr(code, "value") else str(code), count, Decimal(str(total or 0)))
            for code, count, total in rows
        ]

    async def verification_stats(self, days: int = 7) -> dict[str, Any]:
        since = utcnow() - timedelta(days=days)
        stmt = (
            select(VerificationAttempt.outcome, func.count(VerificationAttempt.id))
            .where(VerificationAttempt.created_at >= since)
            .group_by(VerificationAttempt.outcome)
        )
        rows = await self.session.execute(stmt)
        outcomes = {
            (outcome.value if hasattr(outcome, "value") else str(outcome)): count
            for outcome, count in rows
        }
        latency = await self.verifications.average_latency_ms(since)
        total = sum(outcomes.values())
        verified = outcomes.get("verified", 0)
        return {
            "outcomes": outcomes,
            "total": total,
            "verified": verified,
            "success_rate": round(verified / total * 100, 1) if total else 0.0,
            "avg_latency_ms": latency,
        }

    async def conversion(self, days: int = 30) -> dict[str, Any]:
        since = utcnow() - timedelta(days=days)
        created = int(
            (await self.session.scalar(
                select(func.count(Order.id)).where(Order.created_at >= since)
            )) or 0
        )
        paid = int(
            (await self.session.scalar(
                select(func.count(Order.id)).where(
                    Order.created_at >= since, Order.paid_at.is_not(None)
                )
            )) or 0
        )
        completed = int(
            (await self.session.scalar(
                select(func.count(Order.id)).where(
                    Order.created_at >= since, Order.status == OrderStatus.COMPLETED
                )
            )) or 0
        )
        return {
            "orders": created,
            "paid": paid,
            "completed": completed,
            "paid_rate": round(paid / created * 100, 1) if created else 0.0,
            "completion_rate": round(completed / created * 100, 1) if created else 0.0,
        }

    async def provider_health(self) -> list[PaymentProvider]:
        return await self.providers.list_all()
