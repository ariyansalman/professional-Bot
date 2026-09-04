"""Shared keyboard builders.

Every screen's keyboard is built here so navigation stays consistent: Back
always goes to the previous logical screen and Home always returns to the
customer home, exactly as section 75 requires.
"""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.callbacks import Nav, NoopCB, PageCB
from app.db.repositories.base import Page
from app.domain.enums import Language
from app.i18n import t


def button(text: str, callback: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback)


def nav_button(text: str, to: str, arg: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=Nav(to=to, arg=arg).pack())


def back_home_row(
    lang: Language, *, back_to: str | None = None, back_arg: str = "", home: bool = True
) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if back_to:
        row.append(nav_button(t("btn.back", lang), back_to, back_arg))
    if home:
        row.append(nav_button(t("btn.home", lang), "home"))
    return row


def pagination_row(
    page: Page, scope: str, *, arg: str = ""
) -> list[InlineKeyboardButton]:
    """``[◀] [1/5] [▶]`` - arrows are omitted when there is nowhere to go."""
    if page.pages <= 1:
        return []
    row: list[InlineKeyboardButton] = []
    if page.has_prev:
        row.append(
            InlineKeyboardButton(
                text="◀", callback_data=PageCB(scope=scope, page=page.page - 1, arg=arg).pack()
            )
        )
    row.append(
        InlineKeyboardButton(
            text=page.label, callback_data=NoopCB(tag="page").pack()
        )
    )
    if page.has_next:
        row.append(
            InlineKeyboardButton(
                text="▶", callback_data=PageCB(scope=scope, page=page.page + 1, arg=arg).pack()
            )
        )
    return row


def build(rows: Sequence[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    """Drop empty rows so a keyboard never renders a blank line."""
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows if row])


def single(text: str, to: str, arg: str = "") -> InlineKeyboardMarkup:
    return build([[nav_button(text, to, arg)]])


def error_keyboard(lang: Language, *, retry_to: str | None = None, arg: str = "") -> InlineKeyboardMarkup:
    """The standard error recovery keyboard: Retry + Support + Home."""
    rows: list[list[InlineKeyboardButton]] = []
    if retry_to:
        rows.append([nav_button(t("btn.retry", lang), retry_to, arg)])
    rows.append(
        [
            nav_button(t("btn.support", lang), "support"),
            nav_button(t("btn.home", lang), "home"),
        ]
    )
    return build(rows)


def confirm_keyboard(
    lang: Language, token: str, *, yes_text: str | None = None, no_text: str | None = None
) -> InlineKeyboardMarkup:
    from app.bot.callbacks import ConfirmCB

    return build(
        [
            [
                button(yes_text or "✅ Confirm", ConfirmCB(token=token, decision="yes").pack()),
                button(no_text or t("btn.cancel", lang), ConfirmCB(token=token, decision="no").pack()),
            ]
        ]
    )


def chunk(buttons: Sequence[InlineKeyboardButton], per_row: int = 2) -> list[list[InlineKeyboardButton]]:
    return [list(buttons[i : i + per_row]) for i in range(0, len(buttons), per_row)]


def builder() -> InlineKeyboardBuilder:
    return InlineKeyboardBuilder()
