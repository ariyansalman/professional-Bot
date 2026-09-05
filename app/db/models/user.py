"""Users, RBAC roles/permissions, referrals and notifications."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    GUID,
    Base,
    BigIntPrimaryKey,
    Money,
    SoftDeleteMixin,
    TimestampMixin,
    TZDateTime,
    UUIDPrimaryKey,
)
from app.domain.enums import (
    Language,
    NotificationKind,
    ReferralStatus,
    RoleName,
    UserStatus,
)

if TYPE_CHECKING:
    from app.db.models.order import Order
    from app.db.models.reseller import ResellerAccount

user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", GUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("granted_by_id", GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    Column("granted_at", DateTime(timezone=True), nullable=True),
)

role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "permission_id", Integer, ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    ),
)


class Permission(BigIntPrimaryKey, Base):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="")


class Role(BigIntPrimaryKey, TimestampMixin, Base):
    __tablename__ = "roles"

    name: Mapped[RoleName] = mapped_column(
        Enum(RoleName, native_enum=False, length=32), unique=True, nullable=False
    )
    description: Mapped[str] = mapped_column(String(255), default="")
    is_system: Mapped[bool] = mapped_column(Boolean, default=True)

    permissions: Mapped[list[Permission]] = relationship(
        secondary=role_permissions, lazy="selectin"
    )


class User(UUIDPrimaryKey, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_telegram_id", "telegram_id", unique=True),
        Index("ix_users_username_lower", "username"),
    )

    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), default=None)
    first_name: Mapped[str | None] = mapped_column(String(128), default=None)
    last_name: Mapped[str | None] = mapped_column(String(128), default=None)
    language: Mapped[Language] = mapped_column(
        Enum(Language, native_enum=False, length=8), default=Language.EN
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, native_enum=False, length=16), default=UserStatus.ACTIVE, index=True
    )
    is_bot_blocked: Mapped[bool] = mapped_column(Boolean, default=False)

    # Denormalised counters, maintained inside the same transaction as the
    # order state change so the profile screen never needs an aggregate scan.
    orders_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_orders_count: Mapped[int] = mapped_column(Integer, default=0)
    total_spent: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))

    referral_code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    referred_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )
    referral_balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))

    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    risk_flags: Mapped[dict[str, Any]] = mapped_column(default=dict)
    internal_notes: Mapped[str | None] = mapped_column(Text, default=None)

    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None, index=True)

    roles: Mapped[list[Role]] = relationship(
        secondary=user_roles,
        lazy="selectin",
        # user_roles also carries granted_by_id -> users, so the join columns
        # must be named explicitly.
        primaryjoin=lambda: User.id == user_roles.c.user_id,
        secondaryjoin=lambda: Role.id == user_roles.c.role_id,
    )
    orders: Mapped[list[Order]] = relationship(
        back_populates="user", lazy="noload", foreign_keys="Order.user_id"
    )
    reseller_account: Mapped[ResellerAccount | None] = relationship(
        back_populates="user", lazy="noload", uselist=False
    )

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or f"user:{self.telegram_id}"

    @property
    def is_staff(self) -> bool:
        return bool(self.roles)


class Referral(UUIDPrimaryKey, TimestampMixin, Base):
    """One row per referred user. Self-referral is blocked by a check."""

    __tablename__ = "referrals"
    __table_args__ = (
        UniqueConstraint("referred_user_id", name="uq_referrals_referred_user_id"),
        Index("ix_referrals_referrer_status", "referrer_id", "status"),
    )

    referrer_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    referred_user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[ReferralStatus] = mapped_column(
        Enum(ReferralStatus, native_enum=False, length=16), default=ReferralStatus.PENDING
    )
    qualifying_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("orders.id", ondelete="SET NULL"), default=None
    )
    reward_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    reward_currency: Mapped[str] = mapped_column(String(16), default="USDT")
    rewarded_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    #: Signals captured at attribution time to detect abuse rings.
    signals: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Notification(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_read", "user_id", "read_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[NotificationKind] = mapped_column(
        Enum(NotificationKind, native_enum=False, length=16), default=NotificationKind.SYSTEM
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    read_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    #: Set once the notification has been pushed to Telegram.
    pushed_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)


class RestockSubscription(UUIDPrimaryKey, TimestampMixin, Base):
    """'Notify me' registrations for out-of-stock products."""

    __tablename__ = "restock_subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "product_id", name="uq_restock_subscriptions_user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    notified_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
