"""Dispatch table for confirmed high-risk actions.

Every destructive admin action follows the same shape: request → reason →
confirm → execute. The execution handlers live in their own sections and are
registered here so the shared confirmation flow can reach them.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.keyboards.panels import admin_back_row
from app.admin.services.context import AdminContext
from app.bot.keyboards.common import build
from app.bot.services.screen import render
from app.core.logging import get_logger

log = get_logger(__name__)

Handler = Callable[[CallbackQuery, AsyncSession, AdminContext, dict[str, Any]], Awaitable[None]]
_REGISTRY: dict[str, Handler] = {}


def register(action: str) -> Callable[[Handler], Handler]:
    def decorator(handler: Handler) -> Handler:
        _REGISTRY[action] = handler
        return handler

    return decorator


async def dispatch_confirmation(
    callback: CallbackQuery,
    session: AsyncSession,
    admin: AdminContext,
    action: str,
    payload: dict[str, Any],
) -> None:
    handler = _REGISTRY.get(action)
    if handler is None:
        log.warning("admin.unknown_confirmation", action=action)
        await render(
            callback, "⚠️ That action is no longer available.", build([admin_back_row()])
        )
        return
    await handler(callback, session, admin, payload)


def target_uuid(payload: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(str(payload.get("target")))
