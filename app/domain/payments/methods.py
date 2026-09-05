"""Readiness rules for a payment method (sections 91-93).

A method that is enabled but half-configured is worse than one that is off: it
takes the customer's money and cannot credit it. These rules are the single
answer to "may this method be sold with?", used by the admin toggle and by the
smoke test, so the panel and the checks cannot disagree.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.core.timeutils import utcnow
from app.db.models.payment import PaymentMethod
from app.domain.enums import NetworkCode, PaymentProviderKind
from app.domain.payments.addresses import validate_address

#: The asset each chain moves without a token contract. Anything else on that
#: chain is a token and must be matched by contract, never by symbol — that is
#: what stops a counterfeit "USDT" from being credited.
NATIVE_ASSETS: dict[NetworkCode, str] = {
    NetworkCode.TRC20: "TRX",
    NetworkCode.BTC: "BTC",
    NetworkCode.LTC: "LTC",
    NetworkCode.TON: "TON",
    NetworkCode.SOL: "SOL",
    NetworkCode.ERC20: "ETH",
    NetworkCode.BEP20: "BNB",
    NetworkCode.POLYGON: "POL",
    NetworkCode.ARBITRUM: "ETH",
    NetworkCode.AVAXC: "AVAX",
}

#: How long a quoted price for a volatile asset stays trustworthy. This is a
#: prompt to re-check, not an automatic cut-off: expiring a rate on its own
#: would silently close a payment method with no operator ever being told.
RATE_STALE_AFTER_HOURS = 6


def requires_token_contract(method: PaymentMethod) -> bool:
    """True when this asset is a token on this chain rather than the native coin."""
    native = NATIVE_ASSETS.get(method.network)
    if native is None:
        return False
    return method.asset.upper() != native


def is_pegged(method: PaymentMethod) -> bool:
    """A rate of exactly 1 is a peg (USDT priced in USDT), not a live quote."""
    return method.quote_rate == Decimal("1")


def is_rate_stale(method: PaymentMethod) -> bool:
    """True when a volatile asset is still being priced from an old quote."""
    if is_pegged(method):
        return False
    if method.quote_rate_updated_at is None:
        return True
    return utcnow() - method.quote_rate_updated_at > timedelta(hours=RATE_STALE_AFTER_HOURS)


def readiness_blocker(method: PaymentMethod) -> str | None:
    """Why this method cannot be enabled, or ``None`` when it is ready."""
    if not method.receiving_address:
        return "no receiving address is set"

    if method.provider.kind is PaymentProviderKind.BLOCKCHAIN:
        problem = validate_address(method.receiving_address, method.network)
        if problem:
            return f"the receiving address is not valid for {method.network.value} — {problem}"
        if requires_token_contract(method) and not method.token_contract:
            return (
                f"{method.asset} on {method.network.value} is a token, so a token "
                "contract must be set before payments can be told apart from "
                "counterfeits"
            )

    if method.quote_rate is None or method.quote_rate <= 0:
        return (
            f"no quote rate is set — without it a {method.asset} payment would be "
            "priced 1:1 against the order total"
        )

    if method.requires_memo and not method.memo_template:
        return "a memo is required but no memo template is configured"

    if method.asset_decimals < 0:
        return "the asset decimals are invalid"

    return None
