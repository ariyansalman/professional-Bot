"""Injects a database session, the current user and a correlation id.

Every update runs inside one transactional scope, so a handler either commits a
consistent change set or nothing at all.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User as TgUser

from app.core.correlation import correlation_scope
from app.core.logging import get_logger
from app.db.session import session_scope
from app.db.repositories.users import UserRepository
from app.domain.enums import Language, UserStatus
from app.i18n import t

log = get_logger(__name__)


class ContextMiddleware(BaseMiddleware):
    """Opens a session, resolves the user, and binds a correlation id."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return None

        with correlation_scope(prefix="tg") as correlation_id:
            async with session_scope() as session:
                repo = UserRepository(session)
                user, created = await repo.get_or_create(
                    telegram_id=tg_user.id,
                    username=tg_user.username,
                    first_name=tg_user.first_name,
                    last_name=tg_user.last_name,
                    language=_detect_language(tg_user.language_code),
                )

                if user.status is UserStatus.BANNED:
                    await _reject(event, t("error.banned", user.language))
                    return None

                data["session"] = session
                data["user"] = user
                data["lang"] = user.language
                data["is_new_user"] = created
                data["correlation_id"] = correlation_id
                return await handler(event, data)


def _detect_language(code: str | None) -> Language:
    if code and code.lower().startswith("bn"):
        return Language.BN
    return Language.EN


async def _reject(event: TelegramObject, text: str) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    elif isinstance(event, Message):
        await event.answer(text)
