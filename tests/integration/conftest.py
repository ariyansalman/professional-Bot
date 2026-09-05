"""Shared bot-harness fixtures.

Telegram's HTTP session is replaced with a recorder, so no network call is
made, but everything else is genuine: the middleware chain, the database
session, the handlers, the keyboards and the rendered text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

from app.bot.handlers import build_router
from app.bot.middlewares import ContextMiddleware, ErrorMiddleware, MaintenanceMiddleware

TELEGRAM_ID = 777001


class RecordingSession:
    """Captures outgoing API calls instead of performing them.

    The ``timeout`` parameters mirror aiogram's session interface, which the Bot
    calls positionally; they are not a place to use ``asyncio.timeout``.
    """

    def __init__(self) -> None:
        self.calls: list[TelegramMethod] = []
        self._message_id = 1000

    async def __call__(
        self, bot: Bot, method: TelegramMethod, timeout: int | None = None  # noqa: ASYNC109
    ) -> Any:
        self.calls.append(method)
        return self._response_for(method)

    async def make_request(
        self, bot: Bot, method: TelegramMethod, timeout: int | None = None  # noqa: ASYNC109
    ) -> Any:
        return await self(bot, method, timeout)

    async def close(self) -> None:
        return None

    def _response_for(self, method: TelegramMethod) -> Any:
        name = type(method).__name__
        if name in {"SendMessage", "EditMessageText", "SendPhoto", "EditMessageCaption"}:
            self._message_id += 1
            return Message(
                message_id=self._message_id,
                date=datetime.now(UTC),
                chat=Chat(id=TELEGRAM_ID, type="private"),
                text=getattr(method, "text", None) or getattr(method, "caption", None) or "",
            )
        if name == "GetMe":
            return TgUser(id=1, is_bot=True, first_name="TestBot", username="testbot")
        return True

    @property
    def texts(self) -> list[str]:
        """Screen bodies only. Callback toasts are a different surface."""
        out = []
        for call in self.calls:
            if type(call).__name__ == "AnswerCallbackQuery":
                continue
            text = getattr(call, "text", None) or getattr(call, "caption", None)
            if text:
                out.append(text)
        return out

    @property
    def toasts(self) -> list[str]:
        return [
            call.text
            for call in self.calls
            if type(call).__name__ == "AnswerCallbackQuery" and getattr(call, "text", None)
        ]

    @property
    def buttons(self) -> list[str]:
        labels = []
        for call in self.calls:
            markup = getattr(call, "reply_markup", None)
            if markup and getattr(markup, "inline_keyboard", None):
                labels += [b.text for row in markup.inline_keyboard for b in row]
        return labels

    def clear(self) -> None:
        self.calls.clear()


#: The handler routers are module-level singletons and a Router attaches to a
#: single parent, so exactly one Dispatcher is built for the whole test module.
#: The database is still recreated per test: session_scope() resolves the
#: configured sessionmaker at call time, so a shared dispatcher is fine.
_DISPATCHER: Dispatcher | None = None


def _dispatcher() -> Dispatcher:
    global _DISPATCHER
    if _DISPATCHER is None:
        dispatcher = Dispatcher(storage=MemoryStorage())  # noqa: RUF100
        # Outer middleware, exactly as production: router-level filters such as
        # IsAdmin need the session and user to already be in the data dict.
        for observer in (dispatcher.message, dispatcher.callback_query):
            observer.outer_middleware(ErrorMiddleware())
            observer.outer_middleware(ContextMiddleware())
            observer.outer_middleware(MaintenanceMiddleware())
        dispatcher.include_router(build_router())
        _DISPATCHER = dispatcher
    return _DISPATCHER


@pytest_asyncio.fixture
async def bot_harness(sessionmaker_):
    """A dispatcher wired exactly as production, minus the network."""
    recorder = RecordingSession()
    bot = Bot(
        token="123456:test-token-for-handler-tests",
        session=recorder,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = _dispatcher()
    yield dispatcher, bot, recorder
    # Clear FSM state so one test's half-finished flow cannot leak into the next.
    await dispatcher.storage.close()
    await bot.session.close()


def message_update(text: str, update_id: int = 1) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=TELEGRAM_ID, type="private"),
            from_user=TgUser(
                id=TELEGRAM_ID, is_bot=False, first_name="Tester", username="tester"
            ),
            text=text,
        ),
    )


def photo_update(file_id: str = "AgACAgQAAxkBAAI-photo", update_id: int = 3) -> Update:
    """A photo message. Telegram sends the sizes smallest-first."""
    from aiogram.types import PhotoSize

    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=TELEGRAM_ID, type="private"),
            from_user=TgUser(
                id=TELEGRAM_ID, is_bot=False, first_name="Tester", username="tester"
            ),
            photo=[
                PhotoSize(
                    file_id=f"{file_id}-thumb",
                    file_unique_id="thumb",
                    width=90,
                    height=90,
                    file_size=1024,
                ),
                PhotoSize(
                    file_id=file_id,
                    file_unique_id="full",
                    width=1280,
                    height=1280,
                    file_size=91024,
                ),
            ],
        ),
    )


def callback_update(data: str, update_id: int = 2) -> Update:
    from aiogram.types import CallbackQuery

    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=TgUser(
                id=TELEGRAM_ID, is_bot=False, first_name="Tester", username="tester"
            ),
            chat_instance="test-instance",
            data=data,
            message=Message(
                message_id=500,
                date=datetime.now(UTC),
                chat=Chat(id=TELEGRAM_ID, type="private"),
                text="previous screen",
            ),
        ),
    )


async def feed(harness, update: Update) -> RecordingSession:
    dispatcher, bot, session = harness
    session.clear()
    await dispatcher.feed_update(bot, update)
    return session


