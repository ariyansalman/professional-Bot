"""Support screens and ticket flow (sections 43-44)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import Nav, PageCB, SupportCB, pack_uuid, unpack_uuid
from app.bot.keyboards.common import build, button, nav_button, pagination_row
from app.bot.keyboards.customer import support_menu_keyboard, ticket_keyboard
from app.bot.services.formatting import esc
from app.bot.services.screen import render
from app.bot.states import SupportFlow
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.timeutils import short_date
from app.db.models.user import User
from app.db.repositories.support import SupportRepository
from app.domain.enums import Language, TicketCategory, TicketStatus
from app.domain.support.service import SupportService
from app.i18n import t

log = get_logger(__name__)
router = Router(name="support")

TICKET_STATUS_LABEL = {
    TicketStatus.OPEN: "🟢 Open",
    TicketStatus.ASSIGNED: "🔵 In progress",
    TicketStatus.WAITING_USER: "🟡 Awaiting your reply",
    TicketStatus.RESOLVED: "✅ Resolved",
    TicketStatus.CLOSED: "⚫ Closed",
}


@router.message(Command("support"))
async def support_command(message: Message, lang: Language, state: FSMContext) -> None:
    await state.clear()
    await _menu(message, lang)


@router.callback_query(Nav.filter(F.to == "support"))
async def support_nav(callback: CallbackQuery, lang: Language, state: FSMContext) -> None:
    await state.clear()
    await _menu(callback, lang)


@router.callback_query(SupportCB.filter(F.action == "menu"))
async def support_menu(callback: CallbackQuery, lang: Language, state: FSMContext) -> None:
    await state.clear()
    await _menu(callback, lang)


async def _menu(event, lang: Language) -> None:
    settings = get_settings()
    if not settings.features.support_enabled:
        contact = settings.telegram.support_username
        text = (
            f"🎧 <b>SUPPORT</b>\n\nPlease contact @{esc(contact)}."
            if contact
            else t("error.not_found", lang)
        )
        await render(event, text, build([[nav_button(t("btn.home", lang), "home")]]))
        return
    text = "\n".join([t("support.title", lang), "", t("support.how_can_we_help", lang)])
    await render(event, text, support_menu_keyboard(lang))


@router.callback_query(SupportCB.filter(F.action == "category"))
async def choose_category(
    callback: CallbackQuery, callback_data: SupportCB, lang: Language, state: FSMContext
) -> None:
    try:
        category = TicketCategory(callback_data.arg)
    except ValueError:
        category = TicketCategory.OTHER

    await state.set_state(SupportFlow.describing_issue)
    await state.update_data(support_category=category.value)
    text = "\n".join(
        [
            t("support.title", lang),
            "",
            f"Category: <b>{category.value.title()}</b>",
            "",
            t("support.describe", lang),
        ]
    )
    await render(
        callback,
        text,
        build([[button(t("btn.back", lang), SupportCB(action="menu").pack())]]),
    )


@router.message(SupportFlow.describing_issue, F.text)
async def create_ticket(
    message: Message,
    session: AsyncSession,
    user: User,
    lang: Language,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    await state.clear()
    try:
        category = TicketCategory(data.get("support_category", "other"))
    except ValueError:
        category = TicketCategory.OTHER

    body = (message.text or "").strip()[:4000]
    if len(body) < 5:
        await render(
            message,
            "⚠️ Please describe your issue in a little more detail.",
            build([[button(t("btn.try_again", lang), SupportCB(action="category", arg=category.value).pack())]]),
        )
        return

    ticket = await SupportService(session).create_ticket(
        user=user, category=category, body=body
    )
    text = "\n".join(
        [
            t("support.created", lang),
            "",
            f"{t('support.ticket', lang)}: <b>{esc(ticket.reference)}</b>",
            f"Category: {category.value.title()}",
            f"Status: {TICKET_STATUS_LABEL[ticket.status]}",
            "",
            "Our team will reply here as soon as possible.",
        ]
    )
    await render(
        message,
        text,
        build(
            [
                [button(t("btn.my_tickets", lang), SupportCB(action="tickets").pack())],
                [nav_button(t("btn.home", lang), "home")],
            ]
        ),
    )


@router.callback_query(SupportCB.filter(F.action == "tickets"))
async def my_tickets(
    callback: CallbackQuery,
    callback_data: SupportCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    await _tickets(callback, session, user, lang, callback_data.page)


@router.callback_query(PageCB.filter(F.scope == "tickets"))
async def tickets_page(
    callback: CallbackQuery,
    callback_data: PageCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    await _tickets(callback, session, user, lang, callback_data.page)


async def _tickets(event, session: AsyncSession, user: User, lang: Language, page: int) -> None:
    result = await SupportRepository(session).list_for_user(user.id, page=page)
    if result.is_empty:
        await render(
            event,
            t("support.empty", lang),
            build(
                [
                    [button(t("support.title", lang), SupportCB(action="menu").pack())],
                    [nav_button(t("btn.home", lang), "home")],
                ]
            ),
        )
        return

    rows = [
        [
            button(
                f"{ticket.reference} · {TICKET_STATUS_LABEL[ticket.status]}",
                SupportCB(action="view", arg=pack_uuid(ticket.id)).pack(),
            )
        ]
        for ticket in result.items
    ]
    rows.append(pagination_row(result, "tickets"))
    rows.append(
        [
            button(t("btn.back", lang), SupportCB(action="menu").pack()),
            nav_button(t("btn.home", lang), "home"),
        ]
    )
    await render(event, "🎫 <b>MY TICKETS</b>", build(rows))


@router.callback_query(SupportCB.filter(F.action == "view"))
async def view_ticket(
    callback: CallbackQuery,
    callback_data: SupportCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    ticket = await SupportRepository(session).get_with_messages(unpack_uuid(callback_data.arg))
    if ticket is None or ticket.user_id != user.id:
        await render(callback, t("error.not_found", lang), build([[nav_button(t("btn.home", lang), "home")]]))
        return

    lines = [
        f"🎫 <b>{esc(ticket.reference)}</b>",
        "",
        f"Category: {ticket.category.value.title()}",
        f"Status: {TICKET_STATUS_LABEL[ticket.status]}",
        f"Opened: {short_date(ticket.created_at)}",
        "",
    ]
    # Internal staff notes are never shown to the customer.
    for msg in [m for m in ticket.messages if not m.is_internal][-10:]:
        author = "🎧 Support" if msg.is_staff else "👤 You"
        lines.append(f"<b>{author}</b> · {short_date(msg.created_at)}")
        lines.append(esc(msg.body))
        lines.append("")

    await render(
        callback,
        "\n".join(lines),
        ticket_keyboard(lang, ticket.id, can_reply=ticket.status is not TicketStatus.CLOSED),
    )


@router.callback_query(SupportCB.filter(F.action == "reply"))
async def reply_prompt(
    callback: CallbackQuery, callback_data: SupportCB, lang: Language, state: FSMContext
) -> None:
    await state.set_state(SupportFlow.replying)
    await state.update_data(support_ticket_id=callback_data.arg)
    await render(
        callback,
        t("support.reply_prompt", lang),
        build([[button(t("btn.back", lang), SupportCB(action="view", arg=callback_data.arg).pack())]]),
    )


@router.message(SupportFlow.replying, F.text)
async def submit_reply(
    message: Message,
    session: AsyncSession,
    user: User,
    lang: Language,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    await state.clear()
    ticket_ref = data.get("support_ticket_id")
    if not ticket_ref:
        await render(message, t("error.expired_session", lang), build([[nav_button(t("btn.home", lang), "home")]]))
        return

    ticket = await SupportRepository(session).get_with_messages(unpack_uuid(ticket_ref))
    if ticket is None or ticket.user_id != user.id:
        await render(message, t("error.not_found", lang), build([[nav_button(t("btn.home", lang), "home")]]))
        return

    try:
        await SupportService(session).customer_reply(
            ticket=ticket, user=user, body=(message.text or "").strip()[:4000]
        )
    except AppError as exc:
        await render(message, f"⚠️ {exc.safe_message}", build([[nav_button(t("btn.home", lang), "home")]]))
        return

    await render(
        message,
        f"✅ Reply sent on ticket <b>{esc(ticket.reference)}</b>.",
        build(
            [
                [button("🎫 View ticket", SupportCB(action="view", arg=ticket_ref).pack())],
                [nav_button(t("btn.home", lang), "home")],
            ]
        ),
    )
