"""Concurrency tests against real PostgreSQL (section 133).

SQLite cannot express row-level locking or true concurrent transactions, so
these tests only run when ``TEST_DATABASE_URL`` points at a PostgreSQL
instance. They are the tests that actually prove the platform's two hardest
guarantees:

* the same transaction can never be credited to two orders
* two buyers can never be sold the same stock item

Run them with, for example:
    TEST_DATABASE_URL=postgresql+asyncpg://postgres@/commerce pytest tests/integration
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.timeutils import utcnow
from app.db.models import Base
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

TEST_DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL is not set; PostgreSQL concurrency tests skipped"
)


@pytest_asyncio.fixture
async def pg_engine():
    engine = create_async_engine(TEST_DSN, poolclass=None)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def pg_sessionmaker(pg_engine):
    from app.db.session import configure_sessionmaker

    factory = async_sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)
    configure_sessionmaker(factory)
    return factory


def observed(intent, external_id: str) -> ObservedTransaction:
    now = utcnow()
    return ObservedTransaction(
        provider=ProviderCode.TRON,
        network=NetworkCode.TRC20,
        external_id=external_id,
        asset="USDT",
        amount_units=int(intent.expected_amount_units),
        decimals=6,
        to_address=TRON_RECEIVER,
        to_address_normalized=normalize_address(TRON_RECEIVER, NetworkCode.TRC20),
        is_successful=True,
        observed_at=now,
        block_time=now,
        token_contract=USDT_TRC20_CONTRACT,
        confirmations=30,
    )


async def test_same_transaction_cannot_be_claimed_by_two_concurrent_workers(
    pg_sessionmaker, monkeypatch
):
    """The double-spend guard under genuine concurrency.

    Two workers, in two separate database transactions, race to credit the
    same on-chain transaction to two different orders. Exactly one must win.
    """
    async with pg_sessionmaker() as setup:
        user = await make_user(setup, telegram_id=9001)
        product = await make_product(setup, price="15.00")
        await add_stock(setup, product, count=5)
        method = await make_payment_method(setup)
        orders = OrderService(setup)
        payments = PaymentService(setup)

        intent_ids = []
        for _ in range(2):
            quote = await orders.quote(product=product, quantity=1, user=user)
            order = await orders.create_order(quote=quote, user=user)
            await orders.transition(order, OrderStatus.PAYMENT_PENDING)
            intent = await payments.create_intent(order=order, method=method)
            await payments.submit_payment(intent=intent, reference="shared-tx")
            intent_ids.append(intent.id)
        await setup.commit()
        method_id = method.id

    shared_external_id = "shared-onchain-tx-0001"

    class StubAdapter:
        provider_code = ProviderCode.TRON

        def __init__(self, expectation_amount_units: int) -> None:
            self.units = expectation_amount_units

        async def find_transactions(self, expectation, *, reference=None):
            return [
                ObservedTransaction(
                    provider=ProviderCode.TRON,
                    network=NetworkCode.TRC20,
                    external_id=shared_external_id,
                    asset="USDT",
                    amount_units=expectation.expected_amount_units,
                    decimals=6,
                    to_address=TRON_RECEIVER,
                    to_address_normalized=normalize_address(TRON_RECEIVER, NetworkCode.TRC20),
                    is_successful=True,
                    observed_at=utcnow(),
                    block_time=utcnow(),
                    token_contract=USDT_TRC20_CONTRACT,
                    confirmations=30,
                )
            ]

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "app.domain.payments.service.build_adapter",
        lambda provider, method=None: StubAdapter(0),
    )

    async def verify(intent_id):
        """One worker: its own session, its own transaction."""
        async with pg_sessionmaker() as session:
            payments = PaymentService(session)
            intent = await payments.intents.get_full(intent_id)
            result = await payments.verify(intent)
            await session.commit()
            return result.outcome

    outcomes = await asyncio.gather(
        verify(intent_ids[0]), verify(intent_ids[1]), return_exceptions=True
    )
    resolved = [o for o in outcomes if not isinstance(o, Exception)]

    verified = [o for o in resolved if o is VerificationOutcome.VERIFIED]
    assert len(verified) == 1, f"exactly one payment may be credited, got {outcomes}"

    # And exactly one consumption row exists for that transaction.
    async with pg_sessionmaker() as session:
        from app.db.repositories.payments import PaymentConsumptionRepository

        fingerprint = transaction_fingerprint(
            ProviderCode.TRON, NetworkCode.TRC20, shared_external_id, 0
        )
        claim = await PaymentConsumptionRepository(session).get_by_fingerprint(fingerprint)
        assert claim is not None
        assert claim.payment_intent_id in intent_ids


async def test_concurrent_buyers_cannot_be_sold_the_same_last_item(pg_sessionmaker):
    """Ten buyers, one stock item: exactly one order may be created."""
    async with pg_sessionmaker() as setup:
        product = await make_product(setup, price="10.00", sku="SKU-RACE")
        await add_stock(setup, product, count=1)
        users = [await make_user(setup, telegram_id=7000 + i) for i in range(10)]
        await setup.commit()
        product_id = product.id
        user_ids = [u.id for u in users]

    async def buy(user_id):
        async with pg_sessionmaker() as session:
            from app.db.repositories.catalog import ProductRepository
            from app.db.repositories.users import UserRepository

            product = await ProductRepository(session).get_active(product_id)
            user = await UserRepository(session).get(user_id)
            orders = OrderService(session)
            quote = await orders.quote(product=product, quantity=1, user=user)
            order = await orders.create_order(quote=quote, user=user)
            await session.commit()
            return order.reference

    results = await asyncio.gather(*(buy(uid) for uid in user_ids), return_exceptions=True)
    succeeded = [r for r in results if isinstance(r, str)]
    assert len(succeeded) == 1, f"exactly one buyer may win the last item, got {succeeded}"

    async with pg_sessionmaker() as session:
        inventory = InventoryService(session)
        from app.db.repositories.catalog import ProductRepository

        product = await ProductRepository(session).get_active(product_id)
        status = await inventory.stock_status(product)
        assert status.available == 0

        counts = await inventory.counts(product_id)
        assert counts["reserved"] == 1
        assert counts["available"] == 0


async def test_concurrent_delivery_never_allocates_stock_twice(pg_sessionmaker, monkeypatch):
    """Two delivery workers racing on the same order item."""
    async with pg_sessionmaker() as setup:
        user = await make_user(setup, telegram_id=9100)
        product = await make_product(setup, price="10.00", sku="SKU-DELIVERY")
        await add_stock(setup, product, count=5)
        method = await make_payment_method(setup)
        orders = OrderService(setup)
        payments = PaymentService(setup)

        quote = await orders.quote(product=product, quantity=1, user=user)
        order = await orders.create_order(quote=quote, user=user)
        await orders.transition(order, OrderStatus.PAYMENT_PENDING)
        intent = await payments.create_intent(order=order, method=method)
        await payments.submit_payment(intent=intent, reference="tx-delivery")
        await setup.commit()
        order_id = order.id
        intent_id = intent.id

    class StubAdapter:
        provider_code = ProviderCode.TRON

        async def find_transactions(self, expectation, *, reference=None):
            return [observed(expectation, "delivery-tx-0001")]

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "app.domain.payments.service.build_adapter", lambda provider, method=None: StubAdapter()
    )

    async with pg_sessionmaker() as session:
        payments = PaymentService(session)
        intent = await payments.intents.get_full(intent_id)
        result = await payments.verify(intent)
        await session.commit()
        assert result.outcome is VerificationOutcome.VERIFIED

    async def deliver():
        async with pg_sessionmaker() as session:
            from app.workers.delivery.dispatcher import enqueue_delivery

            await enqueue_delivery(session, order_id)
            await session.commit()

        async with pg_sessionmaker() as session:
            from app.db.repositories.orders import DeliveryRepository
            from app.domain.orders.delivery import DeliveryService

            deliveries = await DeliveryRepository(session).list_for_order(order_id)
            service = DeliveryService(session)
            payloads = []
            for delivery in deliveries:
                payloads.extend((await service.fulfil(delivery)).items)
            await session.commit()
            return payloads

    results = await asyncio.gather(deliver(), deliver(), return_exceptions=True)
    delivered = [r for r in results if isinstance(r, list) and r]
    assert delivered, f"at least one delivery must succeed: {results}"

    # Both workers must produce the same key, and only one item may be sold.
    if len(delivered) == 2:
        assert delivered[0] == delivered[1], "retried delivery returned different goods"

    async with pg_sessionmaker() as session:
        from sqlalchemy import select

        from app.db.models.catalog import Product
        from app.db.repositories.catalog import InventoryRepository

        product = await session.scalar(select(Product).where(Product.sku == "SKU-DELIVERY"))
        statuses = await InventoryRepository(session).counts_by_status(product.id)
        # Exactly one item was consumed no matter how many workers ran.
        assert statuses[StockItemStatus.SOLD.value] == 1, statuses
        assert statuses[StockItemStatus.AVAILABLE.value] == 4, statuses
