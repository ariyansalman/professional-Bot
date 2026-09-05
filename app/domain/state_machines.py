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


def payment_path(current: PaymentStatus, target: PaymentStatus) -> list[PaymentStatus]:
    """Shortest legal sequence of transitions from ``current`` to ``target``.

    A verification pass can conclude in one step what the state machine models
    as several (detecting -> detected -> verifying -> verified). Rather than
    adding shortcut edges - which would weaken the guard - the caller walks the
    canonical path, so every intermediate state is genuinely entered and the
    transition table stays the single source of truth.

    Returns an empty list when the states are equal, and raises when no path
    exists (for example anything out of ``VERIFIED``).
    """
    from collections import deque

    if current == target:
        return []
    queue: deque[tuple[PaymentStatus, list[PaymentStatus]]] = deque([(current, [])])
    seen = {current}
    while queue:
        state, path = queue.popleft()
        for nxt in PAYMENT_TRANSITIONS.get(state, frozenset()):
            if nxt in seen:
                continue
            new_path = [*path, nxt]
            if nxt == target:
                return new_path
            seen.add(nxt)
            queue.append((nxt, new_path))
    raise InvalidStateTransition(
        f"no legal payment transition path from {current} to {target}",
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
