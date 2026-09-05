"""Catalog: categories, products, media, inventory and reservations."""

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
    GUID,
    Base,
    Money,
    SoftDeleteMixin,
    TimestampMixin,
    TZDateTime,
    UUIDPrimaryKey,
)
from app.domain.enums import (
    DeliveryType,
    ProductStatus,
    ReservationStatus,
    StockItemStatus,
)

if TYPE_CHECKING:
    pass


class Category(UUIDPrimaryKey, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("slug", name="uq_categories_slug"),)

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name_en: Mapped[str] = mapped_column(String(128), nullable=False)
    name_bn: Mapped[str | None] = mapped_column(String(128), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    emoji: Mapped[str] = mapped_column(String(8), default="📦")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_priority: Mapped[int] = mapped_column(Integer, default=0)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("categories.id", ondelete="SET NULL"), default=None
    )

    products: Mapped[list[Product]] = relationship(back_populates="category", lazy="noload")

    def display_name(self, language: str = "en") -> str:
        if language == "bn" and self.name_bn:
            return self.name_bn
        return self.name_en


class Product(UUIDPrimaryKey, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("sku", name="uq_products_sku"),
        Index("ix_products_status_category", "status", "category_id"),
        Index("ix_products_listing", "status", "sort_priority", "created_at"),
        CheckConstraint("price >= 0", name="price_non_negative"),
        CheckConstraint("min_quantity >= 1", name="min_quantity_positive"),
        CheckConstraint(
            "max_quantity IS NULL OR max_quantity >= min_quantity", name="quantity_range"
        ),
    )

    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("categories.id", ondelete="SET NULL"), default=None, index=True
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    name_bn: Mapped[str | None] = mapped_column(String(160), default=None)
    short_description: Mapped[str] = mapped_column(String(255), default="")
    short_description_bn: Mapped[str | None] = mapped_column(String(255), default=None)
    full_description: Mapped[str] = mapped_column(Text, default="")
    full_description_bn: Mapped[str | None] = mapped_column(Text, default=None)

    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), default="USDT", nullable=False)
    compare_at_price: Mapped[Decimal | None] = mapped_column(Money, default=None)

    status: Mapped[ProductStatus] = mapped_column(
        Enum(ProductStatus, native_enum=False, length=16),
        default=ProductStatus.DRAFT,
        nullable=False,
        index=True,
    )
    delivery_type: Mapped[DeliveryType] = mapped_column(
        Enum(DeliveryType, native_enum=False, length=16),
        default=DeliveryType.STOCK_ITEM,
        nullable=False,
    )
    #: Payload used by STATIC_PAYLOAD / FILE delivery types.
    delivery_payload: Mapped[str | None] = mapped_column(Text, default=None)
    delivery_file_id: Mapped[str | None] = mapped_column(String(255), default=None)
    delivery_instructions: Mapped[str | None] = mapped_column(Text, default=None)

    min_quantity: Mapped[int] = mapped_column(Integer, default=1)
    max_quantity: Mapped[int | None] = mapped_column(Integer, default=10)
    #: Only meaningful for non STOCK_ITEM delivery: unlimited when NULL.
    stock_override: Mapped[int | None] = mapped_column(Integer, default=None)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5)

    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_best_seller: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_new_arrival: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sort_priority: Mapped[int] = mapped_column(Integer, default=0)

    available_to_customers: Mapped[bool] = mapped_column(Boolean, default=True)
    available_to_resellers: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    #: Reseller pricing rules (section 54). NULL disables reseller purchase.
    reseller_price: Mapped[Decimal | None] = mapped_column(Money, default=None)
    reseller_min_price: Mapped[Decimal | None] = mapped_column(Money, default=None)
    reseller_recommended_price: Mapped[Decimal | None] = mapped_column(Money, default=None)

    features: Mapped[list[Any]] = mapped_column(default=list)
    included_items: Mapped[list[Any]] = mapped_column(default=list)
    requirements: Mapped[list[Any]] = mapped_column(default=list)
    faq: Mapped[list[Any]] = mapped_column(default=list)
    product_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)

    sales_count: Mapped[int] = mapped_column(Integer, default=0)
    views_count: Mapped[int] = mapped_column(Integer, default=0)

    category: Mapped[Category | None] = relationship(back_populates="products", lazy="selectin")
    media: Mapped[list[ProductMedia]] = relationship(
        back_populates="product",
        lazy="selectin",
        order_by="ProductMedia.sort_priority",
        cascade="all, delete-orphan",
    )

    def display_name(self, language: str = "en") -> str:
        if language == "bn" and self.name_bn:
            return self.name_bn
        return self.name

    @property
    def tracks_stock_items(self) -> bool:
        return self.delivery_type is DeliveryType.STOCK_ITEM


class ProductMedia(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "product_media"

    product_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Telegram file_id (preferred) or an external URL.
    file_id: Mapped[str | None] = mapped_column(String(255), default=None)
    url: Mapped[str | None] = mapped_column(String(1024), default=None)
    media_type: Mapped[str] = mapped_column(String(16), default="photo")
    caption: Mapped[str | None] = mapped_column(String(255), default=None)
    sort_priority: Mapped[int] = mapped_column(Integer, default=0)

    product: Mapped[Product] = relationship(back_populates="media", lazy="noload")


class InventoryItem(UUIDPrimaryKey, TimestampMixin, Base):
    """A single sellable unit of stock (one key / code / account).

    ``secret_payload`` holds the encrypted deliverable. It is decrypted only at
    delivery time and never logged.
    """

    __tablename__ = "inventory"
    __table_args__ = (
        Index("ix_inventory_product_status", "product_id", "status"),
        UniqueConstraint("product_id", "fingerprint", name="uq_inventory_product_id"),
    )

    product_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[StockItemStatus] = mapped_column(
        Enum(StockItemStatus, native_enum=False, length=16),
        default=StockItemStatus.AVAILABLE,
        nullable=False,
    )
    #: Fernet ciphertext of the deliverable payload.
    secret_payload: Mapped[str] = mapped_column(Text, nullable=False)
    #: SHA-256 of the plaintext; enforces "no duplicate stock item" per product.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Non-secret hint shown in admin lists, e.g. "XXXX-...-9F2A".
    preview: Mapped[str] = mapped_column(String(64), default="")

    order_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("order_items.id", ondelete="SET NULL"), default=None, index=True
    )
    sold_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    added_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    item_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)
    invalid_reason: Mapped[str | None] = mapped_column(String(255), default=None)


class InventoryReservation(UUIDPrimaryKey, TimestampMixin, Base):
    """Time-boxed hold on an inventory item while a payment is in flight."""

    __tablename__ = "inventory_reservations"
    __table_args__ = (
        UniqueConstraint("inventory_item_id", "status", name="uq_inventory_reservations_inventory_item_id"),
        Index("ix_inventory_reservations_expiry", "status", "expires_at"),
        Index("ix_inventory_reservations_order", "order_id"),
    )

    inventory_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("inventory.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    order_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("order_items.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, native_enum=False, length=16),
        default=ReservationStatus.ACTIVE,
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    consumed_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)


class InventoryAdjustment(UUIDPrimaryKey, TimestampMixin, Base):
    """Append-only record of every manual stock change (section 62)."""

    __tablename__ = "inventory_adjustments"

    product_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inventory_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("inventory.id", ondelete="SET NULL"), default=None
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(255), default="")
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
