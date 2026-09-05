"""Receiving-address validation per network (section 93).

A receiving address that is wrong by one character sends every future payment
to nobody, and nothing downstream can recover it — so this validates the
encoding itself rather than only the shape. Base58Check and Bech32 both carry
checksums, and a single mistyped character fails them, which is exactly the
class of mistake an operator pasting an address makes.

What this deliberately does not do:

* It cannot tell you the address is *yours*. Only the operator can.
* EVM addresses carry no checksum unless they use EIP-55 mixed-case, and
  verifying that needs Keccak-256, which is not the SHA3-256 in ``hashlib``
  and has no declared dependency here. EVM addresses are therefore validated
  for shape only, and that limit is stated on the admin screen rather than
  papered over.
"""

from __future__ import annotations

import base64
import binascii
import re

import base58

from app.domain.enums import NetworkCode

#: Chains that use 0x-prefixed 20-byte hex addresses.
EVM_NETWORKS = frozenset(
    {
        NetworkCode.BEP20,
        NetworkCode.ERC20,
        NetworkCode.AVAXC,
        NetworkCode.ARBITRUM,
        NetworkCode.POLYGON,
    }
)

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TON_RAW_RE = re.compile(r"^-?\d+:[0-9a-fA-F]{64}$")

#: Base58Check version bytes we accept per chain, and what they mean.
_BASE58_VERSIONS: dict[NetworkCode, dict[int, str]] = {
    NetworkCode.TRC20: {0x41: "TRON account"},
    NetworkCode.BTC: {0x00: "P2PKH", 0x05: "P2SH"},
    # 0x32 is Litecoin's current P2SH version ("M..."); 0x05 is the deprecated
    # form that collides with Bitcoin P2SH, so it is accepted but flagged.
    NetworkCode.LTC: {0x30: "P2PKH", 0x32: "P2SH", 0x05: "P2SH (deprecated)"},
}

#: Human-readable part required by each Bech32 chain.
_BECH32_HRP: dict[NetworkCode, str] = {NetworkCode.BTC: "bc", NetworkCode.LTC: "ltc"}

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


def _bech32_polymod(values: list[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index in range(5):
            checksum ^= generator[index] if (top >> index) & 1 else 0
    return checksum


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def decode_bech32(address: str) -> tuple[str, list[int], int] | None:
    """Decode a Bech32/Bech32m string into (hrp, data, checksum constant).

    Returns ``None`` when the string is not valid Bech32 at all — a mixed-case
    string, a bad character, or a failed checksum.
    """
    if any(ord(c) < 33 or ord(c) > 126 for c in address):
        return None
    if address.lower() != address and address.upper() != address:
        # Bech32 is case-insensitive but must not be mixed; mixed case is a
        # sign the address was mangled in transit.
        return None
    address = address.lower()
    position = address.rfind("1")
    if position < 1 or position + 7 > len(address) or len(address) > 90:
        return None
    hrp, payload = address[:position], address[position + 1 :]
    data = []
    for char in payload:
        index = _BECH32_CHARSET.find(char)
        if index == -1:
            return None
        data.append(index)
    constant = _bech32_polymod(_hrp_expand(hrp) + data)
    if constant not in (_BECH32_CONST, _BECH32M_CONST):
        return None
    return hrp, data[:-6], constant


def _check_bech32(address: str, network: NetworkCode) -> str | None:
    decoded = decode_bech32(address)
    if decoded is None:
        return "That is not a valid Bech32 address — check it for a typo."
    hrp, data, constant = decoded
    expected_hrp = _BECH32_HRP[network]
    if hrp != expected_hrp:
        return f"That address belongs to another chain (prefix {hrp!r}, expected {expected_hrp!r})."
    if not data:
        return "That address is missing its witness version."
    witness_version = data[0]
    if witness_version > 16:
        return "That address has an invalid witness version."
    # BIP-350: version 0 uses Bech32, versions 1-16 use Bech32m. Accepting the
    # wrong pairing would accept an address no wallet will produce.
    if witness_version == 0 and constant != _BECH32_CONST:
        return "A version-0 address must use a Bech32 checksum."
    if witness_version > 0 and constant != _BECH32M_CONST:
        return "A version-1+ address must use a Bech32m checksum."
    return None


def _check_base58(address: str, network: NetworkCode) -> str | None:
    versions = _BASE58_VERSIONS[network]
    try:
        payload = base58.b58decode_check(address)
    except ValueError:
        return "The address checksum does not match — check it for a typo."
    if len(payload) != 21:
        return "That address is not the right length."
    if payload[0] not in versions:
        known = ", ".join(f"0x{v:02x}" for v in versions)
        return f"That address has version byte 0x{payload[0]:02x}; this chain uses {known}."
    return None


def _check_solana(address: str) -> str | None:
    """A Solana address is a raw 32-byte public key.

    There is no checksum in the encoding, so a single mistyped character that
    still decodes to 32 bytes cannot be detected here. That is a property of
    the format, not an omission — the operator must compare the address itself.
    """
    try:
        decoded = base58.b58decode(address)
    except ValueError:
        return "Solana addresses are base58 — that contains an invalid character."
    if len(decoded) != 32:
        return "A Solana address decodes to 32 bytes; that one does not."
    return None


def _crc16_xmodem(data: bytes) -> int:
    """CRC-16/XMODEM, the checksum in a TON user-friendly address."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _check_ton(address: str) -> str | None:
    if _TON_RAW_RE.match(address):
        return None
    if len(address) != 48:
        return "TON addresses are 48 characters, or raw <workchain>:<64 hex>."
    try:
        # Both base64 alphabets appear in the wild and decode identically.
        raw = base64.urlsafe_b64decode(address.replace("+", "-").replace("/", "_"))
    except (ValueError, binascii.Error):
        return "That is not a valid TON address."
    if len(raw) != 36:
        return "A TON address decodes to 36 bytes; that one does not."
    # Layout: tag(1) + workchain(1) + account hash(32) + CRC-16/XMODEM(2).
    if _crc16_xmodem(raw[:34]) != int.from_bytes(raw[34:], "big"):
        return "The address checksum does not match — check it for a typo."
    # Bit 0x80 of the tag marks non-bounceable, 0x40 marks test-only.
    if raw[0] & 0x40:
        return "That is a testnet address. Use a mainnet address."
    return None


def validate_address(address: str, network: NetworkCode | str) -> str | None:
    """Return a human-readable problem, or ``None`` when the address is sound.

    The message is shown to an operator, so it says what is wrong rather than
    only that something is.
    """
    if isinstance(network, str):
        try:
            network = NetworkCode(network)
        except ValueError:
            return "That network is not supported."

    if not address:
        return "The address is empty."
    if any(char.isspace() for char in address):
        return "The address contains whitespace — paste it without line breaks."
    if len(address) > 128:
        return "That address is too long to be valid."

    if network in EVM_NETWORKS:
        if not _EVM_RE.match(address):
            return "EVM addresses are 0x followed by 40 hexadecimal characters."
        return None
    if network is NetworkCode.TRC20:
        if not address.startswith("T") or len(address) != 34:
            return "TRON addresses start with T and are 34 characters long."
        return _check_base58(address, network)
    if network is NetworkCode.SOL:
        if not 32 <= len(address) <= 44:
            return "Solana addresses are 32-44 base58 characters."
        return _check_solana(address)
    if network is NetworkCode.TON:
        return _check_ton(address)
    if network in (NetworkCode.BTC, NetworkCode.LTC):
        prefix = _BECH32_HRP[network]
        if address.lower().startswith(f"{prefix}1"):
            return _check_bech32(address, network)
        return _check_base58(address, network)
    if network is NetworkCode.EXCHANGE_INTERNAL:
        # An exchange account identifier is not an on-chain address and has no
        # encoding we can check; the exchange itself rejects a wrong one.
        return None
    return "That network is not supported."
