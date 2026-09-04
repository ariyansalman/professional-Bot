"""Screen rendering helper.

Section 82 requires editing the existing message rather than sending a new one.
:func:`render` does exactly that, and transparently falls back to sending when
editing is impossible (the message is a photo, is too old, or was deleted).

It also normalises the "message is not modified" case, which Telegram treats as
an error but which is a perfectly ordinary outcome when a customer taps Refresh
and nothing has changed.
"""

from __future__ import annotations

from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from app.core.logging import get_logger

log = get_logger(__name__)


async def render(
    event: Message | CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
    *,
    photo: str | None = None,
    disable_preview: bool = True,
    answer_text: str | None = None,
    show_alert: bool = False,
) -> Message | None:
    """Show a screen, editing in place wherever possible."""
    if isinstance(event, CallbackQuery):
        # Always answer the callback so the client stops its spinner.
        try:
            await event.answer(answer_text or "", show_alert=show_alert)
        except TelegramBadRequest:
            pass  # already answered or expired
        message = event.message
        if message is None:
            return None
        return await _edit_or_send(message, text, keyboard, photo, disable_preview)

    return await event.answer(
        text,
        reply_markup=keyboard,
        disable_web_page_preview=disable_preview,
    )


async def _edit_or_send(
    message: Message,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
    photo: str | None,
    disable_preview: bool,
) -> Message | None:
    has_media = bool(message.photo or message.video or message.document)

    try:
        if photo:
            if has_media:
                return await message.edit_media(
                    media=InputMediaPhoto(media=photo, caption=text),
                    reply_markup=keyboard,
                )
            # Text -> photo requires a new message; replace the old screen.
            await _safe_delete(message)
            return await message.answer_photo(photo, caption=text, reply_markup=keyboard)

        if has_media:
            return await message.edit_caption(caption=text, reply_markup=keyboard)

        return await message.edit_text(
            text, reply_markup=keyboard, disable_web_page_preview=disable_preview
        )

    except TelegramBadRequest as exc:
        detail = str(exc)
        if "message is not modified" in detail:
            # Nothing changed: refresh only the keyboard if it differs.
            try:
                await message.edit_reply_markup(reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            return message
        if "message can't be edited" in detail or "message to edit not found" in detail:
            return await message.answer(
                text, reply_markup=keyboard, disable_web_page_preview=disable_preview
            )
        raise


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def loading(event: CallbackQuery, text: str) -> None:
    """Show a lightweight loading state on the callback toast.

    Used for operations that take long enough to notice but not long enough to
    justify replacing the whole screen.
    """
    try:
        await event.answer(text[:200])
    except TelegramBadRequest:
        pass


def format_lines(*parts: Any) -> str:
    """Join screen sections, collapsing empties so no blank gaps appear."""
    return "\n".join(str(part) for part in parts if part not in (None, ""))
