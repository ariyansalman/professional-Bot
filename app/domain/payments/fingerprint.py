"""Transaction fingerprints and address normalisation.

The fingerprint is the key that makes a transaction consumable exactly once.
It must be stable across restarts and identical for two observations of the
same transfer, regardless of which worker or code path produced them.
"""

from __future__ import annotations

import hashlib

from app.domain.enums import NetworkCode, ProviderCode

#: Chains whose addresses are case-insensitive (hex) and are compared lowercased.
_HEX_ADDRESS_NETWORKS = frozenset(
    {
        NetworkCode.BEP20,
        NetworkCode.ERC20,
        NetworkCode.AVAXC,
        NetworkCode.ARBITRUM,
        NetworkCode.POLYGON,
    }
)


def normalize_address(address: str | None, network: NetworkCode | str | None = None) -> str:
    """Return a comparison-safe form of an address.

    EVM addresses are lowercased (EIP-55 checksum casing is cosmetic). Base58
    and Bech32-style addresses (TRON, Solana, TON, BTC, LTC) are case-sensitive
    in their payload, so only surrounding whitespace is stripped. TON's
    user-friendly form is additionally normalised for the url-safe/standard
    base64 alphabet difference, which is purely an encoding variant.
    """
    if not address:
        return ""
    value = address.strip()
    if isinstance(network, str):
        try:
            network = NetworkCode(network)
        except ValueError:
            network = None
    if network in _HEX_ADDRESS_NETWORKS or value.startswith("0x"):
        return value.lower()
    if network is NetworkCode.TON:
        # The same TON account can be written with the url-safe or standard
        # base64 alphabet; both decode to identical bytes.
        return value.replace("+", "-").replace("/", "_")
    if network in (NetworkCode.BTC, NetworkCode.LTC) and value.lower().startswith(
        ("bc1", "ltc1", "tb1")
    ):
        # Bech32 is defined as case-insensitive.
        return value.lower()
    return value


def normalize_txid(txid: str | None) -> str:
    """Hashes are hex; compare them lowercased without the ``0x`` prefix."""
    if not txid:
        return ""
    value = txid.strip()
    if value.startswith(("0x", "0X")):
        value = value[2:]
    lowered = value.lower()
    # Solana signatures are base58 and case-sensitive; only fold real hex.
    if all(c in "0123456789abcdef" for c in lowered):
        return lowered
    return value


def transaction_fingerprint(
    provider: ProviderCode | str,
    network: NetworkCode | str,
    external_id: str,
    log_index: int = 0,
) -> str:
    """Stable identity of a single consumable transfer.

    Includes ``log_index`` so a batch transaction carrying two separate
    transfers to us yields two distinct, independently consumable payments.
    """
    provider_value = provider.value if isinstance(provider, ProviderCode) else str(provider)
    network_value = network.value if isinstance(network, NetworkCode) else str(network)
    identity = "|".join(
        [provider_value, network_value, normalize_txid(external_id), str(log_index)]
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def payload_fingerprint(payload: str) -> str:
    """Digest used to prevent duplicate inventory items per product."""
    return hashlib.sha256(payload.strip().encode()).hexdigest()
