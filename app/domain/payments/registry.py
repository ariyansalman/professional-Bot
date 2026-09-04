"""Adapter registry: builds the right adapter for a payment method.

Credentials are decrypted here, held only for the adapter's lifetime, and never
written anywhere. When a provider is not configured we raise a clear
:class:`ConfigurationError` instead of silently pretending verification works.
"""

from __future__ import annotations

from app.core.exceptions import ConfigurationError, UnsupportedCapabilityError
from app.core.logging import get_logger
from app.core.security import get_secret_box
from app.db.models.payment import PaymentMethod, PaymentProvider
from app.domain.enums import NetworkCode, ProviderCode
from app.integrations.base import ProviderCredentials
from app.integrations.binance.adapter import (
    BINANCE_API_BASE,
    BINANCE_PAY_BASE,
    BinanceDepositAdapter,
    BinancePayAdapter,
)
from app.integrations.blockchain.evm import EVMAdapter
from app.integrations.blockchain.solana import SOLANA_MAINNET_RPC, SolanaAdapter
from app.integrations.blockchain.ton import TONCENTER_V3_BASE, TONAdapter
from app.integrations.blockchain.tron import TRONGRID_BASE, TronAdapter
from app.integrations.blockchain.utxo import UTXOAdapter
from app.integrations.bybit.adapter import BYBIT_API_BASE, BybitAdapter
from app.integrations.okx.adapter import OKX_API_BASE, OKXAdapter

log = get_logger(__name__)

EVM_NETWORKS = {
    NetworkCode.BEP20,
    NetworkCode.ERC20,
    NetworkCode.AVAXC,
    NetworkCode.ARBITRUM,
    NetworkCode.POLYGON,
}


def decrypt_credentials(provider: PaymentProvider) -> ProviderCredentials:
    box = get_secret_box()
    return ProviderCredentials(
        api_key=box.decrypt(provider.encrypted_api_key) if provider.encrypted_api_key else None,
        api_secret=box.decrypt(provider.encrypted_api_secret)
        if provider.encrypted_api_secret
        else None,
        passphrase=box.decrypt(provider.encrypted_passphrase)
        if provider.encrypted_passphrase
        else None,
        account_identifier=provider.account_identifier,
    )


def build_adapter(provider: PaymentProvider, method: PaymentMethod | None = None):
    """Instantiate the adapter for a provider (and optionally a method).

    The caller owns the returned adapter and must ``await adapter.aclose()``.
    """
    code = provider.code
    config = provider.config or {}

    if code in (ProviderCode.BINANCE, ProviderCode.BINANCE_PAY, ProviderCode.BYBIT, ProviderCode.OKX):
        credentials = decrypt_credentials(provider)
        if not credentials.is_complete:
            raise ConfigurationError(
                f"{code.value} credentials are not configured",
                safe_message="This payment method is temporarily unavailable.",
            )

    if code is ProviderCode.BINANCE:
        return BinanceDepositAdapter(
            credentials,
            base_url=provider.base_url or BINANCE_API_BASE,
            recv_window=int(config.get("recv_window", 5000)),
            lookback_minutes=int(config.get("lookback_minutes", 240)),
        )
    if code is ProviderCode.BINANCE_PAY:
        return BinancePayAdapter(credentials, base_url=provider.base_url or BINANCE_PAY_BASE)
    if code is ProviderCode.BYBIT:
        return BybitAdapter(
            credentials,
            base_url=provider.base_url or BYBIT_API_BASE,
            recv_window=int(config.get("recv_window", 5000)),
            lookback_minutes=int(config.get("lookback_minutes", 240)),
        )
    if code is ProviderCode.OKX:
        if not credentials.passphrase:
            raise ConfigurationError(
                "OKX requires a passphrase",
                safe_message="This payment method is temporarily unavailable.",
            )
        states = config.get("credited_states")
        return OKXAdapter(
            credentials,
            base_url=provider.base_url or OKX_API_BASE,
            lookback_minutes=int(config.get("lookback_minutes", 240)),
            credited_states=tuple(states) if states else ("2",),
        )

    # --- blockchain providers ------------------------------------------
    if code is ProviderCode.TRON:
        return TronAdapter(
            provider.base_url or TRONGRID_BASE,
            api_key=config.get("api_key") or None,
        )
    if code is ProviderCode.EVM:
        network = method.network if method is not None else None
        if network not in EVM_NETWORKS:
            raise ConfigurationError(
                f"EVM provider cannot serve network {network}",
                safe_message="This payment method is temporarily unavailable.",
            )
        rpc_url = (config.get("rpc_urls") or {}).get(network.value) or provider.base_url
        if not rpc_url:
            raise ConfigurationError(
                f"no RPC URL configured for {network.value}",
                safe_message="This payment method is temporarily unavailable.",
            )
        return EVMAdapter(rpc_url, network)
    if code is ProviderCode.TON:
        return TONAdapter(
            provider.base_url or TONCENTER_V3_BASE,
            api_key=config.get("api_key") or None,
            lookback_minutes=int(config.get("lookback_minutes", 240)),
        )
    if code is ProviderCode.SOLANA:
        return SolanaAdapter(provider.base_url or SOLANA_MAINNET_RPC)
    if code is ProviderCode.UTXO:
        network = method.network if method is not None else None
        if network not in (NetworkCode.BTC, NetworkCode.LTC):
            raise ConfigurationError(
                f"UTXO provider cannot serve network {network}",
                safe_message="This payment method is temporarily unavailable.",
            )
        base_url = (config.get("esplora_urls") or {}).get(network.value) or provider.base_url
        if not base_url:
            raise ConfigurationError(
                f"no Esplora URL configured for {network.value}",
                safe_message="This payment method is temporarily unavailable.",
            )
        return UTXOAdapter(base_url, network)

    raise UnsupportedCapabilityError(
        f"no adapter implemented for provider {code}",
        safe_message="This payment method requires manual verification.",
    )


def requires_customer_reference(provider_code: ProviderCode) -> bool:
    """True when verification cannot start until the customer submits a txid.

    Plain JSON-RPC chains cannot enumerate incoming transfers by address, so
    the hash is required as a lookup key. This is a documented limitation, not
    a shortcut: every field is still verified against the chain afterwards.
    """
    return provider_code in {ProviderCode.EVM, ProviderCode.SOLANA, ProviderCode.BINANCE_PAY}
