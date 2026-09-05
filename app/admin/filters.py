"""Admin access filters.

``IsAdmin`` resolves the operator's roles and injects an :class:`AdminContext`.
Every handler then re-checks its specific permission - being in the admin panel
is never itself authorisation to perform an action.
"""

from __future__ import annotations

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.services.context import AdminContext, load_admin_context
from app.core.logging import get_logger
from app.db.models.user import User

log = get_logger(__name__)


class IsAdmin(BaseFilter):
    async def __call__(
        self,
        event: TelegramObject,
        session: AsyncSession | None = None,
        user: User | None = None,
        **_: Any,
    ) -> bool | dict[str, AdminContext]:
        if session is None or user is None:
            return False
        context = await load_admin_context(session, user)
        if context is None:
            if isinstance(event, (Message, CallbackQuery)):
                log.info("admin.access_denied", telegram_id=user.telegram_id)
            return False
        return {"admin": context}
