"""/start, welcome and home screens (sections 9-10)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import Nav
from app.bot.keyboards.customer import (
    active_payment_keyboard,
    home_keyboard,
    welcome_keyboard,
)
from app.bot.services.formatting import DIVIDER, esc, money
from app.bot.services.screen import render
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.money import format_amount
from app.core.timeutils import format_countdown
from app.db.models.user import User
from app.db.repositories.catalog import ProductRepository
from app.db.repositories.orders import OrderRepository
from app.db.repositories.payments import PaymentIntentRepository
from app.db.repositories.users import NotificationRepository
from app.domain.enums import Language, OrderStatus
from app.domain.referrals.service import ReferralService
from app.i18n import t

log = get_logger(__name__)
router = Router(name="start")


@router.message(CommandStart(deep_link=True))
async def start_with_payload(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    user: User,
    lang: Language,
    is_new_user: bool,
    state: FSMContext,
) -> None:
    """Handles ``/start ref_XXXX`` referral deep links."""
    await state.clear()
    payload = (command.args or "").strip()

    if payload.startswith("ref_") and get_settings().features.referrals_enabled:
        code = payload[4:]
        # Attribution only ever applies to a genuinely new account.
        if is_new_user:
            referral = await ReferralService(session).attribute(
                new_user=user,
                referral_code=code,
                signals={"source": "deep_link", "telegram_id": user.telegram_id},
            )
            if referral is not None:
                log.info("start.referral_attributed", user_id=str(user.id), code=code)
        else:
            log.info("start.referral_ignored_existing_user", user_id=str(user.id))

    await _welcome(message, session, user, lang, first_time=is_new_user)


@router.message(CommandStart())
async def start(
    message: Message,
    session: AsyncSession,
    user: User,
    lang: Language,
    is_new_user: bool,
    state: FSMContext,
) -> None:
    await state.clear()
    await _welcome(message, session, user, lang, first_time=is_new_user)


@router.message(Command("home"))
async def home_command(
    message: Message, session: AsyncSession, user: User, lang: Language, state: FSMContext
) -> None:
    await state.clear()
    await _home(message, session, user, lang)


@router.callback_query(Nav.filter(F.to == "home"))
async def home_callback(
    callback: CallbackQuery, session: AsyncSession, user: User, lang: Language, state: FSMContext
) -> None:
    await state.clear()
    await _home(callback, session, user, lang)


@router.callback_query(Nav.filter(F.to == "how_it_works"))
async def how_it_works(callback: CallbackQuery, lang: Language) -> None:
    from app.bot.keyboards.common import build, nav_button

    text = "\n".join(
        [
            "📖 <b>HOW IT WORKS</b>",
            "",
            "<b>1. Choose a product</b>",
            "Browse the shop and open any product for full details.",
            "",
            "<b>2. Place your order</b>",
            "Confirm the quantity and apply a coupon if you have one.",
            "",
            "<b>3. Pay</b>",
            "Pick an exchange or a blockchain network. You'll get the exact "
            "amount and destination to send.",
            "",
            "<b>4. Automatic verification</b>",
            "We verify your transaction on-chain or with the exchange. Nothing "
            "is marked as paid until it is independently confirmed.",
            "",
            "<b>5. Instant delivery</b>",
            "Once the payment is confirmed, your product is delivered here in "
            "the chat automatically.",
        ]
    )
    await render(
        callback,
        text,
        build(
            [
                [nav_button(t("btn.start_shopping", lang), "shop")],
                [nav_button(t("btn.support", lang), "support"), nav_button(t("btn.home", lang), "home")],
            ]
        ),
    )


async def _welcome(
    event: Message | CallbackQuery,
    session: AsyncSession,
    user: User,
    lang: Language,
    *,
    first_time: bool,
) -> None:
    if first_time:
        text = "\n".join([t("welcome.title", lang), "", t("welcome.body", lang)])
        await render(event, text, welcome_keyboard(lang, first_time=True))
        return

    # A returning customer gets a status summary, not the onboarding copy again.
    orders = OrderRepository(session)
    active = await orders.active_for_user(user.id)
    lines = [t("welcome.back_title", lang), ""]

    if active is not None:
        intent = await PaymentIntentRepository(session).active_for_order(active.id)
        if intent is not None:
            await _render_active_payment(event, active, intent, lang)
            return

    facts: list[str] = []
    if user.orders_count:
        facts.append(f"• {user.completed_orders_count} completed orders")
    if user.total_spent and user.total_spent > 0:
        facts.append(f"• {format_amount(user.total_spent)} spent")
    product_count = await ProductRepository(session).search("", per_page=1)
    if product_count.total:
        facts.append(f"• {product_count.total} products available")
    if facts:
        lines += ["You have:", *facts]

    await render(event, "\n".join(lines), welcome_keyboard(lang, first_time=False))


async def _render_active_payment(event, order, intent, lang: Language) -> None:
    text = "\n".join(
        [
            t("home.active_payment", lang),
            "",
            f"{t('payment.order', lang)} #{esc(order.reference)}",
            money(intent.expected_amount, intent.asset),
            t("home.waiting_payment", lang),
            "",
            f"{t('payment.expires_in', lang)}: {format_countdown(intent.expires_at)}",
        ]
    )
    await render(event, text, active_payment_keyboard(lang, order))


async def _home(
    event: Message | CallbackQuery, session: AsyncSession, user: User, lang: Language
) -> None:
    """Home shows only sections that actually have content (section 10)."""
    settings = get_settings()
    orders = OrderRepository(session)
    products = ProductRepository(session)
    notifications = NotificationRepository(session)

    unread = await notifications.unread_count(user.id)
    active = await orders.active_for_user(user.id)
    featured = await products.list_featured(limit=3)

    lines = [t("home.title", lang), "", t("home.greeting", lang, name=esc(user.first_name or ""))]

    if active is not None:
        intent = await PaymentIntentRepository(session).active_for_order(active.id)
        if intent is not None:
            lines += [
                "",
                t("home.active_payment", lang),
                f"#{esc(active.reference)} · {money(intent.expected_amount, intent.asset)}",
                f"{t('payment.expires_in', lang)}: {format_countdown(intent.expires_at)}",
            ]
        elif active.status is OrderStatus.CREATED:
            lines += ["", f"🧾 Unpaid order #{esc(active.reference)}"]

    if featured:
        lines += ["", t("home.featured", lang), DIVIDER]
        for product in featured:
            lines.append(
                f"• {esc(product.display_name(lang.value))} — {money(product.price, product.currency)}"
            )

    if unread:
        lines += ["", f"🔔 {unread} unread notification(s)"]

    await render(
        event,
        "\n".join(lines),
        home_keyboard(
            lang,
            unread=unread,
            reseller_enabled=settings.features.reseller_enabled,
            referrals_enabled=settings.features.referrals_enabled,
        ),
    )
