"""Customer notifications and broadcast delivery.

Telegram rate limits are respected explicitly: roughly 30 messages/second
globally and about one message/second per chat. The sender is therefore paced,
and ``TelegramRetryAfter`` is honoured rather than retried immediately.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.db.models.payment import PaymentIntent
from app.db.repositories.support import BroadcastRepository
from app.db.repositories.users import NotificationRepository, UserRepository
from app.db.session import session_scope
from app.domain.enums import (
    BroadcastAudience,
    BroadcastStatus,
    Language,
    NotificationKind,
    VerificationOutcome,
)
from app.i18n import t
from app.workers.base import PeriodicWorker

log = get_logger(__name__)


async def notify_payment_result(
    session: AsyncSession, bot: Bot, intent: PaymentIntent, result
) -> None:
    """Tell the customer what happened to their payment.

    The message is chosen from the verification outcome so it always matches
    reality; technical detail stays in the admin panel.
    """
    order = intent.order
    if order is None or order.user_id is None:
        return
    user = await UserRepository(session).get(order.user_id)
    if user is None or user.is_bot_blocked or not user.notifications_enabled:
        return

    lang = user.language
    outcome = result.outcome

    if outcome is VerificationOutcome.VERIFIED:
        kind = NotificationKind.PAYMENT
        title = f"Payment verified — {order.reference}"
        text = "\n".join(
            [
                t("payment.verified_title", lang),
                "",
                f"{t('payment.order', lang)}: #{order.reference}",
                f"{t('payment.amount', lang)}: {intent.received_amount or intent.expected_amount} {intent.asset}",
                "",
                t("payment.verified_body", lang),
            ]
        )
    elif outcome is VerificationOutcome.UNDERPAID:
        kind = NotificationKind.PAYMENT
        title = f"Payment amount mismatch — {order.reference}"
        text = "\n".join([t("payment.underpaid_title", lang), "", t("payment.underpaid_body", lang)])
    elif outcome is VerificationOutcome.OVERPAID:
        kind = NotificationKind.PAYMENT
        title = f"Payment review — {order.reference}"
        text = "\n".join([t("payment.overpaid_title", lang), "", t("payment.overpaid_body", lang)])
    elif outcome is VerificationOutcome.WRONG_NETWORK:
        kind = NotificationKind.PAYMENT
        title = f"Wrong network — {order.reference}"
        text = "\n".join(
            [t("payment.wrong_network_title", lang), "", t("payment.wrong_network_body", lang)]
        )
    elif outcome is VerificationOutcome.DUPLICATE:
        kind = NotificationKind.PAYMENT
        title = f"Transaction already used — {order.reference}"
        text = "\n".join([t("payment.duplicate_title", lang), "", t("payment.duplicate_body", lang)])
    elif result.needs_review:
        kind = NotificationKind.PAYMENT
        title = f"Payment under review — {order.reference}"
        text = "\n".join([t("payment.review_title", lang), "", t("payment.review_body", lang)])
    else:
        return

    await NotificationRepository(session).create(
        user.id, kind=kind, title=title, body=text[:400], payload={"order_id": str(order.id)}
    )
    try:
        await bot.send_message(user.telegram_id, text)
    except TelegramForbiddenError:
        user.is_bot_blocked = True
    except TelegramRetryAfter as exc:
        log.info("notify.rate_limited", retry_after=exc.retry_after)
    except Exception:
        log.warning("notify.send_failed", user_id=str(user.id))


class NotificationWorker(PeriodicWorker):
    """Pushes queued notifications that were not delivered inline."""

    name = "notifications"
    interval = 20.0

    def __init__(self, bot: Bot) -> None:
        super().__init__()
        self.bot = bot
        self._delay = 1.0 / max(get_settings().telegram.global_rate_limit, 1)

    async def run_once(self) -> int:
        sent = 0
        async with session_scope() as session:
            repo = NotificationRepository(session)
            users = UserRepository(session)
            pending = await repo.pending_push(limit=50)

            for notification in pending:
                if self.is_stopping:
                    break
                user = await users.get(notification.user_id)
                notification.pushed_at = utcnow()
                if user is None or user.is_bot_blocked or not user.notifications_enabled:
                    continue
                try:
                    body = f"<b>{notification.title}</b>\n\n{notification.body}"
                    await self.bot.send_message(user.telegram_id, body)
                    sent += 1
                except TelegramForbiddenError:
                    user.is_bot_blocked = True
                except TelegramRetryAfter as exc:
                    # Give the pause back and retry on the next iteration.
                    notification.pushed_at = None
                    await asyncio.sleep(min(exc.retry_after, 30))
                    break
                except Exception:
                    log.warning("notification.send_failed", id=str(notification.id))
                await asyncio.sleep(self._delay)
        return sent


class BroadcastWorker(PeriodicWorker):
    """Sends queued broadcasts, resumably and within Telegram's rate limits.

    Progress is checkpointed after every batch, so a crash resumes from the
    last recipient instead of starting over or skipping people.
    """

    name = "broadcast"
    interval = 30.0
    BATCH = 100

    def __init__(self, bot: Bot) -> None:
        super().__init__()
        self.bot = bot
        self._delay = 1.0 / max(get_settings().telegram.global_rate_limit, 1)

    async def run_once(self) -> int:
        sent = 0
        async with session_scope() as session:
            broadcasts = await BroadcastRepository(session).pending()
            if not broadcasts:
                return 0
            broadcast = broadcasts[0]

            if broadcast.status is BroadcastStatus.QUEUED:
                broadcast.status = BroadcastStatus.SENDING
                broadcast.started_at = utcnow()
                await session.flush()

            users = UserRepository(session)
            import uuid as uuid_module

            after_id = uuid_module.UUID(broadcast.cursor) if broadcast.cursor else None
            language = None
            if broadcast.audience is BroadcastAudience.LANGUAGE:
                raw = (broadcast.audience_filter or {}).get("language")
                language = Language(raw) if raw in {"en", "bn"} else None

            recipients = await users.broadcast_targets(
                audience=broadcast.audience.value,
                language=language,
                after_id=after_id,
                limit=self.BATCH,
            )

            if not recipients:
                broadcast.status = BroadcastStatus.COMPLETED
                broadcast.finished_at = utcnow()
                await session.flush()
                log.info(
                    "broadcast.completed",
                    broadcast_id=str(broadcast.id),
                    sent=broadcast.sent_count,
                    failed=broadcast.failed_count,
                )
                return 0

            for user in recipients:
                if self.is_stopping:
                    break
                try:
                    await self.bot.send_message(user.telegram_id, broadcast.body)
                    broadcast.sent_count += 1
                    sent += 1
                except TelegramForbiddenError:
                    user.is_bot_blocked = True
                    broadcast.blocked_count += 1
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(min(exc.retry_after, 60))
                    break
                except Exception:
                    broadcast.failed_count += 1
                finally:
                    # The cursor advances even on failure so one bad recipient
                    # cannot stall the whole broadcast.
                    broadcast.cursor = str(user.id)
                broadcast.total_recipients = max(
                    broadcast.total_recipients,
                    broadcast.sent_count + broadcast.failed_count + broadcast.blocked_count,
                )
                await asyncio.sleep(self._delay)

            await session.flush()
        return sent


class RestockNotifierWorker(PeriodicWorker):
    """Tells subscribers when an out-of-stock product is available again."""

    name = "restock_notifier"
    interval = 120.0

    def __init__(self, bot: Bot) -> None:
        super().__init__()
        self.bot = bot

    async def run_once(self) -> int:
        if not get_settings().features.restock_notifications_enabled:
            return 0

        notified = 0
        async with session_scope() as session:
            from sqlalchemy import select

            from app.db.models.user import RestockSubscription
            from app.db.repositories.catalog import ProductRepository
            from app.domain.inventory.service import InventoryService

            product_ids = (
                await session.scalars(
                    select(RestockSubscription.product_id)
                    .where(RestockSubscription.notified_at.is_(None))
                    .distinct()
                )
            ).all()

            products = ProductRepository(session)
            inventory = InventoryService(session)
            users = UserRepository(session)

            from app.db.repositories.users import RestockRepository

            restock = RestockRepository(session)

            for product_id in product_ids:
                if self.is_stopping:
                    break
                product = await products.get_active(product_id)
                if product is None:
                    continue
                status = await inventory.stock_status(product)
                if not status.in_stock:
                    continue

                for subscription in await restock.pending_for_product(product_id):
                    user = await users.get(subscription.user_id)
                    subscription.notified_at = utcnow()
                    if user is None or user.is_bot_blocked or not user.notifications_enabled:
                        continue
                    try:
                        await self.bot.send_message(
                            user.telegram_id,
                            f"🔔 <b>Back in stock</b>\n\n{product.name} is available again.",
                        )
                        notified += 1
                    except TelegramForbiddenError:
                        user.is_bot_blocked = True
                    except Exception:
                        log.warning("restock.notify_failed", user_id=str(subscription.user_id))
                    await asyncio.sleep(1.0 / max(get_settings().telegram.global_rate_limit, 1))
        return notified
