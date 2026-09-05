"""Address validation and method-readiness rules (sections 91-93).

These guard the two ways a payment method silently loses money: a receiving
address that is wrong by one character, and a quote rate that prices a volatile
asset as if it were the order currency.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.domain.enums import NetworkCode, PaymentProviderKind
from app.domain.payments.addresses import decode_bech32, validate_address
from app.domain.payments.methods import (
    is_rate_stale,
    readiness_blocker,
    requires_token_contract,
)

# --- addresses ------------------------------------------------------------

VALID = [
    ("0xdAC17F958D2ee523a2206206994597C13D831ec7", NetworkCode.ERC20),
    ("0x55d398326f99059fF775485246999027B3197955", NetworkCode.BEP20),
    ("0xc2132D05D31c914a87C6611C10748AEb04B58e8F", NetworkCode.POLYGON),
    ("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", NetworkCode.ARBITRUM),
    ("0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7", NetworkCode.AVAXC),
    ("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", NetworkCode.TRC20),
    ("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", NetworkCode.SOL),
    ("EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs", NetworkCode.TON),
    ("0:83dfd552e63729b472fcbcc8c45ebcc6691702558b68ec7527e1ba403a0f31a8", NetworkCode.TON),
    ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", NetworkCode.BTC),
    ("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", NetworkCode.BTC),
    ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", NetworkCode.BTC),
    ("LM2WMpR1Rp6j3Sa59cMXMs1SPzj9eXpGc1", NetworkCode.LTC),
    ("MQMcJhpWHYVeQArcZR3sBgyPZxxRtnH441", NetworkCode.LTC),
    ("ltc1qw508d6qejxtdg4y5r3zarvary0c5xw7kgmn4n9", NetworkCode.LTC),
]


@pytest.mark.parametrize(("address", "network"), VALID)
def test_a_real_address_is_accepted(address, network):
    assert validate_address(address, network) is None


@pytest.mark.parametrize(("address", "network"), VALID)
def test_one_wrong_character_is_rejected(address, network):
    """The whole point of checking the encoding rather than the shape.

    Raw TON addresses carry no checksum, so a mistyped hex digit is still a
    syntactically valid address; that case is excluded here and stated rather
    than pretended away.
    """
    if network is NetworkCode.TON and ":" in address:
        pytest.skip("raw TON addresses have no checksum to verify")
    if network is NetworkCode.SOL:
        pytest.skip("a Solana address is a raw 32-byte key with no checksum")
    if network in {
        NetworkCode.ERC20,
        NetworkCode.BEP20,
        NetworkCode.POLYGON,
        NetworkCode.ARBITRUM,
        NetworkCode.AVAXC,
    }:
        pytest.skip("EVM addresses carry no checksum without EIP-55 keccak")

    # Swap the last payload character for a different one from the same family.
    tail = address[-1]
    replacement = "q" if tail != "q" else "p"
    assert validate_address(address[:-1] + replacement, network) is not None


@pytest.mark.parametrize(
    ("address", "network"),
    [
        # An address from the wrong chain must not be accepted as this one's.
        ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", NetworkCode.LTC),
        ("ltc1qw508d6qejxtdg4y5r3zarvary0c5xw7kgmn4n9", NetworkCode.BTC),
        ("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", NetworkCode.BTC),
        ("0xdAC17F958D2ee523a2206206994597C13D831ec7", NetworkCode.TRC20),
        ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", NetworkCode.LTC),
    ],
)
def test_an_address_from_another_chain_is_rejected(address, network):
    assert validate_address(address, network) is not None


@pytest.mark.parametrize(
    "address",
    [
        "",
        "   ",
        "bc1qw508d6qejxtdg4y5r3zarvary0 c5xw7kv8f3t4",
        "x" * 200,
    ],
)
def test_obvious_junk_is_rejected(address):
    assert validate_address(address, NetworkCode.BTC) is not None


def test_mixed_case_bech32_is_rejected():
    """Bech32 is case-insensitive but never mixed; mixed case means mangled."""
    assert decode_bech32("bc1QW508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4") is None


def test_uppercase_bech32_is_accepted():
    upper = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4".upper()
    assert validate_address(upper, NetworkCode.BTC) is None


def test_a_taproot_address_needs_bech32m():
    """BIP-350: version 0 is Bech32, versions 1+ are Bech32m."""
    taproot = "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"
    assert validate_address(taproot, NetworkCode.BTC) is None


def test_a_ton_testnet_address_is_rejected():
    """The test-only flag is bit 0x40 of the tag byte."""
    import base64

    from app.domain.payments.addresses import _crc16_xmodem

    mainnet = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
    raw = bytearray(base64.urlsafe_b64decode(mainnet))
    raw[0] |= 0x40
    # A real testnet address carries a matching checksum, so recompute it —
    # otherwise this only proves the checksum works, which is a different test.
    raw[34:] = _crc16_xmodem(bytes(raw[:34])).to_bytes(2, "big")
    testnet = base64.urlsafe_b64encode(bytes(raw)).decode()
    problem = validate_address(testnet, NetworkCode.TON)
    assert problem is not None
    assert "testnet" in problem.lower()


def test_an_unsupported_network_is_refused_rather_than_waved_through():
    assert validate_address("anything-at-all-here", "dogecoin") is not None


# --- readiness ------------------------------------------------------------


def _method(**overrides):
    defaults = dict(
        code="usdt_trc20",
        display_name="USDT TRC20",
        asset="USDT",
        asset_decimals=6,
        network=NetworkCode.TRC20,
        receiving_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        token_contract="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        quote_rate=Decimal("1"),
        quote_rate_updated_at=None,
        requires_memo=False,
        memo_template=None,
        provider=SimpleNamespace(kind=PaymentProviderKind.BLOCKCHAIN),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_a_fully_configured_method_is_ready():
    assert readiness_blocker(_method()) is None


def test_a_method_without_an_address_is_not_ready():
    assert "receiving address" in readiness_blocker(_method(receiving_address=None))


def test_a_method_with_a_typo_in_its_address_is_not_ready():
    """Otherwise every payment goes to an address nobody controls."""
    broken = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u"
    assert "not valid" in readiness_blocker(_method(receiving_address=broken))


def test_a_token_without_a_contract_is_not_ready():
    """Symbol alone cannot tell real USDT from a counterfeit with the same name."""
    blocker = readiness_blocker(_method(token_contract=None))
    assert "token contract" in blocker


def test_a_native_coin_needs_no_contract():
    btc = _method(
        code="btc",
        asset="BTC",
        asset_decimals=8,
        network=NetworkCode.BTC,
        receiving_address="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
        token_contract=None,
        quote_rate=Decimal("64500"),
    )
    assert requires_token_contract(btc) is False
    assert readiness_blocker(btc) is None


def test_a_volatile_asset_without_a_rate_is_not_ready():
    """The bug this rule exists for: a $15 order would have asked for 15 BTC."""
    btc = _method(
        code="btc",
        asset="BTC",
        asset_decimals=8,
        network=NetworkCode.BTC,
        receiving_address="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
        token_contract=None,
        quote_rate=Decimal("0"),
    )
    blocker = readiness_blocker(btc)
    assert "quote rate" in blocker
    assert "1:1" in blocker


def test_a_memo_chain_without_a_template_is_not_ready():
    ton = _method(
        code="usdt_ton",
        network=NetworkCode.TON,
        receiving_address="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        token_contract="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        requires_memo=True,
        memo_template=None,
    )
    assert "memo" in readiness_blocker(ton)


def test_a_pegged_rate_never_goes_stale():
    assert is_rate_stale(_method(quote_rate=Decimal("1"))) is False


def test_a_volatile_rate_that_was_never_set_is_stale():
    assert is_rate_stale(_method(quote_rate=Decimal("64500"))) is True


def test_a_volatile_rate_set_long_ago_is_stale():
    from datetime import timedelta

    from app.core.timeutils import utcnow
    from app.domain.payments.methods import RATE_STALE_AFTER_HOURS

    fresh = _method(quote_rate=Decimal("64500"), quote_rate_updated_at=utcnow())
    assert is_rate_stale(fresh) is False

    old = _method(
        quote_rate=Decimal("64500"),
        quote_rate_updated_at=utcnow() - timedelta(hours=RATE_STALE_AFTER_HOURS + 1),
    )
    assert is_rate_stale(old) is True


def test_every_seeded_network_has_a_native_asset_declared():
    """A new chain must state its native asset or tokens go uncontract-checked."""
    from app.domain.payments.methods import NATIVE_ASSETS

    on_chain = set(NetworkCode) - {NetworkCode.EXCHANGE_INTERNAL}
    assert on_chain <= set(NATIVE_ASSETS), f"missing: {on_chain - set(NATIVE_ASSETS)}"
