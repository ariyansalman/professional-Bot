"""Provider-neutral payment types.

Every integration - exchange or blockchain - normalises its raw provider
payload into an :class:`ObservedTransaction`. The verification engine only ever
reasons about this normalised shape, which is what keeps the engine independent
of any single provider's quirks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.money import from_base_units
from app.domain.enums import NetworkCode, ProviderCode


@dataclass(frozen=True, slots=True)
class PaymentExpectation:
    """The immutable expectation a transaction is checked against.

    Built from a persisted payment intent so that verification never reads
    live admin configuration - a receiving address changed after the intent
    was created cannot retroactively validate an old payment.
    """

    intent_id: str
    reference: str
    provider: ProviderCode
    network: NetworkCode
    asset: str
    asset_decimals: int
    expected_amount: Decimal
    expected_amount_units: int
    destination: str
    destination_normalized: str
    token_contract: str | None
    memo: str | None
    required_confirmations: int
    created_at: datetime
    expires_at: datetime
    #: Extra provider-specific expectations captured at intent creation.
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ObservedTransaction:
    """A single normalised incoming transfer.

    ``amount_units`` is authoritative: it is the integer base-unit amount as
    reported by the chain/provider. ``amount`` is derived for display and
    database storage only.
    """

    provider: ProviderCode
    network: NetworkCode
    #: Provider-native unique identifier (txid, deposit id, pay order id).
    external_id: str
    asset: str
    amount_units: int
    decimals: int
    to_address: str
    to_address_normalized: str
    is_successful: bool
    observed_at: datetime
    #: Index disambiguating several transfers inside one transaction.
    log_index: int = 0
    from_address: str | None = None
    token_contract: str | None = None
    memo: str | None = None
    reference: str | None = None
    confirmations: int = 0
    block_number: int | None = None
    block_time: datetime | None = None
    txid: str | None = None
    status_label: str = ""
    record_type: str = "transfer"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def amount(self) -> Decimal:
        return from_base_units(self.amount_units, self.decimals)

    @property
    def timestamp(self) -> datetime:
        return self.block_time or self.observed_at


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Result of a provider connectivity/authentication probe."""

    healthy: bool
    latency_ms: int
    message: str = ""
    #: Populated when the probe could reach the provider but auth failed.
    authenticated: bool | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What an adapter can genuinely do, per official documentation.

    Capabilities are declared, never assumed. When a required capability is
    absent the payment is routed to manual review instead of being faked.
    """

    #: Can look a transaction up directly by its id/hash.
    lookup_by_id: bool
    #: Can list recent incoming transactions for the configured account.
    list_recent: bool
    #: Reports confirmation counts.
    reports_confirmations: bool
    #: Reports a memo/tag/comment attached to the transfer.
    reports_memo: bool
    #: Reports the sender address.
    reports_sender: bool
    #: Human-readable notes rendered in the admin provider screen.
    notes: tuple[str, ...] = ()
