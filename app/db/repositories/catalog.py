"""Category, product and inventory repositories.

The inventory repository is where the concurrency guarantees live: stock is
claimed with ``SELECT ... FOR UPDATE SKIP LOCKED`` so two simultaneous buyers
can never be handed the same key.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, delete, func, or_, select, update
from sqlalchemy.orm import selectinload

from app.core.timeutils import utcnow
from app.db.models.catalog import (
    Category,
    InventoryAdjustment,
    InventoryItem,
    InventoryReservation,
    Product,
    ProductMedia,
)
from app.db.repositories.base import BaseRepository, Page
from app.domain.enums import (
    DeliveryType,
    ProductStatus,
    ReservationStatus,
    StockItemStatus,
)


class CategoryRepository(BaseRepository[Category]):
    model = Category

    async def list_active(self) -> list[Category]:
        stmt = (
            select(Category)
            .where(Category.is_active.is_(True), Category.deleted_at.is_(None))
            .order_by(Category.sort_priority.desc(), Category.name_en)
        )
        return list((await self.session.scalars(stmt)).all())

    async def get_by_slug(self, slug: str) -> Category | None:
        stmt = select(Category).where(Category.slug == slug, Category.deleted_at.is_(None))
        return await self.session.scalar(stmt)

    async def product_counts(self) -> dict[uuid.UUID, int]:
        """Listed-product count per category, for the shop screen."""
        stmt = (
            select(Product.category_id, func.count(Product.id))
            .where(
                Product.status == ProductStatus.ACTIVE,
                Product.deleted_at.is_(None),
                Product.available_to_customers.is_(True),
            )
            .group_by(Product.category_id)
        )
        rows = await self.session.execute(stmt)
        return {category_id: count for category_id, count in rows if category_id}


class ProductRepository(BaseRepository[Product]):
    model = Product

    def _listed(self) -> Select:
        return select(Product).where(
            Product.status == ProductStatus.ACTIVE,
            Product.deleted_at.is_(None),
            Product.available_to_customers.is_(True),
        )

    def _ordering(self, stmt: Select) -> Select:
        return stmt.order_by(
            Product.sort_priority.desc(), Product.is_featured.desc(), Product.created_at.desc()
        )

    async def get_active(self, product_id: uuid.UUID) -> Product | None:
        stmt = (
            self._listed()
            .where(Product.id == product_id)
            .options(selectinload(Product.media), selectinload(Product.category))
        )
        return await self.session.scalar(stmt)

    async def get_with_media(self, product_id: uuid.UUID) -> Product | None:
        stmt = (
            select(Product)
            .where(Product.id == product_id)
            .options(selectinload(Product.media), selectinload(Product.category))
        )
        return await self.session.scalar(stmt)

    async def list_by_category(
        self, category_id: uuid.UUID, *, page: int = 1, per_page: int = 5
    ) -> Page[Product]:
        stmt = self._ordering(self._listed().where(Product.category_id == category_id))
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def list_featured(self, limit: int = 3) -> list[Product]:
        stmt = self._ordering(self._listed().where(Product.is_featured.is_(True))).limit(limit)
        return list((await self.session.scalars(stmt)).all())

    async def list_flagged(
        self, flag: str, *, page: int = 1, per_page: int = 5
    ) -> Page[Product]:
        column = {
            "best_sellers": Product.is_best_seller,
            "new_arrivals": Product.is_new_arrival,
            "featured": Product.is_featured,
        }[flag]
        stmt = self._ordering(self._listed().where(column.is_(True)))
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def search(
        self, query: str, *, page: int = 1, per_page: int = 5, include_hidden: bool = False
    ) -> Page[Product]:
        stmt = select(Product).where(Product.deleted_at.is_(None))
        if not include_hidden:
            stmt = stmt.where(
                Product.status == ProductStatus.ACTIVE,
                Product.available_to_customers.is_(True),
            )
        term = query.strip()
        if term:
            stmt = stmt.where(
                or_(
                    Product.name.ilike(f"%{term}%"),
                    Product.short_description.ilike(f"%{term}%"),
                    Product.sku.ilike(f"%{term}%"),
                )
            )
        return await self.paginate(self._ordering(stmt), page=page, per_page=per_page)

    async def list_for_admin(
        self,
        *,
        status: ProductStatus | None = None,
        category_id: uuid.UUID | None = None,
        page: int = 1,
        per_page: int = 8,
    ) -> Page[Product]:
        stmt = select(Product).where(Product.deleted_at.is_(None))
        if status is not None:
            stmt = stmt.where(Product.status == status)
        if category_id is not None:
            stmt = stmt.where(Product.category_id == category_id)
        return await self.paginate(self._ordering(stmt), page=page, per_page=per_page)

    async def list_for_resellers(
        self, *, page: int = 1, per_page: int = 50, category_id: uuid.UUID | None = None
    ) -> Page[Product]:
        stmt = (
            select(Product)
            .where(
                Product.status == ProductStatus.ACTIVE,
                Product.deleted_at.is_(None),
                Product.available_to_resellers.is_(True),
            )
            .options(selectinload(Product.category))
        )
        if category_id is not None:
            stmt = stmt.where(Product.category_id == category_id)
        return await self.paginate(self._ordering(stmt), page=page, per_page=per_page)

    async def get_for_reseller(self, product_id: uuid.UUID) -> Product | None:
        stmt = (
            select(Product)
            .where(
                Product.id == product_id,
                Product.status == ProductStatus.ACTIVE,
                Product.deleted_at.is_(None),
                Product.available_to_resellers.is_(True),
            )
            .options(selectinload(Product.category))
        )
        return await self.session.scalar(stmt)

    async def get_by_sku(self, sku: str) -> Product | None:
        return await self.session.scalar(select(Product).where(Product.sku == sku))

    async def increment_views(self, product_id: uuid.UUID) -> None:
        await self.session.execute(
            update(Product)
            .where(Product.id == product_id)
            .values(views_count=Product.views_count + 1)
        )

    async def record_sale(self, product_id: uuid.UUID, quantity: int) -> None:
        await self.session.execute(
            update(Product)
            .where(Product.id == product_id)
            .values(sales_count=Product.sales_count + quantity)
        )

    async def add_media(self, product: Product, **kwargs: Any) -> ProductMedia:
        media = ProductMedia(product_id=product.id, **kwargs)
        self.session.add(media)
        await self.session.flush()
        return media

    async def archive(self, product: Product) -> Product:
        """Archive rather than delete: order history must remain readable."""
        product.status = ProductStatus.ARCHIVED
        product.available_to_customers = False
        product.available_to_resellers = False
        product.deleted_at = utcnow()
        await self.session.flush()
        return product


class InventoryRepository(BaseRepository[InventoryItem]):
    model = InventoryItem

    async def available_count(self, product_id: uuid.UUID) -> int:
        stmt = select(func.count(InventoryItem.id)).where(
            InventoryItem.product_id == product_id,
            InventoryItem.status == StockItemStatus.AVAILABLE,
        )
        return int((await self.session.scalar(stmt)) or 0)

    async def counts_by_status(self, product_id: uuid.UUID) -> dict[str, int]:
        stmt = (
            select(InventoryItem.status, func.count(InventoryItem.id))
            .where(InventoryItem.product_id == product_id)
            .group_by(InventoryItem.status)
        )
        rows = await self.session.execute(stmt)
        counts = {status.value: 0 for status in StockItemStatus}
        for status, count in rows:
            counts[status.value if hasattr(status, "value") else str(status)] = count
        return counts

    async def available_counts_for(self, product_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Batch stock lookup so product listings need one query, not N."""
        if not product_ids:
            return {}
        stmt = (
            select(InventoryItem.product_id, func.count(InventoryItem.id))
            .where(
                InventoryItem.product_id.in_(product_ids),
                InventoryItem.status == StockItemStatus.AVAILABLE,
            )
            .group_by(InventoryItem.product_id)
        )
        rows = await self.session.execute(stmt)
        counts = dict.fromkeys(product_ids, 0)
        counts.update({product_id: count for product_id, count in rows})
        return counts

    async def claim_available(
        self, product_id: uuid.UUID, quantity: int
    ) -> list[InventoryItem]:
        """Lock and return up to ``quantity`` available items.

        ``FOR UPDATE SKIP LOCKED`` is what prevents two concurrent buyers from
        being allocated the same stock item: the second transaction skips the
        rows the first has locked instead of blocking or double-reading them.
        SQLite (tests) ignores row locking, so the caller's unique constraint
        on active reservations remains the final backstop.
        """
        stmt = (
            select(InventoryItem)
            .where(
                InventoryItem.product_id == product_id,
                InventoryItem.status == StockItemStatus.AVAILABLE,
            )
            .order_by(InventoryItem.created_at)
            .limit(quantity)
        )
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        return list((await self.session.scalars(stmt)).all())

    async def find_by_fingerprint(
        self, product_id: uuid.UUID, fingerprint: str
    ) -> InventoryItem | None:
        stmt = select(InventoryItem).where(
            InventoryItem.product_id == product_id,
            InventoryItem.fingerprint == fingerprint,
        )
        return await self.session.scalar(stmt)

    async def list_for_product(
        self,
        product_id: uuid.UUID,
        *,
        status: StockItemStatus | None = None,
        page: int = 1,
        per_page: int = 10,
    ) -> Page[InventoryItem]:
        stmt = select(InventoryItem).where(InventoryItem.product_id == product_id)
        if status is not None:
            stmt = stmt.where(InventoryItem.status == status)
        return await self.paginate(
            stmt.order_by(InventoryItem.created_at.desc()), page=page, per_page=per_page
        )

    async def low_stock_products(self, limit: int = 10) -> list[tuple[Product, int]]:
        """Active stock-tracked products at or below their threshold."""
        available = (
            select(
                InventoryItem.product_id.label("product_id"),
                func.count(InventoryItem.id).label("available"),
            )
            .where(InventoryItem.status == StockItemStatus.AVAILABLE)
            .group_by(InventoryItem.product_id)
            .subquery()
        )
        stmt = (
            select(Product, func.coalesce(available.c.available, 0))
            .outerjoin(available, available.c.product_id == Product.id)
            .where(
                Product.status == ProductStatus.ACTIVE,
                Product.deleted_at.is_(None),
                Product.delivery_type == DeliveryType.STOCK_ITEM,
                func.coalesce(available.c.available, 0) <= Product.low_stock_threshold,
            )
            .order_by(func.coalesce(available.c.available, 0))
            .limit(limit)
        )
        return [(product, count) for product, count in (await self.session.execute(stmt))]

    async def record_adjustment(
        self,
        *,
        product_id: uuid.UUID,
        action: str,
        quantity: int,
        actor_id: uuid.UUID | None,
        reason: str = "",
        inventory_item_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> InventoryAdjustment:
        adjustment = InventoryAdjustment(
            product_id=product_id,
            inventory_item_id=inventory_item_id,
            actor_id=actor_id,
            action=action,
            quantity=quantity,
            reason=reason,
            details=details or {},
        )
        self.session.add(adjustment)
        await self.session.flush()
        return adjustment


class ReservationRepository(BaseRepository[InventoryReservation]):
    model = InventoryReservation

    async def active_for_order(self, order_id: uuid.UUID) -> list[InventoryReservation]:
        stmt = select(InventoryReservation).where(
            InventoryReservation.order_id == order_id,
            InventoryReservation.status == ReservationStatus.ACTIVE,
        )
        return list((await self.session.scalars(stmt)).all())

    async def all_for_order(self, order_id: uuid.UUID) -> list[InventoryReservation]:
        stmt = select(InventoryReservation).where(InventoryReservation.order_id == order_id)
        return list((await self.session.scalars(stmt)).all())

    async def expired(self, *, now: datetime | None = None, limit: int = 200) -> list[InventoryReservation]:
        stmt = (
            select(InventoryReservation)
            .where(
                InventoryReservation.status == ReservationStatus.ACTIVE,
                InventoryReservation.expires_at <= (now or utcnow()),
            )
            .order_by(InventoryReservation.expires_at)
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def release(self, reservation: InventoryReservation) -> None:
        """Return the held item to available stock."""
        reservation.status = ReservationStatus.RELEASED
        reservation.released_at = utcnow()
        await self.session.execute(
            update(InventoryItem)
            .where(
                InventoryItem.id == reservation.inventory_item_id,
                InventoryItem.status == StockItemStatus.RESERVED,
            )
            .values(status=StockItemStatus.AVAILABLE, order_item_id=None)
        )
        await self.session.flush()

    async def expire(self, reservation: InventoryReservation) -> None:
        reservation.status = ReservationStatus.EXPIRED
        reservation.released_at = utcnow()
        await self.session.execute(
            update(InventoryItem)
            .where(
                InventoryItem.id == reservation.inventory_item_id,
                InventoryItem.status == StockItemStatus.RESERVED,
            )
            .values(status=StockItemStatus.AVAILABLE, order_item_id=None)
        )
        await self.session.flush()

    async def consume(self, reservation: InventoryReservation) -> None:
        reservation.status = ReservationStatus.CONSUMED
        reservation.consumed_at = utcnow()
        await self.session.execute(
            update(InventoryItem)
            .where(InventoryItem.id == reservation.inventory_item_id)
            .values(status=StockItemStatus.SOLD, sold_at=utcnow())
        )
        await self.session.flush()

    async def purge_for_order(self, order_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(InventoryReservation).where(
                and_(
                    InventoryReservation.order_id == order_id,
                    InventoryReservation.status == ReservationStatus.ACTIVE,
                )
            )
        )
