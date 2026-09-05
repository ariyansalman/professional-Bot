"""Bootstrap the platform: roles, permissions, providers and payment methods.

Idempotent: running it repeatedly converges on the same state and never
overwrites an operator's configuration. In particular it never sets a receiving
address, a token contract or a credential - those are deliberately left empty so
an operator must configure them explicitly through the admin panel.

    python -m scripts.seed
    python -m scripts.seed --demo   # additionally create sample catalog data
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.admin.permissions.rbac import ROLE_PERMISSIONS, describe_role
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.models.catalog import Category, Product
from app.db.models.payment import PaymentMethod, PaymentProvider
from app.db.models.user import Permission, Role
from app.db.session import dispose_engine, session_scope
from app.domain.enums import (
    DeliveryType,
    NetworkCode,
    PaymentProviderKind,
    ProductStatus,
    ProviderCode,
)

log = get_logger(__name__)

#: (code, kind, display name, default base url)
PROVIDERS: tuple[tuple[ProviderCode, PaymentProviderKind, str, str | None], ...] = (
    (ProviderCode.BINANCE, PaymentProviderKind.EXCHANGE, "Binance", "https://api.binance.com"),
    (
        ProviderCode.BINANCE_PAY,
        PaymentProviderKind.EXCHANGE,
        "Binance Pay",
        "https://bpay.binanceapi.com",
    ),
    (ProviderCode.BYBIT, PaymentProviderKind.EXCHANGE, "Bybit", "https://api.bybit.com"),
    (ProviderCode.OKX, PaymentProviderKind.EXCHANGE, "OKX", "https://www.okx.com"),
    (ProviderCode.TRON, PaymentProviderKind.BLOCKCHAIN, "TRON", "https://api.trongrid.io"),
    (ProviderCode.EVM, PaymentProviderKind.BLOCKCHAIN, "EVM chains", None),
    (ProviderCode.TON, PaymentProviderKind.BLOCKCHAIN, "TON", "https://toncenter.com"),
    (
        ProviderCode.SOLANA,
        PaymentProviderKind.BLOCKCHAIN,
        "Solana",
        "https://api.mainnet-beta.solana.com",
    ),
    (ProviderCode.UTXO, PaymentProviderKind.BLOCKCHAIN, "Bitcoin / Litecoin", None),
)

#: Canonical USDT contracts per chain, and sensible confirmation defaults.
#: These are the well-known mainnet contract addresses; an operator must still
#: verify them for their deployment before enabling a method.
USDT_CONTRACTS: dict[NetworkCode, str] = {
    NetworkCode.TRC20: "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
    NetworkCode.ERC20: "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    NetworkCode.BEP20: "0x55d398326f99059fF775485246999027B3197955",
    NetworkCode.POLYGON: "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
    NetworkCode.ARBITRUM: "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
    NetworkCode.AVAXC: "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",
    NetworkCode.SOL: "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    NetworkCode.TON: "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
}

#: (code, provider, display, emoji, asset, decimals, network, label,
#:  confirmations, requires_memo)
METHODS: tuple[tuple, ...] = (
    ("binance", ProviderCode.BINANCE, "Binance", "🟡", "USDT", 6, NetworkCode.TRC20, "Binance deposit", 1, False),
    ("bybit", ProviderCode.BYBIT, "Bybit", "🔵", "USDT", 6, NetworkCode.TRC20, "Bybit deposit", 1, False),
    ("okx", ProviderCode.OKX, "OKX", "⚫", "USDT", 6, NetworkCode.TRC20, "OKX deposit", 1, False),
    ("usdt_trc20", ProviderCode.TRON, "USDT TRC20", "💎", "USDT", 6, NetworkCode.TRC20, "TRON / TRC20", 19, False),
    ("usdt_bep20", ProviderCode.EVM, "USDT BEP20", "💎", "USDT", 18, NetworkCode.BEP20, "BNB Smart Chain / BEP20", 15, False),
    ("usdt_erc20", ProviderCode.EVM, "USDT ERC20", "💎", "USDT", 6, NetworkCode.ERC20, "Ethereum / ERC20", 12, False),
    ("usdt_polygon", ProviderCode.EVM, "USDT Polygon", "💎", "USDT", 6, NetworkCode.POLYGON, "Polygon", 128, False),
    ("usdt_arbitrum", ProviderCode.EVM, "USDT Arbitrum", "💎", "USDT", 6, NetworkCode.ARBITRUM, "Arbitrum One", 20, False),
    ("usdt_avaxc", ProviderCode.EVM, "USDT AVAX-C", "💎", "USDT", 6, NetworkCode.AVAXC, "Avalanche C-Chain", 15, False),
    ("usdt_ton", ProviderCode.TON, "USDT TON", "💎", "USDT", 6, NetworkCode.TON, "TON", 1, True),
    ("usdt_sol", ProviderCode.SOLANA, "USDT Solana", "💎", "USDT", 6, NetworkCode.SOL, "Solana", 32, False),
    ("btc", ProviderCode.UTXO, "Bitcoin", "₿", "BTC", 8, NetworkCode.BTC, "Bitcoin", 2, False),
    ("ltc", ProviderCode.UTXO, "Litecoin", "Ł", "LTC", 8, NetworkCode.LTC, "Litecoin", 6, False),
)


async def seed_roles(session) -> int:
    """Create the RBAC roles and their permission sets."""
    created = 0
    permission_cache: dict[str, Permission] = {}

    for code in {p.value for perms in ROLE_PERMISSIONS.values() for p in perms}:
        permission = await session.scalar(select(Permission).where(Permission.code == code))
        if permission is None:
            permission = Permission(code=code, description=code.replace(".", " ").title())
            session.add(permission)
            created += 1
        permission_cache[code] = permission
    await session.flush()

    for role_name, permissions in ROLE_PERMISSIONS.items():
        # The permission collection is eagerly loaded here so assigning to it
        # does not trigger a lazy load, which is not permitted in async code.
        role = await session.scalar(
            select(Role).where(Role.name == role_name).options(selectinload(Role.permissions))
        )
        if role is None:
            role = Role(name=role_name, description=describe_role(role_name), is_system=True)
            # Assigned while the object is still pending: no load is needed.
            role.permissions = [permission_cache[p.value] for p in sorted(permissions, key=str)]
            session.add(role)
            created += 1
        else:
            # Keep the stored role in sync with the code definition.
            role.permissions = [permission_cache[p.value] for p in sorted(permissions, key=str)]
    await session.flush()
    return created


async def seed_providers(session) -> int:
    """Create provider rows, disabled and without credentials."""
    created = 0
    for code, kind, display, base_url in PROVIDERS:
        provider = await session.scalar(
            select(PaymentProvider).where(PaymentProvider.code == code)
        )
        if provider is not None:
            continue
        session.add(
            PaymentProvider(
                code=code,
                kind=kind,
                display_name=display,
                base_url=base_url,
                is_enabled=False,
                config=_default_config(code),
            )
        )
        created += 1
    await session.flush()
    return created


def _default_config(code: ProviderCode) -> dict:
    """Provider defaults an operator can override in the admin panel."""
    if code is ProviderCode.EVM:
        return {
            "rpc_urls": {
                "bep20": "https://bsc-dataseed.binance.org",
                "erc20": "",
                "polygon": "https://polygon-rpc.com",
                "arbitrum": "https://arb1.arbitrum.io/rpc",
                "avaxc": "https://api.avax.network/ext/bc/C/rpc",
            }
        }
    if code is ProviderCode.UTXO:
        return {
            "esplora_urls": {
                "btc": "https://blockstream.info",
                "ltc": "",
            }
        }
    if code is ProviderCode.OKX:
        # Deposit states counted as final. Verify against current OKX docs.
        return {"credited_states": ["2"], "lookback_minutes": 240}
    if code in (ProviderCode.BINANCE, ProviderCode.BYBIT):
        return {"recv_window": 5000, "lookback_minutes": 240}
    return {}


async def seed_methods(session) -> int:
    """Create payment methods, disabled and without a receiving address.

    Leaving the address empty is deliberate: a method cannot be enabled until an
    operator sets where the money should actually go.
    """
    created = 0
    providers = {
        provider.code: provider
        for provider in (await session.scalars(select(PaymentProvider))).all()
    }

    for (
        code,
        provider_code,
        display,
        emoji,
        asset,
        decimals,
        network,
        label,
        confirmations,
        requires_memo,
    ) in METHODS:
        existing = await session.scalar(select(PaymentMethod).where(PaymentMethod.code == code))
        if existing is not None:
            continue
        provider = providers.get(provider_code)
        if provider is None:
            continue
        session.add(
            PaymentMethod(
                code=code,
                provider_id=provider.id,
                display_name=display,
                emoji=emoji,
                asset=asset,
                asset_decimals=decimals,
                network=network,
                network_label=label,
                is_enabled=False,
                receiving_address=None,
                token_contract=USDT_CONTRACTS.get(network) if asset == "USDT" else None,
                required_confirmations=confirmations,
                requires_memo=requires_memo,
                memo_template="{reference}" if requires_memo else None,
                payment_window_seconds=1800,
                quote_rate=Decimal("1") if asset == "USDT" else Decimal("0"),
                warning_text=(
                    f"Send only {asset} on {label}. Funds sent on another network "
                    "cannot be credited automatically."
                ),
            )
        )
        created += 1
    await session.flush()
    return created


async def seed_demo_catalog(session) -> int:
    """Sample catalog for local development. Never run this in production."""
    created = 0
    category = await session.scalar(select(Category).where(Category.slug == "license-keys"))
    if category is None:
        category = Category(
            slug="license-keys", name_en="License Keys", emoji="🔑", sort_priority=10
        )
        session.add(category)
        await session.flush()
        created += 1

    if await session.scalar(select(Product).where(Product.sku == "DEMO-LICENSE")) is None:
        session.add(
            Product(
                sku="DEMO-LICENSE",
                name="Premium License",
                short_description="A premium software license key",
                full_description=(
                    "A demonstration product. Delivered instantly after the "
                    "payment is verified on-chain."
                ),
                price=Decimal("15.00"),
                currency="USDT",
                status=ProductStatus.DRAFT,
                delivery_type=DeliveryType.STOCK_ITEM,
                category_id=category.id,
                is_featured=True,
                features=["Lifetime activation", "Instant delivery", "Email support"],
                included_items=["1 license key", "Setup instructions"],
                requirements=["Windows 10 or later"],
                faq=[{"q": "How fast is delivery?", "a": "Immediately after payment confirmation."}],
                available_to_resellers=True,
                reseller_price=Decimal("12.00"),
                reseller_min_price=Decimal("13.00"),
                reseller_recommended_price=Decimal("18.00"),
            )
        )
        created += 1
    await session.flush()
    return created


async def run(demo: bool = False) -> None:
    settings = get_settings()
    configure_logging(settings.observability.level, json_output=False)

    async with session_scope() as session:
        roles = await seed_roles(session)
        providers = await seed_providers(session)
        methods = await seed_methods(session)
        demo_rows = await seed_demo_catalog(session) if demo else 0

    log.info(
        "seed.completed",
        roles_and_permissions=roles,
        providers=providers,
        payment_methods=methods,
        demo_rows=demo_rows,
    )
    print(
        f"\nSeed complete:\n"
        f"  roles/permissions created : {roles}\n"
        f"  providers created         : {providers}\n"
        f"  payment methods created   : {methods}\n"
        f"  demo catalog rows         : {demo_rows}\n"
    )
    print(
        "Next steps:\n"
        "  1. Open the bot and send /admin (your Telegram id must be in "
        "TELEGRAM_BOOTSTRAP_ADMIN_IDS).\n"
        "  2. Providers -> configure read-only credentials and test the connection.\n"
        "  3. Providers -> Methods -> set the receiving address, then enable the method.\n"
        "  4. Products -> add a product, add stock, then activate it.\n"
    )
    await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed baseline platform data")
    parser.add_argument(
        "--demo", action="store_true", help="also create sample catalog data (development only)"
    )
    args = parser.parse_args()
    asyncio.run(run(demo=args.demo))


if __name__ == "__main__":
    main()
