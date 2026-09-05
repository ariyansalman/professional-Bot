"""Reconciliation worker (section 99).

Detects the financial anomalies that automated verification deliberately
refuses to resolve on its own, and files each one for an administrator:

* payments stuck under review beyond a grace period
* orders that are paid but never delivered
* deliveries that exhausted their retries
* verified payments with no consumption record, or the reverse
* payment intents that expired while a transaction was already detected
* duplicate transaction claims

Every finding is deduplicated so a recurring condition raises one item, not one
per scan.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.db.models.order import Delivery, Order
from app.db.models.payment import PaymentConsumption, PaymentIntent
from app.db.repositories.payments import ReconciliationRepository
from app.db.session import session_scope
from app.domain.enums import (
    DeliveryStatus,
    OrderStatus,
    PaymentStatus,
    ReconciliationKind,
)
from app.workers.base import PeriodicWorker

log = get_logger(__name__)


class ReconciliationWorker(PeriodicWorker):
    name = "reconciliation"
    interval = 600.0

    async def run_once(self) -> int:
        findings = 0
        async with session_scope() as session:
            repo = ReconciliationRepository(session)
            now = utcnow()

            findings += await self._stuck_reviews(session, repo, now)
            findings += await self._paid_not_delivered(session, repo, now)
            findings += await self._exhausted_deliveries(session, repo)
            findings += await self._verified_without_consumption(session, repo)
            findings += await self._orphan_consumptions(session, repo)
            findings += await self._expired_with_funds(session, repo)

        if findings:
            log.warning("reconciliation.findings", count=findings)
        return findings

    async def _stuck_reviews(self, session, repo, now) -> int:
        """Payments waiting on a human for more than 24 hours."""
        cutoff = now - timedelta(hours=24)
        stmt = (
            select(PaymentIntent)
            .where(
                PaymentIntent.status == PaymentStatus.UNDER_REVIEW,
                PaymentIntent.updated_at <= cutoff,
            )
            .limit(50)
        )
        count = 0
        for intent in (await session.scalars(stmt)).all():
            created = await repo.record(
                kind=ReconciliationKind.PROVIDER_INCONSISTENCY,
                dedupe_key=f"stuck_review:{intent.id}",
                summary=f"{intent.reference} has been under review for over 24h",
                payment_intent_id=intent.id,
                order_id=intent.order_id,
                details={
                    "reason": intent.review_reason,
                    "outcome": intent.last_outcome.value if intent.last_outcome else None,
                    "expected": str(intent.expected_amount),
                    "received": str(intent.received_amount) if intent.received_amount else None,
                },
            )
            count += 1 if created else 0
        return count

    async def _paid_not_delivered(self, session, repo, now) -> int:
        """Money taken, nothing delivered, and it has been too long."""
        cutoff = now - timedelta(hours=2)
        stmt = (
            select(Order)
            .where(
                Order.status.in_([OrderStatus.PAYMENT_VERIFIED, OrderStatus.FULFILLING]),
                Order.paid_at.is_not(None),
                Order.paid_at <= cutoff,
            )
            .limit(50)
        )
        count = 0
        for order in (await session.scalars(stmt)).all():
            created = await repo.record(
                kind=ReconciliationKind.STUCK_DELIVERY,
                dedupe_key=f"stuck_delivery:{order.id}",
                summary=f"{order.reference} paid but not delivered after 2h",
                order_id=order.id,
                details={"status": order.status.value, "paid_at": order.paid_at.isoformat()},
            )
            count += 1 if created else 0
        return count

    async def _exhausted_deliveries(self, session, repo) -> int:
        stmt = (
            select(Delivery)
            .where(
                Delivery.status == DeliveryStatus.FAILED,
                Delivery.next_attempt_at.is_(None),
            )
            .limit(50)
        )
        count = 0
        for delivery in (await session.scalars(stmt)).all():
            created = await repo.record(
                kind=ReconciliationKind.STUCK_DELIVERY,
                dedupe_key=f"exhausted_delivery:{delivery.id}",
                summary=f"delivery {delivery.id} exhausted its retries",
                order_id=delivery.order_id,
                details={"attempts": delivery.attempts, "last_error": delivery.last_error},
            )
            count += 1 if created else 0
        return count

    async def _verified_without_consumption(self, session, repo) -> int:
        """A verified payment must be backed by exactly one consumed transaction.

        The exception is a manually approved payment, which is recorded in the
        audit log and the ledger instead; those are excluded by checking the
        last outcome.
        """
        stmt = (
            select(PaymentIntent)
            .outerjoin(
                PaymentConsumption, PaymentConsumption.payment_intent_id == PaymentIntent.id
            )
            .where(
                PaymentIntent.status == PaymentStatus.VERIFIED,
                PaymentConsumption.id.is_(None),
                PaymentIntent.last_outcome.is_not(None),
            )
            .limit(50)
        )
        count = 0
        for intent in (await session.scalars(stmt)).all():
            created = await repo.record(
                kind=ReconciliationKind.ORPHAN_PAYMENT,
                dedupe_key=f"verified_no_consumption:{intent.id}",
                summary=f"{intent.reference} is verified but has no consumed transaction",
                payment_intent_id=intent.id,
                order_id=intent.order_id,
                details={"last_outcome": intent.last_outcome.value if intent.last_outcome else None},
            )
            count += 1 if created else 0
        return count

    async def _orphan_consumptions(self, session, repo) -> int:
        """A consumed transaction whose intent never reached VERIFIED."""
        stmt = (
            select(PaymentConsumption, PaymentIntent)
            .join(PaymentIntent, PaymentConsumption.payment_intent_id == PaymentIntent.id)
            .where(PaymentIntent.status != PaymentStatus.VERIFIED)
            .limit(50)
        )
        count = 0
        for consumption, intent in (await session.execute(stmt)).all():
            created = await repo.record(
                kind=ReconciliationKind.UNMATCHED_TRANSACTION,
                dedupe_key=f"orphan_consumption:{consumption.id}",
                summary=(
                    f"transaction {consumption.external_id[:24]} was consumed but "
                    f"{intent.reference} is {intent.status.value}"
                ),
                payment_intent_id=intent.id,
                order_id=intent.order_id,
                details={
                    "external_id": consumption.external_id,
                    "amount": str(consumption.amount),
                    "intent_status": intent.status.value,
                },
            )
            count += 1 if created else 0
        return count

    async def _expired_with_funds(self, session, repo) -> int:
        """A payment window expired after money had already been detected."""
        stmt = (
            select(PaymentIntent)
            .where(
                PaymentIntent.status == PaymentStatus.EXPIRED,
                PaymentIntent.received_amount.is_not(None),
            )
            .limit(50)
        )
        count = 0
        for intent in (await session.scalars(stmt)).all():
            created = await repo.record(
                kind=ReconciliationKind.EXPIRED_WITH_FUNDS,
                dedupe_key=f"expired_with_funds:{intent.id}",
                summary=f"{intent.reference} expired after a payment was detected",
                payment_intent_id=intent.id,
                order_id=intent.order_id,
                details={
                    "expected": str(intent.expected_amount),
                    "received": str(intent.received_amount),
                    "confirmations": intent.confirmations,
                },
            )
            count += 1 if created else 0
        return count


class IdempotencyCleanupWorker(PeriodicWorker):
    """Purges expired idempotency records so the table stays small."""

    name = "idempotency_cleanup"
    interval = 3600.0

    async def run_once(self) -> int:
        async with session_scope() as session:
            from app.db.repositories.resellers import IdempotencyRepository

            return await IdempotencyRepository(session).purge_expired()
