"""Reseller accounts, API keys, webhook endpoints and delivery logs."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
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
    Base,
    GUID,
    Money,
    TZDateTime,
    TimestampMixin,
    UUIDPrimaryKey,
)
from app.domain.enums import ResellerStatus, WebhookDeliveryStatus

if TYPE_CHECKING:
    from app.db.models.user import User


class ResellerAccount(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "reseller_accounts"
    __table_args__ = (UniqueConstraint("user_id", name="uq_reseller_accounts_user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    business_name: Mapped[str | None] = mapped_column(String(160), default=None)
    status: Mapped[ResellerStatus] = mapped_column(
        Enum(ResellerStatus, native_enum=False, length=16),
        default=ResellerStatus.PENDING,
        index=True,
    )
    terms_accepted_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    terms_version: Mapped[str | None] = mapped_column(String(16), default=None)

    #: Global discount applied on top of per-product reseller pricing (percent).
    discount_percent: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=120)
    #: Optional CIDR/IP allowlist enforced on every API request.
    ip_allowlist: Mapped[list[Any]] = mapped_column(default=list)

    api_requests_count: Mapped[int] = mapped_column(Integer, default=0)
    orders_count: Mapped[int] = mapped_column(Integer, default=0)
    sales_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    sales_currency: Mapped[str] = mapped_column(String(16), default="USDT")

    suspended_reason: Mapped[str | None] = mapped_column(String(255), default=None)
    suspended_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    risk_flags: Mapped[dict[str, Any]] = mapped_column(default=dict)

    user: Mapped[User] = relationship(back_populates="reseller_account", lazy="selectin")
    api_keys: Mapped[list[ApiKey]] = relationship(
        back_populates="reseller", lazy="noload", order_by="ApiKey.created_at"
    )
    webhooks: Mapped[list[WebhookEndpoint]] = relationship(
        back_populates="reseller", lazy="noload"
    )

    @property
    def is_active(self) -> bool:
        return self.status is ResellerStatus.ACTIVE


class ApiKey(UUIDPrimaryKey, TimestampMixin, Base):
    """Only the peppered hash is stored; the plaintext is shown once."""

    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
        UniqueConstraint("public_id", name="uq_api_keys_public_id"),
        Index("ix_api_keys_reseller_active", "reseller_id", "revoked_at"),
    )

    reseller_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("reseller_accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Short public identifier shown in dashboards and logs.
    public_id: Mapped[str] = mapped_column(String(16), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), default="rt_live")
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[list[Any]] = mapped_column(default=list)
    is_live: Mapped[bool] = mapped_column(Boolean, default=True)

    last_used_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    last_used_ip: Mapped[str | None] = mapped_column(String(64), default=None)
    requests_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    revoked_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, default=None)

    reseller: Mapped[ResellerAccount] = relationship(back_populates="api_keys", lazy="selectin")

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class WebhookEndpoint(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "webhook_endpoints"
    __table_args__ = (Index("ix_webhook_endpoints_reseller", "reseller_id", "is_active"),)

    reseller_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("reseller_accounts.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    #: Fernet ciphertext of the signing secret.
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    secret_hint: Mapped[str] = mapped_column(String(24), default="")
    events: Mapped[list[Any]] = mapped_column(default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(String(255), default=None)

    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    disabled_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    last_success_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    last_failure_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    last_status_code: Mapped[int | None] = mapped_column(Integer, default=None)

    reseller: Mapped[ResellerAccount] = relationship(back_populates="webhooks", lazy="selectin")

    @property
    def health(self) -> str:
        if not self.is_active:
            return "disabled"
        if self.consecutive_failures == 0:
            return "healthy"
        return "degraded" if self.consecutive_failures < 5 else "failing"


class WebhookDelivery(UUIDPrimaryKey, TimestampMixin, Base):
    """One row per (endpoint, event). Unique on ``event_id`` + endpoint so a
    replayed internal event never produces a duplicate outbound call."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("endpoint_id", "event_id", name="uq_webhook_deliveries_endpoint_id"),
        Index("ix_webhook_deliveries_pending", "status", "next_attempt_at"),
    )

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("orders.id", ondelete="SET NULL"), default=None, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[WebhookDeliveryStatus] = mapped_column(
        Enum(WebhookDeliveryStatus, native_enum=False, length=16),
        default=WebhookDeliveryStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    last_status_code: Mapped[int | None] = mapped_column(Integer, default=None)
    last_error: Mapped[str | None] = mapped_column(String(512), default=None)
    delivered_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)


class IdempotencyRecord(UUIDPrimaryKey, TimestampMixin, Base):
    """Durable idempotency for reseller API writes.

    The request fingerprint is stored so replaying the *same* key with a
    *different* body is rejected instead of silently returning the old result.
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("scope", "key", name="uq_idempotency_records_scope"),
    )

    scope: Mapped[str] = mapped_column(String(96), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, default=200)
    response_body: Mapped[dict[str, Any]] = mapped_column(default=dict)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, index=True)
