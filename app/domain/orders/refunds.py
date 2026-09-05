"""Refunds.

Refunds are deliberately separate from payment verification (section 104). A
verified payment is never rewound: the money arrived, and that fact stays in the
ledger. A refund is a second, independent financial event with its own record,
its own approval and its own audit trail.

The platform does not move money automatically. Sending crypto requires
withdrawal-capable credentials, which this platform never asks for and never
stores. A refund is therefore *recorded* here and *executed* by an operator from
their own wallet or exchange, who then attaches the external reference. That is
a documented limitation, not an omission: automating it would mean holding keys
that can drain the business.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.money import quantize_money
from app.core.timeutils import utcnow
from app.db.models.order import Order, Refund
from app.db.repositories.orders import LedgerRepository, OrderRepository, RefundRepository
from app.db.repositories.payments import PaymentIntentRepository
from app.db.repositories.users import NotificationRepository
from app.domain.enums import (
    LedgerEntryType,
    NotificationKind,
    OrderStatus,
    RefundStatus,
)

log = get_logger(__name__)


class RefundService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.refunds = RefundRepository(session)
        self.orders = OrderRepository(session)
        self.intents = PaymentIntentRepository(session)
        self.ledger = LedgerRepository(session)
        self.notifications = NotificationRepository(session)

    async def request(
        self,
        *,
        order: Order,
        amount: Decimal | None,
        reason: str,
        requested_by_id: uuid.UUID,
        destination: str | None = None,
        destination_network: str | None = None,
    ) -> Refund:
        """Record a refund request against a paid order.

        Refuses to record more than was actually received, so the refund ledger
        can never claim the business returned money it never took.
        """
        if not order.status.is_paid:
            raise ConflictError(
                f"order {order.reference} was never paid; there is nothing to refund",
                safe_message="This order has no payment to refund.",
            )

        intent = await self.intents.verified_for_order(order.id)
        if intent is None:
            raise ConflictError(
                f"order {order.reference} has no verified payment",
                safe_message="This order has no verified payment to refund.",
            )

        paid = intent.received_amount or intent.expected_amount
        already = await self.refunds.refunded_total(order.id)
        remaining = quantize_money(paid - already)
        if remaining <= 0:
            raise ConflictError(
                f"order {order.reference} is already fully refunded ({already} of {paid})",
                safe_message="This order has already been fully refunded.",
            )

        requested = quantize_money(amount) if amount is not None else remaining
        if requested <= 0:
            raise ValidationError(
                "refund amount must be positive",
                safe_message="The refund amount must be greater than zero.",
            )
        if requested > remaining:
            raise ValidationError(
                f"refund {requested} exceeds the refundable remainder {remaining}",
                safe_message=f"At most {remaining} {intent.asset} can still be refunded.",
            )

        refund = Refund(
            order_id=order.id,
            payment_intent_id=intent.id,
            amount=requested,
            currency=intent.asset,
            status=RefundStatus.REQUESTED,
            reason=reason[:512],
            destination=destination,
            destination_network=destination_network,
            requested_by_id=requested_by_id,
        )
        await self.refunds.add(refund)
        log.info(
            "refund.requested",
            refund_id=str(refund.id),
            order=order.reference,
            amount=str(requested),
            currency=intent.asset,
        )
        return refund

    async def approve(self, *, refund: Refund, actor_id: uuid.UUID) -> Refund:
        if refund.status is not RefundStatus.REQUESTED:
            raise ConflictError(
                f"refund {refund.id} is {refund.status}",
                safe_message="This refund cannot be approved in its current state.",
            )
        refund.status = RefundStatus.APPROVED
        refund.approved_by_id = actor_id
        await self.session.flush()
        log.info("refund.approved", refund_id=str(refund.id), actor=str(actor_id))
        return refund

    async def reject(self, *, refund: Refund, actor_id: uuid.UUID, reason: str) -> Refund:
        if refund.status in (RefundStatus.COMPLETED, RefundStatus.PROCESSING):
            raise ConflictError(
                f"refund {refund.id} is already {refund.status}",
                safe_message="This refund is already being processed.",
            )
        refund.status = RefundStatus.REJECTED
        refund.approved_by_id = actor_id
        refund.notes = reason[:512]
        await self.session.flush()
        log.info("refund.rejected", refund_id=str(refund.id), actor=str(actor_id))
        return refund

    async def complete(
        self, *, refund: Refund, actor_id: uuid.UUID, external_reference: str
    ) -> Refund:
        """Mark an executed refund complete and journal it.

        The external reference is the operator's proof that the transfer
        actually happened; it is required, so a refund can never be closed with
        no evidence behind it.
        """
        if refund.status is RefundStatus.COMPLETED:
            return refund
        if refund.status not in (RefundStatus.APPROVED, RefundStatus.PROCESSING):
            raise ConflictError(
                f"refund {refund.id} is {refund.status} and cannot be completed",
                safe_message="This refund must be approved first.",
            )
        if not external_reference.strip():
            raise ValidationError(
                "an external reference is required to complete a refund",
                safe_message="Enter the transaction reference for the refund you sent.",
            )

        refund.status = RefundStatus.COMPLETED
        refund.external_reference = external_reference.strip()[:255]
        refund.processed_at = utcnow()
        await self.session.flush()

        order = await self.orders.get_with_items(refund.order_id)

        # Negative amount: money leaving the business. Journalled once.
        await self.ledger.record(
            entry_type=LedgerEntryType.REFUND,
            amount=-refund.amount,
            currency=refund.currency,
            dedupe_key=f"refund:{refund.id}",
            order_id=refund.order_id,
            payment_intent_id=refund.payment_intent_id,
            user_id=order.user_id if order else None,
            actor_id=actor_id,
            description=f"Refund for order {order.reference if order else refund.order_id}",
            details={
                "reason": refund.reason,
                "external_reference": refund.external_reference,
                "destination_network": refund.destination_network,
            },
            correlation_id=order.correlation_id if order else None,
        )

        # A fully refunded order is marked as such; a partial refund leaves the
        # order where it is, because the customer still keeps what they bought.
        if order is not None:
            total_refunded = await self.refunds.refunded_total(order.id)
            intent = await self.intents.verified_for_order(order.id)
            paid = (intent.received_amount or intent.expected_amount) if intent else order.total
            if total_refunded >= paid:
                from app.domain.orders.service import OrderService

                try:
                    await OrderService(self.session).transition(order, OrderStatus.REFUNDED)
                except Exception as exc:
                    log.warning(
                        "refund.order_transition_failed",
                        order=order.reference,
                        detail=str(exc)[:200],
                    )

            if order.user_id is not None:
                await self.notifications.create(
                    order.user_id,
                    kind=NotificationKind.ORDER,
                    title=f"Refund issued — {order.reference}",
                    body=f"A refund of {refund.amount} {refund.currency} has been sent.",
                    payload={"order_id": str(order.id), "refund_id": str(refund.id)},
                )

        log.info(
            "refund.completed",
            refund_id=str(refund.id),
            order=order.reference if order else None,
            amount=str(refund.amount),
            actor=str(actor_id),
        )
        return refund

    async def get_or_404(self, refund_id: uuid.UUID) -> Refund:
        refund = await self.refunds.get(refund_id)
        if refund is None:
            raise NotFoundError(f"refund {refund_id} not found", safe_message="Refund not found.")
        return refund

    async def refundable_amount(self, order: Order) -> Decimal:
        """How much of this order can still be refunded."""
        intent = await self.intents.verified_for_order(order.id)
        if intent is None:
            return Decimal("0")
        paid = intent.received_amount or intent.expected_amount
        return quantize_money(paid - await self.refunds.refunded_total(order.id))
