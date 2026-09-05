"""Inventory service: stock, reservations and allocation.

Concurrency contract:

* Stock is claimed with ``SELECT ... FOR UPDATE SKIP LOCKED`` (PostgreSQL), so
  two concurrent buyers never receive the same item.
* Every reservation is backed by a UNIQUE constraint on
  ``(inventory_item_id, status)``, so even without row locking a second
  ACTIVE reservation on the same item is impossible.
* Reservations expire; the reaper returns lapsed holds to available stock.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import InsufficientStockError, OutOfStockError
from app.core.logging import get_logger
from app.core.security import get_secret_box
from app.core.timeutils import utcnow
from app.db.models.catalog import InventoryItem, InventoryReservation, Product
from app.db.repositories.catalog import (
    InventoryRepository,
    ProductRepository,
    ReservationRepository,
)
from app.domain.enums import DeliveryType, ReservationStatus, StockItemStatus
from app.domain.payments.fingerprint import payload_fingerprint

log = get_logger(__name__)


@dataclass(slots=True)
class StockStatus:
    """What the product card should display."""

    available: int
    is_unlimited: bool
    in_stock: bool
    low_stock: bool

    @property
    def label(self) -> str:
        if not self.in_stock:
            return "out_of_stock"
        return "low_stock" if self.low_stock else "in_stock"


class InventoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.items = InventoryRepository(session)
        self.reservations = ReservationRepository(session)
        self.products = ProductRepository(session)

    # -- availability ------------------------------------------------------

    async def stock_status(self, product: Product) -> StockStatus:
        if product.delivery_type is not DeliveryType.STOCK_ITEM:
            if product.stock_override is None:
                return StockStatus(available=-1, is_unlimited=True, in_stock=True, low_stock=False)
            available = max(product.stock_override, 0)
        else:
            available = await self.items.available_count(product.id)
        return StockStatus(
            available=available,
            is_unlimited=False,
            in_stock=available > 0,
            low_stock=0 < available <= product.low_stock_threshold,
        )

    async def stock_map(self, products: list[Product]) -> dict[uuid.UUID, StockStatus]:
        """Batch stock lookup for a listing page (one query, not N)."""
        tracked = [p for p in products if p.delivery_type is DeliveryType.STOCK_ITEM]
        counts = await self.items.available_counts_for([p.id for p in tracked])
        result: dict[uuid.UUID, StockStatus] = {}
        for product in products:
            if product.delivery_type is not DeliveryType.STOCK_ITEM:
                if product.stock_override is None:
                    result[product.id] = StockStatus(-1, True, True, False)
                    continue
                available = max(product.stock_override, 0)
            else:
                available = counts.get(product.id, 0)
            result[product.id] = StockStatus(
                available=available,
                is_unlimited=False,
                in_stock=available > 0,
                low_stock=0 < available <= product.low_stock_threshold,
            )
        return result

    async def assert_purchasable(self, product: Product, quantity: int) -> None:
        if quantity < product.min_quantity:
            raise InsufficientStockError(
                f"quantity {quantity} below minimum {product.min_quantity}",
                safe_message=f"Minimum order quantity is {product.min_quantity}.",
            )
        if product.max_quantity is not None and quantity > product.max_quantity:
            raise InsufficientStockError(
                f"quantity {quantity} above maximum {product.max_quantity}",
                safe_message=f"Maximum order quantity is {product.max_quantity}.",
            )
        status = await self.stock_status(product)
        if not status.in_stock:
            raise OutOfStockError(f"product {product.id} has no stock")
        if not status.is_unlimited and status.available < quantity:
            raise InsufficientStockError(
                f"requested {quantity}, available {status.available}",
                safe_message=f"Only {status.available} left in stock.",
            )

    # -- reservations ------------------------------------------------------

    async def reserve(
        self,
        *,
        product: Product,
        quantity: int,
        order_id: uuid.UUID,
        order_item_id: uuid.UUID,
        ttl_seconds: int | None = None,
    ) -> list[InventoryReservation]:
        """Hold stock for an order while its payment is in flight.

        Non stock-tracked products need no reservation: their payload is not a
        scarce resource.
        """
        if product.delivery_type is not DeliveryType.STOCK_ITEM:
            return []

        ttl = ttl_seconds or get_settings().payments.reservation_ttl_seconds
        expires_at = utcnow() + timedelta(seconds=ttl)
        claimed = await self.items.claim_available(product.id, quantity)
        if len(claimed) < quantity:
            raise InsufficientStockError(
                f"could not claim {quantity} items for product {product.id}; "
                f"only {len(claimed)} were available",
                safe_message="This product just went out of stock.",
            )

        reservations: list[InventoryReservation] = []
        for item in claimed:
            item.status = StockItemStatus.RESERVED
            item.order_item_id = order_item_id
            reservation = InventoryReservation(
                inventory_item_id=item.id,
                order_id=order_id,
                order_item_id=order_item_id,
                status=ReservationStatus.ACTIVE,
                expires_at=expires_at,
            )
            self.session.add(reservation)
            reservations.append(reservation)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            # Another transaction reserved one of these items first.
            await self.session.rollback()
            raise InsufficientStockError(
                f"reservation conflict for product {product.id}: {exc}",
                safe_message="This product just went out of stock.",
            ) from exc

        log.info(
            "inventory.reserved",
            product_id=str(product.id),
            order_id=str(order_id),
            quantity=len(reservations),
            expires_at=expires_at.isoformat(),
        )
        return reservations

    async def extend_reservations(self, order_id: uuid.UUID, ttl_seconds: int) -> int:
        """Push a reservation's expiry out (used when payment is detected)."""
        expires_at = utcnow() + timedelta(seconds=ttl_seconds)
        active = await self.reservations.active_for_order(order_id)
        for reservation in active:
            reservation.expires_at = expires_at
        await self.session.flush()
        return len(active)

    async def release_for_order(self, order_id: uuid.UUID, *, reason: str = "") -> int:
        released = 0
        for reservation in await self.reservations.active_for_order(order_id):
            await self.reservations.release(reservation)
            released += 1
        if released:
            log.info("inventory.released", order_id=str(order_id), count=released, reason=reason)
        return released

    async def reap_expired(self, *, limit: int = 200) -> int:
        """Return lapsed reservations to available stock."""
        expired = await self.reservations.expired(limit=limit)
        for reservation in expired:
            await self.reservations.expire(reservation)
        if expired:
            log.info("inventory.reservations_expired", count=len(expired))
        return len(expired)

    # -- allocation --------------------------------------------------------

    async def allocate_for_order_item(
        self, *, order_id: uuid.UUID, order_item_id: uuid.UUID, quantity: int, product: Product
    ) -> list[InventoryItem]:
        """Permanently consume the reserved stock for a paid order item.

        Idempotent: items already marked SOLD for this order item are returned
        as-is, so a retried delivery never consumes extra stock.
        """
        if product.delivery_type is not DeliveryType.STOCK_ITEM:
            return []

        reservations = [
            r
            for r in await self.reservations.all_for_order(order_id)
            if r.order_item_id == order_item_id
        ]
        active = [r for r in reservations if r.status is ReservationStatus.ACTIVE]
        consumed = [r for r in reservations if r.status is ReservationStatus.CONSUMED]

        if consumed and not active:
            # Already allocated by an earlier delivery attempt.
            return await self.items.get_many([r.inventory_item_id for r in consumed])

        if len(active) + len(consumed) < quantity:
            # The hold lapsed before payment cleared: take fresh stock. The
            # payment is already verified, so the order must still be filled.
            missing = quantity - len(active) - len(consumed)
            replacement = await self.items.claim_available(product.id, missing)
            if len(replacement) < missing:
                raise InsufficientStockError(
                    f"order {order_id} is paid but only {len(replacement)}/{missing} "
                    "replacement items are available",
                    safe_message="Your order needs manual fulfilment by our team.",
                )
            expires_at = utcnow() + timedelta(hours=1)
            for item in replacement:
                item.status = StockItemStatus.RESERVED
                item.order_item_id = order_item_id
                reservation = InventoryReservation(
                    inventory_item_id=item.id,
                    order_id=order_id,
                    order_item_id=order_item_id,
                    status=ReservationStatus.ACTIVE,
                    expires_at=expires_at,
                )
                self.session.add(reservation)
                active.append(reservation)
            await self.session.flush()

        for reservation in active:
            await self.reservations.consume(reservation)

        item_ids = [r.inventory_item_id for r in [*active, *consumed]]
        items = await self.items.get_many(item_ids)
        for item in items:
            item.order_item_id = order_item_id
        await self.session.flush()
        log.info(
            "inventory.allocated",
            order_id=str(order_id),
            order_item_id=str(order_item_id),
            count=len(items),
        )
        return items

    # -- stock management --------------------------------------------------

    async def add_stock(
        self,
        *,
        product: Product,
        payloads: list[str],
        actor_id: uuid.UUID | None = None,
        reason: str = "manual add",
    ) -> tuple[int, int]:
        """Add stock items. Returns ``(added, skipped_duplicates)``.

        Payloads are encrypted at rest and de-duplicated per product by
        fingerprint, so importing the same file twice cannot create duplicate
        keys that would later be sold to two customers.
        """
        box = get_secret_box()
        added = 0
        skipped = 0
        for raw in payloads:
            payload = raw.strip()
            if not payload:
                continue
            fingerprint = payload_fingerprint(payload)
            if await self.items.find_by_fingerprint(product.id, fingerprint) is not None:
                skipped += 1
                continue
            item = InventoryItem(
                product_id=product.id,
                status=StockItemStatus.AVAILABLE,
                secret_payload=box.encrypt(payload),
                fingerprint=fingerprint,
                preview=self._preview(payload),
                added_by_id=actor_id,
            )
            self.session.add(item)
            added += 1
        await self.session.flush()
        if added:
            await self.items.record_adjustment(
                product_id=product.id,
                action="add_stock",
                quantity=added,
                actor_id=actor_id,
                reason=reason,
                details={"skipped_duplicates": skipped},
            )
        log.info(
            "inventory.stock_added",
            product_id=str(product.id),
            added=added,
            skipped=skipped,
        )
        return added, skipped

    async def mark_invalid(
        self,
        *,
        item: InventoryItem,
        actor_id: uuid.UUID | None,
        reason: str,
    ) -> None:
        """Take a bad key out of circulation. Sold items are never altered."""
        if item.status is StockItemStatus.SOLD:
            raise OutOfStockError(
                "a sold inventory item cannot be invalidated; issue a refund instead",
                safe_message="This item has already been delivered.",
            )
        item.status = StockItemStatus.INVALID
        item.invalid_reason = reason[:255]
        await self.session.flush()
        await self.items.record_adjustment(
            product_id=item.product_id,
            inventory_item_id=item.id,
            action="mark_invalid",
            quantity=-1,
            actor_id=actor_id,
            reason=reason,
        )

    def reveal_payload(self, item: InventoryItem) -> str:
        """Decrypt a stock payload. Callers must never log the result."""
        return get_secret_box().decrypt(item.secret_payload)

    @staticmethod
    def _preview(payload: str) -> str:
        """A non-secret hint for admin lists: last 4 characters only."""
        compact = payload.strip().splitlines()[0] if payload.strip() else ""
        if len(compact) <= 4:
            return "****"
        return f"****{compact[-4:]}"

    async def counts(self, product_id: uuid.UUID) -> dict[str, Any]:
        return await self.items.counts_by_status(product_id)
