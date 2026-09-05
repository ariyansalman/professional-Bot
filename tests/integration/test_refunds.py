"""Refund tests (section 104).

Refunds are a separate financial event from payment verification. These assert
the guards that keep the refund ledger truthful: you cannot refund an unpaid
order, cannot refund more than arrived, cannot refund twice, and cannot close a
refund without evidence.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.exceptions import ConflictError, ValidationError
from app.core.timeutils import utcnow
from app.domain.enums import (
    LedgerEntryType,
    NetworkCode,
    OrderStatus,
    ProviderCode,
    RefundStatus,
    VerificationOutcome,
)
from app.domain.orders.refunds import RefundService
from app.domain.orders.service import OrderService
from app.domain.payments.fingerprint import normalize_address
from app.domain.payments.service import PaymentService
from app.domain.payments.types import ObservedTransaction
from tests.factories import (
    TRON_RECEIVER,
    USDT_TRC20_CONTRACT,
    add_stock,
    make_payment_method,
    make_product,
    make_user,
)


def _stub_adapter(monkeypatch, amount_units: int | None = None):
    class StubAdapter:
        provider_code = ProviderCode.TRON

        async def find_transactions(self, expectation, *, reference=None):
            now = utcnow()
            return [
                ObservedTransaction(
                    provider=ProviderCode.TRON,
                    network=NetworkCode.TRC20,
                    external_id="refund-tx-0001",
                    asset="USDT",
                    amount_units=amount_units or expectation.expected_amount_units,
                    decimals=6,
                    to_address=TRON_RECEIVER,
                    to_address_normalized=normalize_address(TRON_RECEIVER, NetworkCode.TRC20),
                    is_successful=True,
                    observed_at=now,
                    block_time=now,
                    token_contract=USDT_TRC20_CONTRACT,
                    confirmations=30,
                )
            ]

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "app.domain.payments.service.build_adapter", lambda provider, method=None: StubAdapter()
    )


async def _paid_order(session, monkeypatch, price: str = "20.00"):
    user = await make_user(session, telegram_id=8100)
    product = await make_product(session, price=price)
    await add_stock(session, product, count=3)
    method = await make_payment_method(session)

    orders = OrderService(session)
    payments = PaymentService(session)
    quote = await orders.quote(product=product, quantity=1, user=user)
    order = await orders.create_order(quote=quote, user=user)
    await orders.transition(order, OrderStatus.PAYMENT_PENDING)
    intent = await payments.create_intent(order=order, method=method)
    await payments.submit_payment(intent=intent, reference="refund-tx-0001")

    _stub_adapter(monkeypatch)
    result = await payments.verify(intent)
    assert result.outcome is VerificationOutcome.VERIFIED
    return order, user


async def test_an_unpaid_order_cannot_be_refunded(session):
    user = await make_user(session, telegram_id=8101)
    product = await make_product(session)
    await add_stock(session, product, count=1)
    orders = OrderService(session)
    quote = await orders.quote(product=product, quantity=1, user=user)
    order = await orders.create_order(quote=quote, user=user)

    with pytest.raises(ConflictError):
        await RefundService(session).request(
            order=order, amount=None, reason="test", requested_by_id=user.id
        )


async def test_refund_cannot_exceed_the_amount_received(session, monkeypatch):
    order, user = await _paid_order(session, monkeypatch, price="20.00")
    service = RefundService(session)

    assert await service.refundable_amount(order) == Decimal("20.00000000")

    with pytest.raises(ValidationError):
        await service.request(
            order=order, amount=Decimal("25.00"), reason="too much", requested_by_id=user.id
        )


async def test_a_full_refund_journals_and_marks_the_order_refunded(session, monkeypatch):
    order, user = await _paid_order(session, monkeypatch, price="20.00")
    service = RefundService(session)

    refund = await service.request(
        order=order, amount=None, reason="customer request", requested_by_id=user.id
    )
    assert refund.amount == Decimal("20.00000000")
    assert refund.status is RefundStatus.REQUESTED

    await service.approve(refund=refund, actor_id=user.id)
    await service.complete(refund=refund, actor_id=user.id, external_reference="0xrefundtx")

    assert refund.status is RefundStatus.COMPLETED
    assert refund.external_reference == "0xrefundtx"
    assert order.status is OrderStatus.REFUNDED

    entries = await service.ledger.list_for_order(order.id)
    refunds = [e for e in entries if e.entry_type is LedgerEntryType.REFUND]
    assert len(refunds) == 1
    # Money leaving the business is recorded as a negative amount.
    assert refunds[0].amount == Decimal("-20.00000000")

    # The original payment entry is untouched: the money did arrive.
    verified = [e for e in entries if e.entry_type is LedgerEntryType.PAYMENT_VERIFIED]
    assert len(verified) == 1
    assert verified[0].amount == Decimal("20.00000000")


async def test_partial_refunds_accumulate_and_cannot_overdraw(session, monkeypatch):
    order, user = await _paid_order(session, monkeypatch, price="20.00")
    service = RefundService(session)

    first = await service.request(
        order=order, amount=Decimal("5.00"), reason="partial", requested_by_id=user.id
    )
    await service.approve(refund=first, actor_id=user.id)
    await service.complete(refund=first, actor_id=user.id, external_reference="ref-1")

    # A partial refund leaves the order alone: the customer keeps the product.
    assert order.status is not OrderStatus.REFUNDED
    assert await service.refundable_amount(order) == Decimal("15.00000000")

    with pytest.raises(ValidationError):
        await service.request(
            order=order, amount=Decimal("20.00"), reason="overdraw", requested_by_id=user.id
        )

    second = await service.request(
        order=order, amount=Decimal("15.00"), reason="rest", requested_by_id=user.id
    )
    await service.approve(refund=second, actor_id=user.id)
    await service.complete(refund=second, actor_id=user.id, external_reference="ref-2")

    assert order.status is OrderStatus.REFUNDED
    assert await service.refundable_amount(order) == Decimal("0E-8")


async def test_completing_a_refund_requires_evidence(session, monkeypatch):
    order, user = await _paid_order(session, monkeypatch)
    service = RefundService(session)
    refund = await service.request(
        order=order, amount=Decimal("1.00"), reason="test", requested_by_id=user.id
    )
    await service.approve(refund=refund, actor_id=user.id)

    with pytest.raises(ValidationError):
        await service.complete(refund=refund, actor_id=user.id, external_reference="   ")


async def test_an_unapproved_refund_cannot_be_completed(session, monkeypatch):
    order, user = await _paid_order(session, monkeypatch)
    service = RefundService(session)
    refund = await service.request(
        order=order, amount=Decimal("1.00"), reason="test", requested_by_id=user.id
    )

    with pytest.raises(ConflictError):
        await service.complete(refund=refund, actor_id=user.id, external_reference="ref")


async def test_completing_twice_journals_once(session, monkeypatch):
    order, user = await _paid_order(session, monkeypatch)
    service = RefundService(session)
    refund = await service.request(
        order=order, amount=Decimal("2.00"), reason="test", requested_by_id=user.id
    )
    await service.approve(refund=refund, actor_id=user.id)
    await service.complete(refund=refund, actor_id=user.id, external_reference="ref-x")
    await service.complete(refund=refund, actor_id=user.id, external_reference="ref-x")

    entries = await service.ledger.list_for_order(order.id)
    assert len([e for e in entries if e.entry_type is LedgerEntryType.REFUND]) == 1


async def test_a_rejected_refund_does_not_touch_the_ledger(session, monkeypatch):
    order, user = await _paid_order(session, monkeypatch)
    service = RefundService(session)
    refund = await service.request(
        order=order, amount=Decimal("3.00"), reason="test", requested_by_id=user.id
    )
    await service.reject(refund=refund, actor_id=user.id, reason="not warranted")

    assert refund.status is RefundStatus.REJECTED
    entries = await service.ledger.list_for_order(order.id)
    assert not [e for e in entries if e.entry_type is LedgerEntryType.REFUND]
    # And the amount becomes refundable again.
    assert await service.refundable_amount(order) == Decimal("20.00000000")
