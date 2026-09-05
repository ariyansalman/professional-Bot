"""Payment providers, methods, intents, attempts, observed transactions,
verification attempts and the single-consumption guard.

The integrity rules that this schema enforces at the database level:

* ``payment_consumptions`` has a UNIQUE constraint on the normalised
  transaction fingerprint - a transaction can be credited to exactly one
  payment intent, ever, even under concurrent workers.
* ``provider_transactions`` / ``blockchain_transactions`` are unique per
  (provider, external id) so the same observation is recorded once.
* ``payment_intents.expected_amount`` is written at creation and never
  updated; a re-quote creates a new intent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    GUID,
    Base,
    BaseUnits,
    Money,
    TimestampMixin,
    TZDateTime,
    UUIDPrimaryKey,
)
from app.domain.enums import (
    NetworkCode,
    PaymentProviderKind,
    PaymentStatus,
    ProviderCode,
    ReconciliationKind,
    ReconciliationStatus,
    VerificationOutcome,
)

if TYPE_CHECKING:
    from app.db.models.order import Order


class PaymentProvider(UUIDPrimaryKey, TimestampMixin, Base):
    """An integration (Binance / Bybit / OKX / a chain family) plus its
    encrypted credentials and live health snapshot."""

    __tablename__ = "payment_providers"
    __table_args__ = (UniqueConstraint("code", name="uq_payment_providers_code"),)

    code: Mapped[ProviderCode] = mapped_column(
        Enum(ProviderCode, native_enum=False, length=24), nullable=False
    )
    kind: Mapped[PaymentProviderKind] = mapped_column(
        Enum(PaymentProviderKind, native_enum=False, length=16), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    #: Fernet ciphertext. Never returned to any UI, never logged.
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, default=None)
    encrypted_api_secret: Mapped[str | None] = mapped_column(Text, default=None)
    encrypted_passphrase: Mapped[str | None] = mapped_column(Text, default=None)
    #: Last 4 characters of the API key, for admin display only.
    api_key_hint: Mapped[str | None] = mapped_column(String(16), default=None)
    #: Sub-account / merchant identifier where the provider requires one.
    account_identifier: Mapped[str | None] = mapped_column(String(128), default=None)

    base_url: Mapped[str | None] = mapped_column(String(255), default=None)
    #: Provider-specific knobs (recv_window, lookback minutes, ...).
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)

    # Health snapshot maintained by the health-check worker.
    health_status: Mapped[str] = mapped_column(String(16), default="unknown")
    health_checked_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    health_latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    health_message: Mapped[str | None] = mapped_column(String(255), default=None)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)

    methods: Mapped[list[PaymentMethod]] = relationship(
        back_populates="provider", lazy="selectin"
    )

    @property
    def has_credentials(self) -> bool:
        return bool(self.encrypted_api_key and self.encrypted_api_secret)


class PaymentMethod(UUIDPrimaryKey, TimestampMixin, Base):
    """A concrete way to pay: asset + network + receiving destination."""

    __tablename__ = "payment_methods"
    __table_args__ = (
        UniqueConstraint("code", name="uq_payment_methods_code"),
        Index("ix_payment_methods_enabled", "is_enabled", "sort_priority"),
        CheckConstraint("required_confirmations >= 0", name="confirmations_non_negative"),
        CheckConstraint("asset_decimals >= 0", name="decimals_non_negative"),
    )

    #: Stable slug used in callback data, e.g. ``usdt_trc20`` or ``binance``.
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("payment_providers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    emoji: Mapped[str] = mapped_column(String(8), default="💎")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_priority: Mapped[int] = mapped_column(Integer, default=0)

    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    asset_decimals: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    network: Mapped[NetworkCode] = mapped_column(
        Enum(NetworkCode, native_enum=False, length=24), nullable=False
    )
    network_label: Mapped[str] = mapped_column(String(64), default="")

    #: Receiving address (blockchain) or exchange account id (exchange).
    receiving_address: Mapped[str | None] = mapped_column(String(255), default=None)
    #: Contract (EVM/TRON), jetton master (TON) or SPL mint (Solana). Payments
    #: are matched against this - never against the token symbol alone.
    token_contract: Mapped[str | None] = mapped_column(String(128), default=None)
    #: TON comment / memo / tag requirement.
    requires_memo: Mapped[bool] = mapped_column(Boolean, default=False)
    memo_template: Mapped[str | None] = mapped_column(String(64), default=None)

    required_confirmations: Mapped[int] = mapped_column(Integer, default=1)
    payment_window_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    min_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    max_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)

    #: Quote-currency price per unit of ``asset``. 1 for stablecoin==quote.
    quote_rate: Mapped[Decimal] = mapped_column(Money, default=Decimal("1"))
    instructions: Mapped[str | None] = mapped_column(Text, default=None)
    warning_text: Mapped[str | None] = mapped_column(Text, default=None)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)

    provider: Mapped[PaymentProvider] = relationship(back_populates="methods", lazy="selectin")

    @property
    def is_blockchain(self) -> bool:
        return self.provider.kind is PaymentProviderKind.BLOCKCHAIN


class PaymentIntent(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "payment_intents"
    __table_args__ = (
        UniqueConstraint("reference", name="uq_payment_intents_reference"),
        Index("ix_payment_intents_status_expires", "status", "expires_at"),
        Index("ix_payment_intents_order", "order_id"),
        Index("ix_payment_intents_polling", "status", "next_poll_at"),
        CheckConstraint("expected_amount > 0", name="expected_amount_positive"),
    )

    #: Public reference shown to the customer and used as the payment memo.
    reference: Mapped[str] = mapped_column(String(32), nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    payment_method_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("payment_methods.id", ondelete="RESTRICT"), nullable=False
    )
    provider_code: Mapped[ProviderCode] = mapped_column(
        Enum(ProviderCode, native_enum=False, length=24), nullable=False
    )
    network: Mapped[NetworkCode] = mapped_column(
        Enum(NetworkCode, native_enum=False, length=24), nullable=False
    )

    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, native_enum=False, length=24),
        default=PaymentStatus.CREATED,
        nullable=False,
    )

    # --- immutable expectation snapshot -----------------------------------
    #: Amount in the order currency (what the customer owes).
    order_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    order_currency: Mapped[str] = mapped_column(String(16), default="USDT")
    #: Amount in the payment asset (what must arrive on-chain / at exchange).
    expected_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    expected_amount_units: Mapped[int] = mapped_column(BaseUnits, nullable=False)
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    asset_decimals: Mapped[int] = mapped_column(Integer, nullable=False)
    destination: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Normalised (case-folded/checksum-stripped) destination for comparison.
    destination_normalized: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    token_contract: Mapped[str | None] = mapped_column(String(128), default=None)
    memo: Mapped[str | None] = mapped_column(String(64), default=None)
    required_confirmations: Mapped[int] = mapped_column(Integer, default=1)
    quote_rate: Mapped[Decimal] = mapped_column(Money, default=Decimal("1"))

    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    detected_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    verified_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    failed_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)

    #: Amount actually observed (set once a transaction is matched).
    received_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    received_amount_units: Mapped[int | None] = mapped_column(BaseUnits, default=None)
    confirmations: Mapped[int] = mapped_column(Integer, default=0)

    verification_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_poll_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    last_outcome: Mapped[VerificationOutcome | None] = mapped_column(
        Enum(VerificationOutcome, native_enum=False, length=32), default=None
    )
    failure_reason: Mapped[str | None] = mapped_column(String(255), default=None)
    review_reason: Mapped[str | None] = mapped_column(String(255), default=None)

    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    #: Frozen copy of the verification configuration used for this intent, so
    #: later admin changes cannot retroactively alter how it was validated.
    verification_config: Mapped[dict[str, Any]] = mapped_column(default=dict)

    order: Mapped[Order] = relationship(back_populates="payment_intents", lazy="selectin")
    method: Mapped[PaymentMethod] = relationship(lazy="selectin")
    attempts_rel: Mapped[list[PaymentAttempt]] = relationship(
        back_populates="intent", lazy="noload", order_by="PaymentAttempt.created_at"
    )

    @property
    def is_open(self) -> bool:
        return self.status.is_open


class PaymentAttempt(UUIDPrimaryKey, TimestampMixin, Base):
    """One customer claim of payment ("I've paid" + optional reference)."""

    __tablename__ = "payment_attempts"
    __table_args__ = (
        Index("ix_payment_attempts_intent", "payment_intent_id"),
        Index("ix_payment_attempts_reference", "submitted_reference"),
    )

    payment_intent_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("payment_intents.id", ondelete="CASCADE"), nullable=False
    )
    provider_code: Mapped[ProviderCode] = mapped_column(
        Enum(ProviderCode, native_enum=False, length=24), nullable=False
    )
    network: Mapped[NetworkCode] = mapped_column(
        Enum(NetworkCode, native_enum=False, length=24), nullable=False
    )
    #: What the customer typed. Treated purely as a lookup hint, never as proof.
    submitted_reference: Mapped[str | None] = mapped_column(String(160), default=None)
    submitted_txid: Mapped[str | None] = mapped_column(String(160), default=None)
    submitted_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    verification_count: Mapped[int] = mapped_column(Integer, default=0)
    last_outcome: Mapped[VerificationOutcome | None] = mapped_column(
        Enum(VerificationOutcome, native_enum=False, length=32), default=None
    )
    failure_reason: Mapped[str | None] = mapped_column(String(255), default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    source: Mapped[str] = mapped_column(String(16), default="telegram")

    intent: Mapped[PaymentIntent] = relationship(back_populates="attempts_rel", lazy="noload")


class ProviderTransaction(UUIDPrimaryKey, TimestampMixin, Base):
    """A transaction observed at an exchange account (off-chain record)."""

    __tablename__ = "provider_transactions"
    __table_args__ = (
        UniqueConstraint(
            "provider_code", "external_id", name="uq_provider_transactions_provider_code"
        ),
        Index("ix_provider_transactions_lookup", "provider_code", "asset", "observed_at"),
    )

    provider_code: Mapped[ProviderCode] = mapped_column(
        Enum(ProviderCode, native_enum=False, length=24), nullable=False
    )
    #: Provider-native unique id (Binance Pay orderId / Bybit id / OKX depId).
    external_id: Mapped[str] = mapped_column(String(160), nullable=False)
    record_type: Mapped[str] = mapped_column(String(32), default="deposit")
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    network: Mapped[str | None] = mapped_column(String(32), default=None)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="")
    txid: Mapped[str | None] = mapped_column(String(160), default=None, index=True)
    address: Mapped[str | None] = mapped_column(String(255), default=None)
    counterparty: Mapped[str | None] = mapped_column(String(160), default=None)
    reference: Mapped[str | None] = mapped_column(String(160), default=None, index=True)
    confirmations: Mapped[int | None] = mapped_column(Integer, default=None)
    observed_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    #: Raw provider payload, retained for reconciliation and dispute handling.
    raw_payload: Mapped[dict[str, Any]] = mapped_column(default=dict)


class BlockchainTransaction(UUIDPrimaryKey, TimestampMixin, Base):
    """A transfer observed on-chain and normalised to a common shape."""

    __tablename__ = "blockchain_transactions"
    __table_args__ = (
        UniqueConstraint(
            "network", "txid", "log_index", name="uq_blockchain_transactions_network"
        ),
        Index("ix_blockchain_transactions_receiver", "network", "to_address_normalized"),
    )

    network: Mapped[NetworkCode] = mapped_column(
        Enum(NetworkCode, native_enum=False, length=24), nullable=False
    )
    txid: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Distinguishes multiple transfers inside one transaction.
    log_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    token_contract: Mapped[str | None] = mapped_column(String(128), default=None)
    from_address: Mapped[str | None] = mapped_column(String(255), default=None)
    to_address: Mapped[str] = mapped_column(String(255), nullable=False)
    to_address_normalized: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Integer base units - the authoritative amount.
    amount_units: Mapped[int] = mapped_column(BaseUnits, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    decimals: Mapped[int] = mapped_column(Integer, default=6)
    memo: Mapped[str | None] = mapped_column(String(160), default=None)
    block_number: Mapped[int | None] = mapped_column(BigInteger, default=None)
    block_time: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    confirmations: Mapped[int] = mapped_column(Integer, default=0)
    is_successful: Mapped[bool] = mapped_column(Boolean, default=False)
    observed_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(default=dict)


class VerificationAttempt(UUIDPrimaryKey, TimestampMixin, Base):
    """Audit trail of every check performed against a provider.

    ``checks`` records the individual predicates (asset/network/receiver/
    amount/confirmations) so an admin can see exactly why a payment was or
    was not credited.
    """

    __tablename__ = "verification_attempts"
    __table_args__ = (
        Index("ix_verification_attempts_intent_created", "payment_intent_id", "created_at"),
    )

    payment_intent_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("payment_intents.id", ondelete="CASCADE"), nullable=False
    )
    payment_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("payment_attempts.id", ondelete="SET NULL"), default=None
    )
    provider_code: Mapped[ProviderCode] = mapped_column(
        Enum(ProviderCode, native_enum=False, length=24), nullable=False
    )
    outcome: Mapped[VerificationOutcome] = mapped_column(
        Enum(VerificationOutcome, native_enum=False, length=32), nullable=False
    )
    checks: Mapped[dict[str, Any]] = mapped_column(default=dict)
    observed_amount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    observed_confirmations: Mapped[int | None] = mapped_column(Integer, default=None)
    external_reference: Mapped[str | None] = mapped_column(String(160), default=None)
    #: Internal-only technical detail. Never shown to a customer.
    detail: Mapped[str | None] = mapped_column(String(512), default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)


class PaymentConsumption(UUIDPrimaryKey, TimestampMixin, Base):
    """The single-consumption guard.

    Inserting a row here is the atomic act of claiming a transaction for a
    payment intent. The UNIQUE constraint on ``fingerprint`` makes double
    spending of the same transaction impossible, including across concurrent
    workers and processes.
    """

    __tablename__ = "payment_consumptions"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_payment_consumptions_fingerprint"),
        UniqueConstraint(
            "payment_intent_id", name="uq_payment_consumptions_payment_intent_id"
        ),
    )

    #: ``sha256(provider|network|external_id|log_index)`` - see
    #: :func:`app.domain.payments.fingerprint.transaction_fingerprint`.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payment_intent_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("payment_intents.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider_code: Mapped[ProviderCode] = mapped_column(
        Enum(ProviderCode, native_enum=False, length=24), nullable=False
    )
    network: Mapped[NetworkCode] = mapped_column(
        Enum(NetworkCode, native_enum=False, length=24), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(160), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    amount_units: Mapped[int] = mapped_column(BaseUnits, nullable=False)
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)


class ReconciliationRecord(UUIDPrimaryKey, TimestampMixin, Base):
    """An anomaly found by the reconciliation worker, awaiting an admin."""

    __tablename__ = "reconciliation_records"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_reconciliation_records_dedupe_key"),
        Index("ix_reconciliation_records_status_kind", "status", "kind"),
    )

    kind: Mapped[ReconciliationKind] = mapped_column(
        Enum(ReconciliationKind, native_enum=False, length=32), nullable=False
    )
    status: Mapped[ReconciliationStatus] = mapped_column(
        Enum(ReconciliationStatus, native_enum=False, length=16),
        default=ReconciliationStatus.OPEN,
    )
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(String(255), default="")
    payment_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("payment_intents.id", ondelete="SET NULL"), default=None
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("orders.id", ondelete="SET NULL"), default=None
    )
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    resolution_note: Mapped[str | None] = mapped_column(String(512), default=None)
