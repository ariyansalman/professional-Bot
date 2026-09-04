"""Coupon validation and redemption."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CouponError
from app.core.logging import get_logger
from app.core.money import quantize_money
from app.core.timeutils import ensure_utc, utcnow
from app.db.models.order import Coupon
from app.db.repositories.orders import CouponRepository
from app.domain.enums import CouponType

log = get_logger(__name__)


@dataclass(slots=True)
class CouponEvaluation:
    coupon: Coupon
    discount: Decimal
    new_total: Decimal


class CouponService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.coupons = CouponRepository(session)

    async def evaluate(
        self,
        *,
        code: str,
        user_id: uuid.UUID,
        subtotal: Decimal,
        currency: str,
        product_id: uuid.UUID | None = None,
        category_id: uuid.UUID | None = None,
        is_reseller: bool = False,
    ) -> CouponEvaluation:
        """Validate a coupon against an order and compute its discount.

        Raises :class:`CouponError` with a single generic customer message for
        every failure mode: telling a customer *why* a coupon failed would leak
        campaign configuration and invite probing.
        """
        coupon = await self.coupons.get_by_code(code)
        if coupon is None:
            raise CouponError(f"coupon {code!r} not found")
        if not coupon.is_active:
            raise CouponError(f"coupon {coupon.code} is inactive")

        now = utcnow()
        starts_at = ensure_utc(coupon.starts_at)
        expires_at = ensure_utc(coupon.expires_at)
        if starts_at and now < starts_at:
            raise CouponError(f"coupon {coupon.code} has not started")
        if expires_at and now >= expires_at:
            raise CouponError(f"coupon {coupon.code} expired at {expires_at.isoformat()}")

        if coupon.currency and coupon.currency != currency:
            raise CouponError(
                f"coupon currency {coupon.currency} does not match order currency {currency}"
            )
        if coupon.reseller_only and not is_reseller:
            raise CouponError(f"coupon {coupon.code} is reseller-only")
        if subtotal < coupon.min_order_amount:
            raise CouponError(
                f"order total {subtotal} below coupon minimum {coupon.min_order_amount}"
            )

        if coupon.product_ids and str(product_id) not in {str(p) for p in coupon.product_ids}:
            raise CouponError(f"coupon {coupon.code} does not apply to product {product_id}")
        if coupon.category_ids and str(category_id) not in {
            str(c) for c in coupon.category_ids
        }:
            raise CouponError(f"coupon {coupon.code} does not apply to category {category_id}")

        if coupon.max_redemptions is not None and coupon.redemptions_count >= coupon.max_redemptions:
            raise CouponError(f"coupon {coupon.code} is fully redeemed")

        used_by_user = await self.coupons.usage_count_for_user(coupon.id, user_id)
        if used_by_user >= coupon.max_redemptions_per_user:
            raise CouponError(
                f"user {user_id} already redeemed coupon {coupon.code} "
                f"{used_by_user}/{coupon.max_redemptions_per_user} times"
            )

        discount = self.compute_discount(coupon, subtotal)
        if discount <= 0:
            raise CouponError(f"coupon {coupon.code} yields no discount for this order")

        return CouponEvaluation(
            coupon=coupon,
            discount=discount,
            new_total=quantize_money(subtotal - discount),
        )

    @staticmethod
    def compute_discount(coupon: Coupon, subtotal: Decimal) -> Decimal:
        """Discount, capped at ``max_discount`` and never exceeding the total."""
        if coupon.coupon_type is CouponType.PERCENTAGE:
            raw = (subtotal * coupon.value / Decimal(100)).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP
            )
        else:
            raw = coupon.value
        if coupon.max_discount is not None:
            raw = min(raw, coupon.max_discount)
        return quantize_money(min(raw, subtotal))

    async def redeem(
        self,
        *,
        coupon: Coupon,
        user_id: uuid.UUID,
        order_id: uuid.UUID,
        discount: Decimal,
    ) -> bool:
        """Consume a redemption. Idempotent per order."""
        usage = await self.coupons.redeem(
            coupon=coupon, user_id=user_id, order_id=order_id, discount_amount=discount
        )
        if usage is None:
            log.info("coupon.redeem_duplicate", coupon=coupon.code, order_id=str(order_id))
            return False
        log.info(
            "coupon.redeemed",
            coupon=coupon.code,
            order_id=str(order_id),
            discount=str(discount),
        )
        return True

    async def revert(self, order_id: uuid.UUID) -> None:
        """Give the redemption back when the order is cancelled or expires."""
        await self.coupons.revert(order_id)
