"""Orders, order items, coupons, deliveries, refunds and the ledger."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
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
    Base,
    GUID,
    Money,
    TZDateTime,
    TimestampMixin,
    UUIDPrimaryKey,
)
from app.domain.enums import (
    CouponType,
    DeliveryStatus,
    LedgerEntryType,
    OrderStatus,
    RefundStatus,
)

if TYPE_CHECKING:
    from app.db.models.payment import PaymentIntent
    from app.db.models.user import User


class Coupon(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "coupons"
    __table_args__ = (
        UniqueConstraint("code", name="uq_coupons_code"),
        CheckConstraint("value > 0", name="value_positive"),
        CheckConstraint(
            "max_redemptions IS NULL OR max_redemptions > 0", name="max_redemptions_positive"
        ),
    )

    #: Stored uppercase; lookups normalise input.
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="")
    coupon_type: Mapped[CouponType] = mapped_column(
        Enum(CouponType, native_enum=False, length=16), default=CouponType.PERCENTAGE
    )
    value: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), default="USDT")
    max_discount: Mapped[Decimal | None] = mapped_column(Money, default=None)
    min_order_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))

    max_redemptions: Mapped[int | None] = mapped_column(Integer, default=None)
    max_redemptions_per_user: Mapped[int] = mapped_column(Integer, default=1)
    redemptions_count: Mapped[int] = mapped_column(Integer, default=0)

    starts_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    #: Empty lists mean "applies to everything".
    product_ids: Mapped[list[Any]] = mapped_column(default=list)
    category_ids: Mapped[list[Any]] = mapped_column(default=list)
    #: When true only resellers may redeem it.
    reseller_only: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


class CouponUsage(UUIDPrimaryKey, TimestampMixin, Base):
    """One row per redemption. The unique constraint on ``order_id`` makes
    redemption idempotent even if the checkout is retried."""

    __tablename__ = "coupon_usage"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_coupon_usage_order_id"),
        Index("ix_coupon_usage_coupon_user", "coupon_id", "user_id"),
    )

    coupon_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    discount_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: Set when an order is cancelled/expired and the redemption is given back.
    reverted_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)


class Order(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("reference", name="uq_orders_reference"),
        UniqueConstraint(
            "reseller_id", "idempotency_key", name="uq_orders_idempotency_key"
        ),
        Index("ix_orders_user_status", "user_id", "status"),
        Index("ix_orders_status_created", "status", "created_at"),
        CheckConstraint("total >= 0", name="total_non_negative"),
        CheckConstraint("subtotal >= 0", name="subtotal_non_negative"),
        CheckConstraint("discount_total >= 0", name="discount_non_negative"),
    )

    #: Customer-facing reference, e.g. ``TG-10284``.
    reference: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("reseller_accounts.id", ondelete="SET NULL"), default=None, index=True
    )
    #: Reseller-supplied idempotency key for POST /api/v1/orders.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), default=None)
    #: Reseller's own identifiers, echoed back on every API response.
    customer_reference: Mapped[str | None] = mapped_column(String(128), default=None)
    reseller_reference: Mapped[str | None] = mapped_column(String(128), default=None)

    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=24),
        default=OrderStatus.CREATED,
        nullable=False,
    )

    # Immutable pricing snapshot taken at order creation.
    currency: Mapped[str] = mapped_column(String(16), default="USDT", nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Money, nullable=False)
    discount_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Money, nullable=False)

    coupon_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("coupons.id", ondelete="SET NULL"), default=None
    )
    coupon_code: Mapped[str | None] = mapped_column(String(32), default=None)

    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    channel: Mapped[str] = mapped_column(String(16), default="telegram")
    order_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)

    paid_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    delivered_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    cancelled_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    cancellation_reason: Mapped[str | None] = mapped_column(String(255), default=None)
    review_reason: Mapped[str | None] = mapped_column(String(255), default=None)

    user: Mapped[User | None] = relationship(
        back_populates="orders", lazy="selectin", foreign_keys=[user_id]
    )
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", lazy="selectin", cascade="all, delete-orphan"
    )
    payment_intents: Mapped[list[PaymentIntent]] = relationship(
        back_populates="order", lazy="noload", order_by="PaymentIntent.created_at"
    )
    deliveries: Mapped[list[Delivery]] = relationship(back_populates="order", lazy="noload")

    @property
    def is_paid(self) -> bool:
        return self.status.is_paid


class OrderItem(UUIDPrimaryKey, TimestampMixin, Base):
    """Line item with a full product snapshot so historical orders stay
    readable even after the product is edited or archived."""

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        Index("ix_order_items_order", "order_id"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("products.id", ondelete="SET NULL"), default=None, index=True
    )
    product_name: Mapped[str] = mapped_column(String(160), nullable=False)
    product_sku: Mapped[str] = mapped_column(String(64), default="")
    delivery_type: Mapped[str] = mapped_column(String(24), default="stock_item")
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), default="USDT")
    product_snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)

    order: Mapped[Order] = relationship(back_populates="items", lazy="noload")


class Delivery(UUIDPrimaryKey, TimestampMixin, Base):
    """Idempotent fulfilment record: one per order item, at most once."""

    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("order_item_id", name="uq_deliveries_order_item_id"),
        Index("ix_deliveries_status_next_attempt", "status", "next_attempt_at"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("order_items.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, native_enum=False, length=16),
        default=DeliveryStatus.PENDING,
        nullable=False,
    )
    delivery_type: Mapped[str] = mapped_column(String(24), default="stock_item")
    #: Fernet ciphertext of the delivered payload (keys, credentials, links).
    encrypted_payload: Mapped[str | None] = mapped_column(Text, default=None)
    file_id: Mapped[str | None] = mapped_column(String(255), default=None)
    #: Ids of the inventory items consumed by this delivery.
    inventory_item_ids: Mapped[list[Any]] = mapped_column(default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    last_error: Mapped[str | None] = mapped_column(String(512), default=None)
    delivered_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    #: Set when the customer has opened the delivered secret at least once.
    first_viewed_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)

    order: Mapped[Order] = relationship(back_populates="deliveries", lazy="noload")


class Refund(UUIDPrimaryKey, TimestampMixin, Base):
    """Refunds are tracked separately from payment verification (section 104)."""

    __tablename__ = "refunds"
    __table_args__ = (
        Index("ix_refunds_order", "order_id"),
        CheckConstraint("amount > 0", name="amount_positive"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    payment_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("payment_intents.id", ondelete="SET NULL"), default=None
    )
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), default="USDT")
    status: Mapped[RefundStatus] = mapped_column(
        Enum(RefundStatus, native_enum=False, length=16), default=RefundStatus.REQUESTED
    )
    reason: Mapped[str] = mapped_column(String(512), default="")
    destination: Mapped[str | None] = mapped_column(String(255), default=None)
    destination_network: Mapped[str | None] = mapped_column(String(32), default=None)
    external_reference: Mapped[str | None] = mapped_column(String(255), default=None)
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    processed_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)


class LedgerEntry(UUIDPrimaryKey, TimestampMixin, Base):
    """Append-only financial journal. Rows are never updated or deleted."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        Index("ix_ledger_entries_order", "order_id"),
        Index("ix_ledger_entries_type_created", "entry_type", "created_at"),
        UniqueConstraint("dedupe_key", name="uq_ledger_entries_dedupe_key"),
    )

    entry_type: Mapped[LedgerEntryType] = mapped_column(
        Enum(LedgerEntryType, native_enum=False, length=32), nullable=False
    )
    #: Signed amount: positive = money in, negative = money out.
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), default="USDT")
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("orders.id", ondelete="SET NULL"), default=None
    )
    payment_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("payment_intents.id", ondelete="SET NULL"), default=None
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    #: Guarantees a given financial event is journalled exactly once.
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="")
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)
