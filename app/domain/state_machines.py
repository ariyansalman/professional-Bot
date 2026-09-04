"""Guarded state transitions for orders and payments.

Status columns are never assigned directly by callers. Every change goes
through these helpers so an invalid transition raises instead of silently
corrupting financial state.
"""

from __future__ import annotations

from app.core.exceptions import InvalidStateTransition
from app.domain.enums import (
    ORDER_TRANSITIONS,
    PAYMENT_TRANSITIONS,
    OrderStatus,
    PaymentStatus,
)


def can_transition_order(current: OrderStatus, target: OrderStatus) -> bool:
    return target in ORDER_TRANSITIONS.get(current, frozenset())


def assert_order_transition(current: OrderStatus, target: OrderStatus) -> None:
    if current == target:
        return
    if not can_transition_order(current, target):
        raise InvalidStateTransition(
            f"order transition {current} -> {target} is not allowed",
            context={"from": current.value, "to": target.value},
        )


def can_transition_payment(current: PaymentStatus, target: PaymentStatus) -> bool:
    return target in PAYMENT_TRANSITIONS.get(current, frozenset())


def assert_payment_transition(current: PaymentStatus, target: PaymentStatus) -> None:
    if current == target:
        return
    if not can_transition_payment(current, target):
        raise InvalidStateTransition(
            f"payment transition {current} -> {target} is not allowed",
            context={"from": current.value, "to": target.value},
        )


def order_status_for_payment(payment: PaymentStatus) -> OrderStatus | None:
    """Map a payment status onto the order status it implies, if any."""
    mapping = {
        PaymentStatus.AWAITING_PAYMENT: OrderStatus.PAYMENT_PENDING,
        PaymentStatus.SUBMITTED: OrderStatus.PAYMENT_PENDING,
        PaymentStatus.DETECTING: OrderStatus.PAYMENT_PENDING,
        PaymentStatus.DETECTED: OrderStatus.PAYMENT_PENDING,
        PaymentStatus.VERIFYING: OrderStatus.PAYMENT_PENDING,
        PaymentStatus.PENDING_CONFIRMATION: OrderStatus.PAYMENT_PENDING,
        PaymentStatus.VERIFIED: OrderStatus.PAYMENT_VERIFIED,
        PaymentStatus.UNDER_REVIEW: OrderStatus.MANUAL_REVIEW,
        PaymentStatus.EXPIRED: OrderStatus.EXPIRED,
    }
    return mapping.get(payment)
