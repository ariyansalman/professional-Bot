"""Test data builders."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_secret_box
from app.db.models.catalog import Category, InventoryItem, Product
from app.db.models.payment import PaymentMethod, PaymentProvider
from app.db.models.user import User
from app.db.repositories.users import generate_referral_code
from app.domain.enums import (
    DeliveryType,
    NetworkCode,
    PaymentProviderKind,
    ProductStatus,
    ProviderCode,
    StockItemStatus,
)
from app.domain.payments.fingerprint import payload_fingerprint

USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRON_RECEIVER = "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"


async def make_user(session: AsyncSession, telegram_id: int = 1001, **kwargs) -> User:
    user = User(
        telegram_id=telegram_id,
        username=kwargs.pop("username", f"user{telegram_id}"),
        first_name=kwargs.pop("first_name", "Test"),
        referral_code=kwargs.pop("referral_code", generate_referral_code()),
        **kwargs,
    )
    session.add(user)
    await session.flush()
    return user


async def make_category(session: AsyncSession, slug: str = "licenses") -> Category:
    category = Category(slug=slug, name_en="License Keys", emoji="🔑")
    session.add(category)
    await session.flush()
    return category


async def make_product(
    session: AsyncSession,
    *,
    price: str = "15.00",
    sku: str = "SKU-001",
    delivery_type: DeliveryType = DeliveryType.STOCK_ITEM,
    status: ProductStatus = ProductStatus.ACTIVE,
    category: Category | None = None,
    **kwargs,
) -> Product:
    product = Product(
        sku=sku,
        name=kwargs.pop("name", "Premium License"),
        short_description="A premium license key",
        price=Decimal(price),
        currency="USDT",
        status=status,
        delivery_type=delivery_type,
        category_id=category.id if category else None,
        **kwargs,
    )
    session.add(product)
    await session.flush()
    return product


async def add_stock(session: AsyncSession, product: Product, count: int = 3) -> list[InventoryItem]:
    box = get_secret_box()
    items = []
    for index in range(count):
        payload = f"XXXX-YYYY-ZZZZ-{index:04d}"
        item = InventoryItem(
            product_id=product.id,
            status=StockItemStatus.AVAILABLE,
            secret_payload=box.encrypt(payload),
            fingerprint=payload_fingerprint(payload),
            preview=f"****{index:04d}",
        )
        session.add(item)
        items.append(item)
    await session.flush()
    return items


async def make_payment_method(
    session: AsyncSession,
    *,
    code: str = "usdt_trc20",
    network: NetworkCode = NetworkCode.TRC20,
    provider_code: ProviderCode = ProviderCode.TRON,
    required_confirmations: int = 19,
    **kwargs,
) -> PaymentMethod:
    provider = PaymentProvider(
        code=provider_code,
        kind=PaymentProviderKind.BLOCKCHAIN,
        display_name=provider_code.value.title(),
        is_enabled=True,
        base_url="https://tron.example",
    )
    session.add(provider)
    await session.flush()

    method = PaymentMethod(
        code=code,
        provider_id=provider.id,
        display_name="USDT TRC20",
        is_enabled=True,
        asset="USDT",
        asset_decimals=6,
        network=network,
        network_label="TRON / TRC20",
        receiving_address=kwargs.pop("receiving_address", TRON_RECEIVER),
        token_contract=kwargs.pop("token_contract", USDT_TRC20_CONTRACT),
        required_confirmations=required_confirmations,
        payment_window_seconds=1800,
        quote_rate=Decimal("1"),
        **kwargs,
    )
    session.add(method)
    await session.flush()
    # Make the relationship available without a lazy load in async context.
    method.provider = provider
    return method
