"""Financial integrity audit (specification section 129).

Runs the invariants that must hold in any healthy deployment. Intended to be
run against production periodically, and after any restore.

    python -m scripts.audit_financial

Exit code 0 means every invariant holds.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select

from app.core.logging import configure_logging, get_logger
from app.db.models.order import Delivery, LedgerEntry, Order
from app.db.models.payment import (
    PaymentConsumption,
    PaymentIntent,
)
from app.db.session import dispose_engine, read_session
from app.domain.enums import DeliveryStatus, OrderStatus, PaymentStatus

log = get_logger(__name__)

OK = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"


class Audit:
    def __init__(self) -> None:
        self.violations: list[tuple[str, int]] = []
        self.checks = 0

    def record(self, name: str, offenders: int, detail: str = "") -> None:
        self.checks += 1
        ok = offenders == 0
        suffix = "" if ok else f" — {offenders} violation(s)" + (f": {detail}" if detail else "")
        print(f"  {OK if ok else BAD} {name}{suffix}")
        if not ok:
            self.violations.append((name, offenders))

    async def run(self) -> int:
        async with read_session() as session:
            await self._payments(session)
            await self._deliveries(session)
            await self._inventory(session)
            await self._ledger(session)

        print()
        if self.violations:
            print(f"{BAD} {len(self.violations)}/{self.checks} invariants violated")
            return 1
        print(f"{OK} all {self.checks} invariants hold")
        return 0

    async def _count(self, session, stmt) -> int:
        return int((await session.scalar(select(func.count()).select_from(stmt.subquery()))) or 0)

    async def _payments(self, session) -> None:
        print("\nPayments")

        # Every payment intent belongs to an order.
        orphans = select(PaymentIntent).outerjoin(
            Order, PaymentIntent.order_id == Order.id
        ).where(Order.id.is_(None))
        self.record("every payment intent has an order", await self._count(session, orphans))

        # A transaction is consumed at most once. Enforced by a UNIQUE index;
        # this proves the index is actually present and effective.
        duplicates = (
            select(PaymentConsumption.fingerprint)
            .group_by(PaymentConsumption.fingerprint)
            .having(func.count(PaymentConsumption.id) > 1)
        )
        self.record(
            "no transaction is consumed twice", await self._count(session, duplicates)
        )

        # An order holds at most one consumed transaction.
        multi = (
            select(PaymentConsumption.order_id)
            .group_by(PaymentConsumption.order_id)
            .having(func.count(PaymentConsumption.id) > 1)
        )
        self.record("no order consumes two transactions", await self._count(session, multi))

        # A verified payment must have received at least the expected amount.
        short = select(PaymentIntent).where(
            PaymentIntent.status == PaymentStatus.VERIFIED,
            PaymentIntent.received_amount.is_not(None),
            PaymentIntent.received_amount < PaymentIntent.expected_amount,
        )
        self.record("no verified payment is underpaid", await self._count(session, short))

        # A paid order must have a verified payment intent.
        unbacked = (
            select(Order)
            .outerjoin(
                PaymentIntent,
                (PaymentIntent.order_id == Order.id)
                & (PaymentIntent.status == PaymentStatus.VERIFIED),
            )
            .where(
                Order.status.in_(
                    [
                        OrderStatus.PAYMENT_VERIFIED,
                        OrderStatus.FULFILLING,
                        OrderStatus.DELIVERED,
                        OrderStatus.COMPLETED,
                    ]
                ),
                PaymentIntent.id.is_(None),
            )
        )
        self.record("every paid order has a verified payment", await self._count(session, unbacked))

    async def _deliveries(self, session) -> None:
        print("\nDeliveries")

        # Nothing is delivered without a verified payment. This is the single
        # most important invariant in the platform.
        unpaid = (
            select(Delivery)
            .join(Order, Delivery.order_id == Order.id)
            .outerjoin(
                PaymentIntent,
                (PaymentIntent.order_id == Order.id)
                & (PaymentIntent.status == PaymentStatus.VERIFIED),
            )
            .where(
                Delivery.status == DeliveryStatus.COMPLETED,
                PaymentIntent.id.is_(None),
            )
        )
        self.record("no completed delivery lacks a verified payment", await self._count(session, unpaid))

        # One delivery per order item, enforced by a UNIQUE constraint.
        duplicated = (
            select(Delivery.order_item_id)
            .group_by(Delivery.order_item_id)
            .having(func.count(Delivery.id) > 1)
        )
        self.record("no order item is delivered twice", await self._count(session, duplicated))

    async def _inventory(self, session) -> None:
        print("\nInventory")
        from app.db.models.catalog import InventoryItem, InventoryReservation
        from app.domain.enums import ReservationStatus, StockItemStatus

        # An item cannot be held by two active reservations.
        double_held = (
            select(InventoryReservation.inventory_item_id)
            .where(InventoryReservation.status == ReservationStatus.ACTIVE)
            .group_by(InventoryReservation.inventory_item_id)
            .having(func.count(InventoryReservation.id) > 1)
        )
        self.record("no stock item has two active reservations", await self._count(session, double_held))

        # A sold item must be attached to an order item.
        unattached = select(InventoryItem).where(
            InventoryItem.status == StockItemStatus.SOLD,
            InventoryItem.order_item_id.is_(None),
        )
        self.record("every sold item belongs to an order", await self._count(session, unattached))

        # An available item must not be attached to an order item.
        leaked = select(InventoryItem).where(
            InventoryItem.status == StockItemStatus.AVAILABLE,
            InventoryItem.order_item_id.is_not(None),
        )
        self.record("no available item is attached to an order", await self._count(session, leaked))

    async def _ledger(self, session) -> None:
        print("\nLedger")

        duplicated = (
            select(LedgerEntry.dedupe_key)
            .group_by(LedgerEntry.dedupe_key)
            .having(func.count(LedgerEntry.id) > 1)
        )
        self.record("no financial event is journalled twice", await self._count(session, duplicated))

        # Every verified payment is journalled.
        unjournalled = (
            select(PaymentIntent)
            .outerjoin(
                LedgerEntry,
                (LedgerEntry.payment_intent_id == PaymentIntent.id)
                & (LedgerEntry.entry_type == "payment_verified"),
            )
            .where(PaymentIntent.status == PaymentStatus.VERIFIED, LedgerEntry.id.is_(None))
        )
        self.record("every verified payment is journalled", await self._count(session, unjournalled))


def main() -> None:
    configure_logging("WARNING", json_output=False)
    print("Financial integrity audit")
    code = asyncio.run(_run())
    sys.exit(code)


async def _run() -> int:
    audit = Audit()
    try:
        return await audit.run()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    main()
