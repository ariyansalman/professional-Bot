"""End-to-end order -> payment -> verification -> delivery integrity tests."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.money import base_units
from app.core.timeutils import utcnow
from app.db.repositories.payments import PaymentConsumptionRepository
from app.domain.enums import (
    NetworkCode,
    OrderStatus,
    PaymentStatus,
    ProviderCode,
    StockItemStatus,
    VerificationOutcome,
)
from app.domain.inventory.service import InventoryService
from app.domain.orders.service import OrderService
from app.domain.payments.fingerprint import normalize_address, transaction_fingerprint
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


def observed(intent, **overrides) -> ObservedTransaction:
    now = utcnow()
    defaults = dict(
        provider=ProviderCode.TRON,
        network=NetworkCode.TRC20,
        external_id="tx_" + "a" * 60,
        asset="USDT",
        amount_units=int(intent.expected_amount_units),
        decimals=6,
        to_address=TRON_RECEIVER,
        to_address_normalized=normalize_address(TRON_RECEIVER, NetworkCode.TRC20),
        is_successful=True,
        observed_at=now,
        block_time=now,
        token_contract=USDT_TRC20_CONTRACT,
        confirmations=25,
    )
    defaults.update(overrides)
    return ObservedTransaction(**defaults)


async def build_order(session, *, quantity: int = 1, stock: int = 3):
    user = await make_user(session)
    product = await make_product(session, price="15.00")
    await add_stock(session, product, count=stock)
    orders = OrderService(session)
    quote = await orders.quote(product=product, quantity=quantity, user=user)
    order = await orders.create_order(quote=quote, user=user)
    return user, product, order


# --- order creation -------------------------------------------------------


async def test_order_creation_snapshots_price_and_reserves_stock(session):
    user, product, order = await build_order(session)
    inventory = InventoryService(session)

    assert order.total == Decimal("15.00000000")
    assert order.status is OrderStatus.CREATED
    assert len(order.items) == 1
    assert order.items[0].product_snapshot["sku"] == "SKU-001"

    # One item is now held, two remain sellable.
    status = await inventory.stock_status(product)
    assert status.available == 2

    reservations = await inventory.reservations.active_for_order(order.id)
    assert len(reservations) == 1


async def test_price_change_does_not_affect_an_existing_order(session):
    _, product, order = await build_order(session)
    original_total = order.total

    product.price = Decimal("999.00")
    await session.flush()

    refreshed = await OrderService(session).get_or_404(order.id)
    assert refreshed.total == original_total


async def test_cancelling_an_order_returns_stock(session):
    _, product, order = await build_order(session)
    inventory = InventoryService(session)
    assert (await inventory.stock_status(product)).available == 2

    await OrderService(session).cancel(order, reason="customer cancelled")
    assert order.status is OrderStatus.CANCELLED
    assert (await inventory.stock_status(product)).available == 3


async def test_paid_order_cannot_be_cancelled(session):
    from app.core.exceptions import ConflictError

    _, _, order = await build_order(session)
    orders = OrderService(session)
    await orders.transition(order, OrderStatus.PAYMENT_PENDING)
    await orders.transition(order, OrderStatus.PAYMENT_VERIFIED)

    with pytest.raises(ConflictError):
        await orders.cancel(order, reason="nope")


# --- payment intent -------------------------------------------------------


async def test_intent_freezes_the_expectation(session):
    _, _, order = await build_order(session)
    method = await make_payment_method(session)
    payments = PaymentService(session)
    await OrderService(session).transition(order, OrderStatus.PAYMENT_PENDING)

    intent = await payments.create_intent(order=order, method=method)
    assert intent.expected_amount == Decimal("15.000000")
    assert intent.expected_amount_units == base_units("15.000000", 6)
    assert intent.status is PaymentStatus.AWAITING_PAYMENT
    assert intent.token_contract == USDT_TRC20_CONTRACT

    # Admin later changes the receiving address and confirmations.
    method.receiving_address = "TDifferentAddress0000000000000000"
    method.required_confirmations = 1
    await session.flush()

    expectation = payments.expectation(intent)
    assert expectation.destination == TRON_RECEIVER
    assert expectation.required_confirmations == 19


async def test_intent_rejects_a_disabled_method(session):
    from app.core.exceptions import ConfigurationError

    _, _, order = await build_order(session)
    method = await make_payment_method(session)
    method.is_enabled = False
    await session.flush()

    with pytest.raises(ConfigurationError):
        await PaymentService(session).create_intent(order=order, method=method)


# --- verification ---------------------------------------------------------


async def _verified_intent(session, monkeypatch, transaction_overrides=None, *, quantity=1):
    """Create an intent and drive one verification pass with a stubbed adapter."""
    _, product, order = await build_order(session, quantity=quantity)
    method = await make_payment_method(session)
    payments = PaymentService(session)
    await OrderService(session).transition(order, OrderStatus.PAYMENT_PENDING)
    intent = await payments.create_intent(order=order, method=method)
    await payments.submit_payment(intent=intent, reference="tx_" + "a" * 60)

    transaction = observed(intent, **(transaction_overrides or {}))
    _stub_adapter(monkeypatch, [transaction])
    result = await payments.verify(intent)
    return payments, intent, order, product, result


def _stub_adapter(monkeypatch, transactions):
    """Replace the network adapter with one returning fixed observations.

    Only the *transport* is stubbed. Every verification rule still runs for
    real against these observations.
    """

    class StubAdapter:
        provider_code = ProviderCode.TRON

        async def find_transactions(self, expectation, *, reference=None):
            return list(transactions)

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "app.domain.payments.service.build_adapter", lambda provider, method=None: StubAdapter()
    )


async def test_exact_payment_verifies_and_marks_order_paid(session, monkeypatch):
    payments, intent, order, _, result = await _verified_intent(session, monkeypatch)

    assert result.outcome is VerificationOutcome.VERIFIED
    assert result.newly_verified is True
    assert intent.status is PaymentStatus.VERIFIED
    assert order.status is OrderStatus.PAYMENT_VERIFIED
    assert order.paid_at is not None


async def test_verified_payment_is_journalled_exactly_once(session, monkeypatch):
    payments, intent, order, _, _ = await _verified_intent(session, monkeypatch)
    entries = await payments.ledger.list_for_order(order.id)
    verified = [e for e in entries if e.entry_type.value == "payment_verified"]
    assert len(verified) == 1

    # A re-run of the worker must not journal it again.
    await payments.verify(intent)
    entries = await payments.ledger.list_for_order(order.id)
    assert len([e for e in entries if e.entry_type.value == "payment_verified"]) == 1


async def test_underpayment_never_marks_the_order_paid(session, monkeypatch):
    payments, intent, order, _, result = await _verified_intent(
        session, monkeypatch, {"amount_units": base_units("14.000000", 6)}
    )
    assert result.outcome is VerificationOutcome.UNDERPAID
    assert intent.status is PaymentStatus.UNDER_REVIEW
    assert order.status is OrderStatus.MANUAL_REVIEW
    assert not order.status.is_paid


async def test_wrong_network_is_not_credited(session, monkeypatch):
    _, intent, order, _, result = await _verified_intent(
        session,
        monkeypatch,
        {"network": NetworkCode.BEP20, "provider": ProviderCode.EVM},
    )
    assert result.outcome is VerificationOutcome.WRONG_NETWORK
    assert intent.status is PaymentStatus.UNDER_REVIEW
    assert not order.status.is_paid


async def test_insufficient_confirmations_waits(session, monkeypatch):
    _, intent, order, _, result = await _verified_intent(
        session, monkeypatch, {"confirmations": 5}
    )
    assert result.outcome is VerificationOutcome.PENDING_CONFIRMATION
    assert intent.status is PaymentStatus.PENDING_CONFIRMATION
    assert intent.confirmations == 5
    assert order.status is OrderStatus.PAYMENT_PENDING
    assert result.retry_in_seconds is not None


async def test_counterfeit_token_is_not_credited(session, monkeypatch):
    _, intent, order, _, result = await _verified_intent(
        session, monkeypatch, {"token_contract": "TFakeContract00000000000000000000"}
    )
    assert result.outcome is VerificationOutcome.WRONG_ASSET
    assert not order.status.is_paid


async def test_anomaly_raises_a_reconciliation_record(session, monkeypatch):
    payments, intent, _, _, _ = await _verified_intent(
        session, monkeypatch, {"amount_units": base_units("14.000000", 6)}
    )
    page = await payments.reconciliation.open_records()
    assert page.total == 1
    assert page.items[0].kind.value == "amount_mismatch"


# --- duplicate protection -------------------------------------------------


async def test_a_transaction_cannot_pay_two_orders(session, monkeypatch):
    """The core double-spend guard."""
    user = await make_user(session)
    product = await make_product(session)
    await add_stock(session, product, count=5)
    orders = OrderService(session)
    payments = PaymentService(session)
    method = await make_payment_method(session)

    intents = []
    for _ in range(2):
        quote = await orders.quote(product=product, quantity=1, user=user)
        order = await orders.create_order(quote=quote, user=user)
        await orders.transition(order, OrderStatus.PAYMENT_PENDING)
        intent = await payments.create_intent(order=order, method=method)
        await payments.submit_payment(intent=intent, reference="tx_shared")
        intents.append(intent)

    shared = observed(intents[0], external_id="tx_shared_txid")
    _stub_adapter(monkeypatch, [shared])

    first = await payments.verify(intents[0])
    second = await payments.verify(intents[1])

    assert first.outcome is VerificationOutcome.VERIFIED
    assert second.outcome is VerificationOutcome.DUPLICATE
    assert intents[0].status is PaymentStatus.VERIFIED
    assert intents[1].status is PaymentStatus.UNDER_REVIEW

    consumptions = PaymentConsumptionRepository(session)
    fingerprint = transaction_fingerprint(
        ProviderCode.TRON, NetworkCode.TRC20, "tx_shared_txid", 0
    )
    claim = await consumptions.get_by_fingerprint(fingerprint)
    assert claim is not None
    assert claim.payment_intent_id == intents[0].id


async def test_reverifying_the_same_intent_is_idempotent(session, monkeypatch):
    payments, intent, order, _, _ = await _verified_intent(session, monkeypatch)
    result = await payments.verify(intent)
    assert result.outcome is VerificationOutcome.VERIFIED
    assert result.newly_verified is False
    consumptions = await payments.consumptions.for_order(order.id)
    assert len(consumptions) == 1


async def test_paid_order_cannot_get_another_payment_intent(session, monkeypatch):
    """A paid order is closed to further payment: no second way to pay it."""
    from app.core.exceptions import ConflictError

    payments, verified_intent, order, _, _ = await _verified_intent(session, monkeypatch)
    method = await payments.methods.get(verified_intent.payment_method_id)

    with pytest.raises(ConflictError):
        await payments.create_intent(order=order, method=method)


async def test_manual_approval_refuses_when_a_transaction_is_already_consumed(
    session, monkeypatch
):
    """Manual review resolves ambiguity; it never bypasses integrity.

    An admin approving a second intent for an order that already consumed a
    transaction would credit the order twice, so it is refused outright.
    """
    from app.core.exceptions import ConflictError

    payments, verified_intent, order, _, _ = await _verified_intent(session, monkeypatch)
    method = await payments.methods.get(verified_intent.payment_method_id)

    # Build a second intent directly, bypassing the create_intent guard, to
    # prove the approval path has its own independent check.
    second = await payments.create_intent(
        order=await _unpaid_clone(session, order), method=method
    )
    second.order_id = order.id
    await session.flush()
    await payments._to_review(second, "needs review")

    with pytest.raises(ConflictError):
        await payments.approve_manually(
            intent=second, actor_id=order.user_id, actor_label="admin", reason="test"
        )


async def _unpaid_clone(session, order):
    """A fresh pending order used to construct a second intent for testing."""
    from tests.factories import add_stock, make_product

    product = await make_product(session, sku="SKU-CLONE")
    await add_stock(session, product, count=1)
    orders = OrderService(session)
    quote = await orders.quote(product=product, quantity=1, user=await _user_of(session, order))
    clone = await orders.create_order(quote=quote, user=await _user_of(session, order))
    await orders.transition(clone, OrderStatus.PAYMENT_PENDING)
    return clone


async def _user_of(session, order):
    from app.db.repositories.users import UserRepository

    return await UserRepository(session).get(order.user_id)


async def test_verified_payment_cannot_be_rejected(session, monkeypatch):
    from app.core.exceptions import ConflictError

    payments, intent, order, _, _ = await _verified_intent(session, monkeypatch)
    with pytest.raises(ConflictError):
        await payments.reject_manually(
            intent=intent, actor_id=order.user_id, actor_label="admin", reason="test"
        )


# --- expiry ---------------------------------------------------------------


async def test_expired_intent_is_not_paid(session, monkeypatch):
    _, _, order = await build_order(session)
    method = await make_payment_method(session)
    payments = PaymentService(session)
    await OrderService(session).transition(order, OrderStatus.PAYMENT_PENDING)
    intent = await payments.create_intent(order=order, method=method)

    intent.expires_at = utcnow() - timedelta(minutes=1)
    await session.flush()

    assert await payments.expire_due() == 1
    assert intent.status is PaymentStatus.EXPIRED
    assert not order.status.is_paid


# --- inventory concurrency ------------------------------------------------


async def test_two_orders_cannot_take_the_last_item(session):
    from app.core.exceptions import InsufficientStockError

    user_a = await make_user(session, telegram_id=1)
    user_b = await make_user(session, telegram_id=2)
    product = await make_product(session)
    await add_stock(session, product, count=1)
    orders = OrderService(session)

    quote_a = await orders.quote(product=product, quantity=1, user=user_a)
    await orders.create_order(quote=quote_a, user=user_a)

    # The second buyer finds nothing left, and is told so before paying.
    with pytest.raises((InsufficientStockError, Exception)):
        quote_b = await orders.quote(product=product, quantity=1, user=user_b)
        await orders.create_order(quote=quote_b, user=user_b)


async def test_expired_reservation_returns_stock(session):
    _, product, order = await build_order(session)
    inventory = InventoryService(session)
    assert (await inventory.stock_status(product)).available == 2

    for reservation in await inventory.reservations.active_for_order(order.id):
        reservation.expires_at = utcnow() - timedelta(minutes=1)
    await session.flush()

    assert await inventory.reap_expired() == 1
    assert (await inventory.stock_status(product)).available == 3


async def test_allocation_is_idempotent(session):
    _, product, order = await build_order(session)
    inventory = InventoryService(session)
    item = order.items[0]

    first = await inventory.allocate_for_order_item(
        order_id=order.id, order_item_id=item.id, quantity=1, product=product
    )
    second = await inventory.allocate_for_order_item(
        order_id=order.id, order_item_id=item.id, quantity=1, product=product
    )
    assert len(first) == 1
    assert {i.id for i in first} == {i.id for i in second}
    assert first[0].status is StockItemStatus.SOLD
    # No extra stock was consumed by the retry.
    assert (await inventory.stock_status(product)).available == 2
