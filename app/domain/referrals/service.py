"""Referral attribution, qualification and reward payout.

Abuse controls (section 42):

* self-referral is impossible - the referrer and referred user are compared
* attribution is one-shot: a user can only ever be referred once, enforced by a
  UNIQUE constraint on ``referrals.referred_user_id``
* attribution only happens for a genuinely new account
* a reward is only granted when a referred user's order actually completes,
  and the payout is journalled once via the ledger dedupe key
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.money import quantize_money
from app.core.timeutils import utcnow
from app.db.models.order import Order
from app.db.models.user import Referral, User
from app.db.repositories.orders import LedgerRepository
from app.db.repositories.users import ReferralRepository, UserRepository
from app.domain.enums import LedgerEntryType, NotificationKind, ReferralStatus

log = get_logger(__name__)

#: Default reward: a percentage of the referred user's first completed order.
DEFAULT_REWARD_PERCENT = Decimal("5")


class ReferralService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.referrals = ReferralRepository(session)
        self.users = UserRepository(session)
        self.ledger = LedgerRepository(session)

    async def attribute(
        self,
        *,
        new_user: User,
        referral_code: str,
        signals: dict[str, Any] | None = None,
    ) -> Referral | None:
        """Link a new user to their referrer. Returns None when not allowed."""
        code = referral_code.strip().upper()
        if not code:
            return None

        referrer = await self.users.get_by_referral_code(code)
        if referrer is None:
            log.info("referral.unknown_code", code=code)
            return None
        if referrer.id == new_user.id:
            log.warning("referral.self_referral_blocked", user_id=str(new_user.id))
            return None
        if new_user.referred_by_id is not None:
            return None
        if await self.referrals.get_for_user(new_user.id) is not None:
            return None

        referral = Referral(
            referrer_id=referrer.id,
            referred_user_id=new_user.id,
            status=ReferralStatus.PENDING,
            signals=signals or {},
        )
        self.session.add(referral)
        new_user.referred_by_id = referrer.id
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            return None
        log.info(
            "referral.attributed",
            referrer_id=str(referrer.id),
            referred_id=str(new_user.id),
        )
        return referral

    async def qualify_order(
        self, order: Order, *, reward_percent: Decimal = DEFAULT_REWARD_PERCENT
    ) -> Referral | None:
        """Grant the referral reward once a referred user's order completes."""
        if order.user_id is None:
            return None
        referral = await self.referrals.get_for_user(order.user_id)
        if referral is None or referral.status is not ReferralStatus.PENDING:
            return None

        reward = quantize_money(order.total * reward_percent / Decimal(100))
        if reward <= 0:
            return None

        referral.status = ReferralStatus.REWARDED
        referral.qualifying_order_id = order.id
        referral.reward_amount = reward
        referral.reward_currency = order.currency
        referral.rewarded_at = utcnow()

        referrer = await self.users.get(referral.referrer_id)
        if referrer is not None:
            referrer.referral_balance = (referrer.referral_balance or Decimal("0")) + reward

        # Journal the payout once; a retried completion cannot double-credit.
        await self.ledger.record(
            entry_type=LedgerEntryType.REFERRAL_REWARD,
            amount=-reward,
            currency=order.currency,
            dedupe_key=f"referral:{referral.id}",
            order_id=order.id,
            user_id=referral.referrer_id,
            description=f"Referral reward for order {order.reference}",
            details={"reward_percent": str(reward_percent)},
            correlation_id=order.correlation_id,
        )
        await self.session.flush()
        log.info(
            "referral.rewarded",
            referral_id=str(referral.id),
            reward=str(reward),
            order=order.reference,
        )
        return referral

    async def stats(self, user_id: uuid.UUID) -> dict[str, Any]:
        return await self.referrals.stats(user_id)

    @staticmethod
    def build_link(bot_username: str, referral_code: str) -> str:
        return f"https://t.me/{bot_username}?start=ref_{referral_code}"

    @staticmethod
    def reward_notification(reward: Decimal, currency: str) -> tuple[NotificationKind, str, str]:
        return (
            NotificationKind.REFERRAL,
            "Referral reward earned",
            f"You earned {reward} {currency} from a referral purchase.",
        )
