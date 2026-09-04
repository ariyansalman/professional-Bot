"""Exact money and on-chain amount arithmetic.

Hard rules enforced here:

* Floating point is never used for money. Every amount is a ``Decimal``.
* On-chain amounts are compared as integers in the asset's base units
  (wei / sun / satoshi / token units), never as scaled floats.
* Rounding is explicit and always away from the customer's favour is *not*
  applied silently - quantisation uses ``ROUND_HALF_UP`` for display and
  ``ROUND_DOWN`` is never used to fabricate a match.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import StrEnum
from typing import Final

#: Storage scale used by the NUMERIC columns for fiat/quote amounts.
MONEY_SCALE: Final[int] = 8
MONEY_PRECISION: Final[int] = 30

_QUANTUM = Decimal(1).scaleb(-MONEY_SCALE)


class AmountMatch(StrEnum):
    """Outcome of comparing a received amount against an expected amount."""

    EXACT = "exact"
    UNDERPAID = "underpaid"
    OVERPAID = "overpaid"


def to_decimal(value: object) -> Decimal:
    """Coerce a value into a Decimal without ever going through ``float``."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Accepting a float would silently import binary rounding error.
        raise TypeError("float is not accepted for monetary values; pass str/int/Decimal")
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:  # pragma: no cover - defensive
            raise ValueError(f"invalid decimal literal: {value!r}") from exc
    raise TypeError(f"cannot convert {type(value).__name__} to Decimal")


def quantize_money(value: Decimal | str | int, scale: int = MONEY_SCALE) -> Decimal:
    """Quantise to the storage scale using banker-safe half-up rounding."""
    dec = to_decimal(value)
    return dec.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)


def format_amount(value: Decimal | str | int, currency: str | None = None, dp: int = 2) -> str:
    """Human display: trims trailing zeros but keeps at least ``dp`` decimals."""
    dec = to_decimal(value)
    quant = dec.quantize(Decimal(1).scaleb(-max(dp, 8)), rounding=ROUND_HALF_UP)
    text = format(quant.normalize(), "f")
    if "." in text:
        integer, _, frac = text.partition(".")
        frac = frac.rstrip("0")
        while len(frac) < dp:
            frac += "0"
        text = f"{integer}.{frac}" if frac else integer
    else:
        text = f"{text}.{'0' * dp}" if dp else text
    return f"{text} {currency}" if currency else text


def base_units(amount: Decimal | str | int, decimals: int) -> int:
    """Convert a human amount into integer base units for an asset.

    Raises when the amount cannot be represented exactly at the asset's
    precision - we refuse to silently truncate money.
    """
    dec = to_decimal(amount)
    scaled = dec.scaleb(decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"amount {dec} cannot be represented exactly with {decimals} decimals"
        )
    return int(scaled)


def from_base_units(units: int | str, decimals: int) -> Decimal:
    """Convert integer base units back into a human Decimal amount."""
    value = int(units)
    return (Decimal(value).scaleb(-decimals)).normalize() + Decimal(0)


def compare_amounts(
    expected: Decimal,
    received: Decimal,
    *,
    underpayment_tolerance: Decimal = Decimal("0"),
    overpayment_tolerance: Decimal = Decimal("0"),
) -> AmountMatch:
    """Classify a received amount.

    ``underpayment_tolerance`` only ever accepts a *shortfall* that the operator
    has explicitly configured (for example to absorb an exchange withdrawal
    fee). It defaults to zero: an underpayment is never treated as full
    payment by accident.
    """
    expected_q = quantize_money(expected)
    received_q = quantize_money(received)
    delta = received_q - expected_q
    if delta == 0:
        return AmountMatch.EXACT
    if delta < 0:
        return (
            AmountMatch.EXACT
            if -delta <= quantize_money(underpayment_tolerance)
            else AmountMatch.UNDERPAID
        )
    return (
        AmountMatch.EXACT
        if delta <= quantize_money(overpayment_tolerance)
        else AmountMatch.OVERPAID
    )
