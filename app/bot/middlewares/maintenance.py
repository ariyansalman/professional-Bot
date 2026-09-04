"""Maintenance mode gate.

When maintenance is on, customers see a notice and staff keep working. Workers
are unaffected: in-flight payments must keep being verified and delivered even
while the storefront is closed, so no active order is corrupted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.core.logging import get_logger
from app.db.repositories.support import SettingsRepository
from app.domain.enums import Language
from app.i18n import t

log = get_logger(__name__)

MAINTENANCE_KEY = "maintenance.enabled"
MAINTENANCE_MESSAGE_KEY = "maintenance.message"


class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session = data.get("session")
        user = data.get("user")
        if session is None or user is None:
            return await handler(event, data)

        settings_repo = SettingsRepository(session)
        enabled = bool(await settings_repo.get_value(MAINTENANCE_KEY, False))
        if not enabled:
            return await handler(event, data)

        # Staff retain full access so they can fix whatever caused it.
        if user.is_staff:
            data["maintenance_mode"] = True
            return await handler(event, data)

        lang: Language = data.get("lang", Language.EN)
        custom = await settings_repo.get_value(MAINTENANCE_MESSAGE_KEY, None)
        text = custom or t("maintenance.notice", lang)
        if isinstance(event, CallbackQuery):
            await event.answer(text[:200], show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)
        return None
