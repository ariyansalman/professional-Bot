"""Catch-all handlers: unknown callbacks, stray text, inert buttons."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import NoopCB
from app.bot.keyboards.common import build, nav_button
from app.bot.services.screen import render
from app.core.logging import get_logger
from app.domain.enums import Language
from app.i18n import t

log = get_logger(__name__)
router = Router(name="common")


@router.callback_query(NoopCB.filter())
async def noop(callback: CallbackQuery) -> None:
    """Page indicators and section headers do nothing when tapped."""
    await callback.answer()


@router.message(Command("help"))
async def help_command(message: Message, lang: Language) -> None:
    lines = [
        "ℹ️ <b>HELP</b>",
        "",
        "/start — open the store",
        "/shop — browse products",
        "/orders — your orders",
        "/profile — your account",
        "/support — contact support",
        "",
        "Use the buttons to navigate. You can return home at any time.",
    ]
    await render(
        message,
        "\n".join(lines),
        build(
            [
                [nav_button(t("btn.shop", lang), "shop")],
                [nav_button(t("btn.support", lang), "support"), nav_button(t("btn.home", lang), "home")],
            ]
        ),
    )


@router.callback_query()
async def unknown_callback(callback: CallbackQuery, lang: Language) -> None:
    """A stale button from an old message.

    Rather than silently doing nothing, tell the customer the screen expired
    and give them a way back.
    """
    log.info("bot.unknown_callback", data=(callback.data or "")[:64])
    await render(
        callback,
        t("error.expired_session", lang),
        build([[nav_button(t("btn.home", lang), "home")]]),
        answer_text=t("error.expired_session", lang)[:60],
    )


@router.message(F.text)
async def fallback_text(message: Message, lang: Language, state: FSMContext) -> None:
    """Free text outside a flow: guide the customer back to the buttons."""
    if await state.get_state() is not None:
        return
    await render(
        message,
        "\n".join(
            [
                "👋 Use the buttons below to navigate.",
                "",
                "Send /start at any time to return to the store.",
            ]
        ),
        build(
            [
                [nav_button(t("btn.shop", lang), "shop")],
                [nav_button(t("btn.my_orders", lang), "orders"), nav_button(t("btn.home", lang), "home")],
            ]
        ),
    )
