"""Dispatch table for confirmed high-risk actions.

Every destructive admin action follows the same shape: request → reason →
confirm → execute. The execution handlers live in their own sections and are
registered here so the shared confirmation flow can reach them.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.keyboards.panels import admin_back_row
from app.admin.services.context import AdminContext
from app.bot.keyboards.common import build
from app.bot.services.screen import render
from app.core.logging import get_logger

log = get_logger(__name__)

Handler = Callable[[CallbackQuery, AsyncSession, AdminContext, dict[str, Any]], Awaitable[None]]
_REGISTRY: dict[str, Handler] = {}

#: Second step of a high-risk flow: the operator's free-text reply (a reason, an
#: amount, a transaction reference) is turned into a pending confirmation.
#: Exactly one handler owns the ``action_reason`` FSM state; it dispatches here,
#: so two admin sections can share the state without competing for the update.
ReasonHandler = Callable[
    [Message, AsyncSession, AdminContext, str, str, str], Awaitable[None]
]
_REASON_REGISTRY: dict[str, ReasonHandler] = {}


def register(action: str) -> Callable[[Handler], Handler]:
    def decorator(handler: Handler) -> Handler:
        _REGISTRY[action] = handler
        return handler

    return decorator


def register_reason(action: str) -> Callable[[ReasonHandler], ReasonHandler]:
    """Register the reason-capture step for a pending action."""

    def decorator(handler: ReasonHandler) -> ReasonHandler:
        _REASON_REGISTRY[action] = handler
        return handler

    return decorator


async def dispatch_reason(
    message: Message,
    session: AsyncSession,
    admin: AdminContext,
    action: str,
    target: str,
    text: str,
) -> None:
    handler = _REASON_REGISTRY.get(action)
    if handler is None:
        log.warning("admin.unknown_reason_action", action=action)
        await render(message, "⚠️ That action expired.", build([admin_back_row()]))
        return
    await handler(message, session, admin, action, target, text)


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
