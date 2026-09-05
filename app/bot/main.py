"""Telegram bot entrypoint (polling or webhook)."""

from __future__ import annotations

import asyncio
import contextlib

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage

from app.bot.handlers import build_router
from app.bot.middlewares import (
    ContextMiddleware,
    ErrorMiddleware,
    MaintenanceMiddleware,
    ThrottlingMiddleware,
)
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import close_redis, get_redis
from app.db.session import dispose_engine

log = get_logger(__name__)


def build_bot() -> Bot:
    settings = get_settings()
    token = settings.telegram.bot_token.get_secret_value()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    return Bot(
        token=token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML, link_preview_is_disabled=True
        ),
    )


def build_dispatcher() -> Dispatcher:
    """Assemble the dispatcher with FSM storage and the middleware chain.

    The chain is registered as **outer** middleware. This is not cosmetic: inner
    middleware runs after a router's filters, so the admin router's ``IsAdmin``
    filter would be evaluated before the session and user existed in the data
    dict and would deny every request. Outer middleware runs before root filters,
    which is what those filters need.

    Order is deliberate: errors wrap everything (including a failure inside the
    context middleware), the session exists before maintenance is checked, and
    throttling runs last, where the user's language is already known.
    """
    try:
        storage = RedisStorage(redis=get_redis())
    except Exception as exc:
        log.warning("bot.redis_storage_unavailable", error=str(exc)[:200])
        storage = MemoryStorage()

    dispatcher = Dispatcher(storage=storage)

    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.outer_middleware(ErrorMiddleware())
        observer.outer_middleware(ContextMiddleware())
        observer.outer_middleware(MaintenanceMiddleware())
        observer.outer_middleware(ThrottlingMiddleware())

    dispatcher.include_router(build_router())
    return dispatcher


async def run_polling() -> None:
    settings = get_settings()
    configure_logging(settings.observability.level, settings.observability.json_output)
    bot = build_bot()
    dispatcher = build_dispatcher()

    me = await bot.get_me()
    log.info("bot.starting", mode="polling", username=me.username, service="bot")

    # Drop any webhook so polling is not rejected by Telegram.
    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()
        await dispose_engine()
        await close_redis()
        log.info("bot.stopped")


async def run_webhook() -> None:
    """Run the bot behind a webhook, served by aiohttp."""
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    from aiohttp import web

    settings = get_settings()
    configure_logging(settings.observability.level, settings.observability.json_output)
    url = settings.telegram.webhook_url
    if not url:
        raise RuntimeError("TELEGRAM_WEBHOOK_BASE_URL is required for webhook mode")

    bot = build_bot()
    dispatcher = build_dispatcher()
    secret = settings.telegram.webhook_secret.get_secret_value() or None

    await bot.set_webhook(
        url=url,
        secret_token=secret,
        allowed_updates=dispatcher.resolve_used_update_types(),
        drop_pending_updates=False,
    )
    log.info("bot.starting", mode="webhook", path=settings.telegram.webhook_path)

    application = web.Application()
    SimpleRequestHandler(dispatcher=dispatcher, bot=bot, secret_token=secret).register(
        application, path=settings.telegram.webhook_path
    )
    setup_application(application, dispatcher, bot=bot)

    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.api.host, port=settings.api.port)
    await site.start()

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await bot.session.close()
        await dispose_engine()
        await close_redis()


def main() -> None:
    settings = get_settings()
    runner = run_webhook if settings.telegram.webhook_base_url else run_polling
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(runner())


if __name__ == "__main__":
    main()
