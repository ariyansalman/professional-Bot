"""Profile, settings, referral and notification screens (40-42)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import Nav, PageCB, ProfileCB
from app.bot.keyboards.common import build, nav_button
from app.bot.keyboards.customer import (
    language_keyboard,
    notifications_keyboard,
    profile_keyboard,
    referral_keyboard,
    settings_keyboard,
)
from app.bot.services.formatting import DIVIDER, esc, money
from app.bot.services.screen import render
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.timeutils import short_date
from app.db.models.user import User
from app.db.repositories.users import NotificationRepository, ReferralRepository
from app.domain.enums import Language
from app.domain.referrals.service import ReferralService
from app.i18n import t
from urllib.parse import quote

log = get_logger(__name__)
router = Router(name="profile")


@router.message(Command("profile"))
async def profile_command(message: Message, user: User, lang: Language) -> None:
    await _profile(message, user, lang)


@router.callback_query(Nav.filter(F.to == "profile"))
async def profile_nav(callback: CallbackQuery, user: User, lang: Language) -> None:
    await _profile(callback, user, lang)


@router.callback_query(ProfileCB.filter(F.action == "view"))
async def profile_view(callback: CallbackQuery, user: User, lang: Language) -> None:
    await _profile(callback, user, lang)


async def _profile(event, user: User, lang: Language) -> None:
    settings = get_settings()
    lines = [
        t("profile.title", lang),
        "",
        t("profile.account", lang),
        DIVIDER,
        "",
        f"{t('profile.orders', lang)}:",
        str(user.orders_count),
        "",
        f"{t('profile.completed', lang)}:",
        str(user.completed_orders_count),
        "",
        f"{t('profile.total_spent', lang)}:",
        money(user.total_spent or 0, "USDT"),
    ]
    if settings.features.referrals_enabled and user.referral_balance:
        lines += ["", f"{t('referral.earned', lang)}:", money(user.referral_balance, "USDT")]
    await render(
        event,
        "\n".join(lines),
        profile_keyboard(lang, referrals_enabled=settings.features.referrals_enabled),
    )


@router.callback_query(ProfileCB.filter(F.action == "settings"))
async def settings_screen(callback: CallbackQuery, user: User, lang: Language) -> None:
    lines = [
        "⚙️ <b>SETTINGS</b>",
        "",
        f"Language: {'English' if lang is Language.EN else 'বাংলা'}",
        f"Notifications: {'on' if user.notifications_enabled else 'off'}",
    ]
    await render(
        callback,
        "\n".join(lines),
        settings_keyboard(lang, notifications_on=user.notifications_enabled),
    )


@router.callback_query(ProfileCB.filter(F.action == "toggle_notifications"))
async def toggle_notifications(callback: CallbackQuery, user: User, lang: Language) -> None:
    user.notifications_enabled = not user.notifications_enabled
    await render(
        callback,
        "\n".join(
            [
                "⚙️ <b>SETTINGS</b>",
                "",
                f"Language: {'English' if lang is Language.EN else 'বাংলা'}",
                f"Notifications: {'on' if user.notifications_enabled else 'off'}",
            ]
        ),
        settings_keyboard(lang, notifications_on=user.notifications_enabled),
        answer_text=t("success.saved", lang),
    )


@router.callback_query(ProfileCB.filter(F.action == "language"))
async def language_screen(callback: CallbackQuery, lang: Language) -> None:
    await render(callback, "🌐 <b>LANGUAGE</b>\n\nChoose your language.", language_keyboard(lang))


@router.callback_query(ProfileCB.filter(F.action == "set_language"))
async def set_language(
    callback: CallbackQuery, callback_data: ProfileCB, user: User
) -> None:
    try:
        new_lang = Language(callback_data.arg)
    except ValueError:
        new_lang = Language.EN
    user.language = new_lang
    log.info("profile.language_changed", user_id=str(user.id), language=new_lang.value)
    await render(
        callback,
        "🌐 <b>LANGUAGE</b>\n\n" + t("success.saved", new_lang),
        language_keyboard(new_lang),
    )


@router.callback_query(Nav.filter(F.to == "referral"))
async def referral_nav(
    callback: CallbackQuery, session: AsyncSession, user: User, lang: Language
) -> None:
    await _referral(callback, session, user, lang)


@router.callback_query(ProfileCB.filter(F.action == "referral"))
async def referral_screen(
    callback: CallbackQuery, session: AsyncSession, user: User, lang: Language
) -> None:
    await _referral(callback, session, user, lang)


async def _referral(event, session: AsyncSession, user: User, lang: Language) -> None:
    if not get_settings().features.referrals_enabled:
        await render(event, t("error.not_found", lang), build([[nav_button(t("btn.home", lang), "home")]]))
        return

    stats = await ReferralService(session).stats(user.id)
    me = await event.bot.get_me()
    link = ReferralService.build_link(me.username, user.referral_code)
    share_url = (
        "https://t.me/share/url?url="
        + quote(link, safe="")
        + "&text="
        + quote("Check out this digital store", safe="")
    )

    lines = [
        t("referral.title", lang),
        "",
        t("referral.body", lang),
        "",
        t("referral.your_link", lang),
        f"<code>{esc(link)}</code>",
        "",
        f"{t('referral.invited', lang)}: {stats['invited']}",
        f"{t('referral.qualified', lang)}: {stats['qualified']}",
        f"{t('referral.earned', lang)}: {money(stats['earned'], 'USDT')}",
    ]
    await render(event, "\n".join(lines), referral_keyboard(lang, share_url))


@router.callback_query(ProfileCB.filter(F.action == "referral_history"))
async def referral_history(
    callback: CallbackQuery, session: AsyncSession, user: User, lang: Language
) -> None:
    page = await ReferralRepository(session).list_for_referrer(user.id, per_page=10)
    if page.is_empty:
        await render(
            callback,
            t("referral.empty", lang),
            build([[nav_button(t("btn.back", lang), "referral")]]),
        )
        return
    lines = ["📜 <b>REFERRAL HISTORY</b>", ""]
    for referral in page.items:
        lines.append(
            f"• {short_date(referral.created_at)} · {referral.status.value} · "
            f"{money(referral.reward_amount, referral.reward_currency)}"
        )
    await render(
        callback,
        "\n".join(lines),
        build([[nav_button(t("btn.back", lang), "referral"), nav_button(t("btn.home", lang), "home")]]),
    )


@router.callback_query(ProfileCB.filter(F.action == "notifications"))
async def notifications_screen(
    callback: CallbackQuery,
    callback_data: ProfileCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    await _notifications(callback, session, user, lang, callback_data.page)


@router.callback_query(PageCB.filter(F.scope == "notifications"))
async def notifications_page(
    callback: CallbackQuery,
    callback_data: PageCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    await _notifications(callback, session, user, lang, callback_data.page)


async def _notifications(
    event, session: AsyncSession, user: User, lang: Language, page: int
) -> None:
    repo = NotificationRepository(session)
    result = await repo.list_for_user(user.id, page=page)
    unread = await repo.unread_count(user.id)

    if result.is_empty:
        await render(
            event,
            t("notifications.empty", lang),
            build([[nav_button(t("btn.back", lang), "profile"), nav_button(t("btn.home", lang), "home")]]),
        )
        return

    lines = [t("notifications.title", lang)]
    if unread:
        lines.append(f"({unread} unread)")
    lines.append("")
    for notification in result.items:
        marker = "🔵" if notification.read_at is None else "⚪"
        lines.append(f"{marker} <b>{esc(notification.title)}</b>")
        if notification.body:
            lines.append(esc(notification.body))
        lines.append(f"<i>{short_date(notification.created_at)}</i>")
        lines.append("")

    await render(
        event,
        "\n".join(lines),
        notifications_keyboard(lang, result, has_unread=bool(unread)),
    )


@router.callback_query(ProfileCB.filter(F.action == "mark_read"))
async def mark_all_read(
    callback: CallbackQuery, session: AsyncSession, user: User, lang: Language
) -> None:
    await NotificationRepository(session).mark_all_read(user.id)
    await _notifications(callback, session, user, lang, 1)
