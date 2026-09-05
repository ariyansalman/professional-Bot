"""Converts every exception into a safe customer message.

Rule from section 79: a stack trace, SQL error, provider exception, internal
path or secret must never reach a customer. Only ``AppError.safe_message`` is
displayed; everything technical goes to the structured log with the correlation
id so support can find it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.core.exceptions import AppError, RateLimitedError
from app.core.logging import get_logger
from app.domain.enums import Language
from app.i18n import t

log = get_logger(__name__)


class ErrorMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        lang: Language = data.get("lang", Language.EN)
        try:
            return await handler(event, data)

        except TelegramRetryAfter as exc:
            # Telegram's own flood control: never surfaced as an app error.
            log.warning("telegram.retry_after", seconds=exc.retry_after)
            await _notify(event, t("error.rate_limited", lang), alert=True)

        except TelegramBadRequest as exc:
            message = str(exc)
            if "message is not modified" in message:
                # Re-rendering an identical screen: acknowledge silently.
                if isinstance(event, CallbackQuery):
                    await event.answer()
                return None
            log.warning("telegram.bad_request", error=message[:300])
            await _notify(event, t("error.generic", lang), alert=True)

        except TelegramForbiddenError:
            # The user blocked the bot; nothing to show them.
            user = data.get("user")
            log.info("telegram.blocked_by_user", user_id=str(user.id) if user else None)
            if user is not None:
                user.is_bot_blocked = True

        except RateLimitedError as exc:
            await _notify(event, t("error.rate_limited", lang), alert=True)
            log.info("bot.rate_limited", retry_after=exc.retry_after)

        except AppError as exc:
            log.warning(
                "bot.app_error",
                code=exc.code,
                detail=exc.detail[:400],
                context=exc.context,
            )
            await _notify(event, f"⚠️ {exc.safe_message}", alert=True)

        except Exception:
            # Unexpected: log the full traceback internally, show nothing of it.
            log.exception("bot.unhandled_error", event_type=type(event).__name__)
            await _notify(event, t("error.generic", lang), alert=True)
        return None


async def _notify(event: TelegramObject, text: str, *, alert: bool = False) -> None:
    try:
        if isinstance(event, CallbackQuery):
            await event.answer(text[:200], show_alert=alert)
        elif isinstance(event, Message):
            await event.answer(text)
    except Exception:  # pragma: no cover - the channel itself is broken
        log.warning("bot.error_notify_failed")
