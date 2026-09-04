"""Per-user throttling and duplicate-callback suppression.

Two distinct protections:

* a per-user rate limit that keeps one person from hammering the bot
* callback de-duplication, so double-tapping a button (a very common mobile
  gesture) does not run an action twice
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.core.logging import get_logger
from app.core.redis import dedupe, rate_limit
from app.domain.enums import Language
from app.i18n import t

log = get_logger(__name__)


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, *, limit: int = 20, window: int = 10) -> None:
        self.limit = limit
        self.window = window

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        lang: Language = data.get("lang", Language.EN)
        allowed, retry_after = await rate_limit(
            f"tg:{tg_user.id}", self.limit, self.window, raise_on_limit=False
        )
        if not allowed:
            if isinstance(event, CallbackQuery):
                await event.answer(t("error.rate_limited", lang), show_alert=True)
            elif isinstance(event, Message):
                await event.answer(t("error.rate_limited", lang))
            log.info("bot.throttled", telegram_id=tg_user.id, retry_after=retry_after)
            return None

        # A repeated tap on the same button within a couple of seconds is a
        # double-tap, not a second intent.
        if isinstance(event, CallbackQuery) and event.data:
            key = f"cb:{tg_user.id}:{event.message.message_id if event.message else 0}:{event.data}"
            if not await dedupe(key, ttl=2):
                await event.answer()
                return None

        return await handler(event, data)
