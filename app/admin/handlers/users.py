"""Admin user management and support queue (63, 70)."""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.filters import IsAdmin
from app.admin.handlers.confirmations import register, target_uuid
from app.admin.keyboards.panels import adm, admin_back_row, confirm_keyboard
from app.admin.permissions.rbac import Permissions
from app.admin.services.context import AdminContext, audit, create_confirmation
from app.bot.callbacks import AdminCB, PageCB
from app.bot.keyboards.common import build, button
from app.bot.services.formatting import DIVIDER, esc, money
from app.bot.services.screen import render
from app.bot.states import AdminFlow
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.timeutils import humanize_datetime, short_date
from app.db.repositories.orders import OrderRepository
from app.db.repositories.support import SupportRepository
from app.db.repositories.users import ReferralRepository, UserRepository
from app.domain.enums import AuditAction, TicketStatus, UserStatus
from app.domain.support.service import SupportService

log = get_logger(__name__)
router = Router(name="admin_users")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


@router.callback_query(AdminCB.filter(F.section == "users"))
async def users_section(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, admin: AdminContext
) -> None:
    admin.require(Permissions.USERS_VIEW)
    action = callback_data.action
    if action == "view":
        await _user_detail(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "restrict":
        await _request_status_change(callback, session, admin, uuid.UUID(callback_data.arg), UserStatus.RESTRICTED)
    elif action == "ban":
        await _request_status_change(callback, session, admin, uuid.UUID(callback_data.arg), UserStatus.BANNED)
    elif action == "unban":
        await _set_status(callback, session, admin, uuid.UUID(callback_data.arg), UserStatus.ACTIVE, "reinstated")
    else:
        await _user_list(callback, session, admin, callback_data.page)


@router.callback_query(PageCB.filter(F.scope == "adm_users"))
async def users_page(
    callback: CallbackQuery, callback_data: PageCB, session: AsyncSession, admin: AdminContext
) -> None:
    admin.require(Permissions.USERS_VIEW)
    await _user_list(callback, session, admin, callback_data.page)


async def _user_list(event, session: AsyncSession, admin: AdminContext, page: int) -> None:
    result = await UserRepository(session).search("", page=page, per_page=8)
    lines = ["👥 <b>USERS</b>", "", f"{result.total} user(s)", DIVIDER]
    rows = []
    for user in result.items:
        icon = {"active": "🟢", "restricted": "🟡", "banned": "🔴"}[user.status.value]
        lines.append(
            f"{icon} {esc(user.display_name)} · {user.orders_count} orders · "
            f"{money(user.total_spent or 0, 'USDT')}"
        )
        rows.append([button(f"👤 {user.display_name[:22]}", adm("users", "view", user.id.hex))])
    if result.pages > 1:
        nav = []
        if result.has_prev:
            nav.append(button("◀", PageCB(scope="adm_users", page=result.page - 1).pack()))
        nav.append(button(result.label, adm("users", "noop")))
        if result.has_next:
            nav.append(button("▶", PageCB(scope="adm_users", page=result.page + 1).pack()))
        rows.append(nav)
    rows.append([button("🔎 Search", adm("search"))])
    rows.append(admin_back_row())
    await render(event, "\n".join(lines), build(rows))


async def _user_detail(
    event, session: AsyncSession, admin: AdminContext, user_id: uuid.UUID
) -> None:
    users = UserRepository(session)
    user = await users.get(user_id)
    if user is None:
        await render(event, "⚠️ User not found.", build([admin_back_row("users")]))
        return

    orders = await OrderRepository(session).list_for_user(user.id, per_page=5)
    referrals = await ReferralRepository(session).stats(user.id)

    lines = [
        f"👤 <b>{esc(user.display_name)}</b>",
        "",
        f"Telegram ID: <code>{user.telegram_id}</code>",
        f"Status: <b>{user.status.value}</b>",
        f"Language: {user.language.value}",
        f"Joined: {short_date(user.created_at)}",
        f"Last seen: {humanize_datetime(user.last_seen_at)}",
        f"Bot blocked: {'yes' if user.is_bot_blocked else 'no'}",
        "",
        DIVIDER,
        "<b>COMMERCE</b>",
        f"Orders: {user.orders_count} ({user.completed_orders_count} completed)",
        f"Total spent: {money(user.total_spent or 0, 'USDT')}",
        f"Referrals: {referrals['invited']} invited · {referrals['qualified']} qualified",
        f"Referral balance: {money(user.referral_balance or 0, 'USDT')}",
        f"Referral code: <code>{esc(user.referral_code)}</code>",
    ]
    if user.roles:
        lines += ["", f"Roles: {', '.join(role.name.value for role in user.roles)}"]
    if user.risk_flags:
        lines += ["", f"⚠️ Risk flags: {esc(str(user.risk_flags)[:120])}"]
    if user.internal_notes:
        lines += ["", f"📝 {esc(user.internal_notes[:200])}"]

    if not orders.is_empty:
        lines += ["", DIVIDER, "<b>RECENT ORDERS</b>"]
        for order in orders.items:
            lines.append(
                f"• #{esc(order.reference)} · {order.status.value} · {money(order.total, order.currency)}"
            )

    rows = []
    if admin.can(Permissions.USERS_MANAGE):
        if user.status is UserStatus.ACTIVE:
            rows.append([button("🟡 Restrict", adm("users", "restrict", user.id.hex))])
        else:
            rows.append([button("🟢 Reinstate", adm("users", "unban", user.id.hex))])
    if admin.can(Permissions.USERS_BAN) and user.status is not UserStatus.BANNED:
        rows.append([button("🔴 Ban", adm("users", "ban", user.id.hex))])
    rows.append(admin_back_row("users"))
    await render(event, "\n".join(lines), build(rows))


async def _request_status_change(
    event, session: AsyncSession, admin: AdminContext, user_id: uuid.UUID, status: UserStatus
) -> None:
    """Restricting or banning a customer is confirmed and audited."""
    permission = Permissions.USERS_BAN if status is UserStatus.BANNED else Permissions.USERS_MANAGE
    admin.require(permission)

    user = await UserRepository(session).get(user_id)
    if user is None:
        await render(event, "⚠️ User not found.", build([admin_back_row("users")]))
        return

    token = await create_confirmation(
        actor_id=admin.user.id,
        action="user_status",
        payload={"target": str(user_id), "status": status.value, "reason": f"set by {admin.label}"},
    )
    verb = "BAN" if status is UserStatus.BANNED else "RESTRICT"
    await render(
        event,
        "\n".join(
            [
                f"⚠️ <b>{verb} USER</b>",
                "",
                f"<b>{esc(user.display_name)}</b> ({user.telegram_id})",
                "",
                "They will lose access to the bot immediately.",
                "Existing orders and payment history are preserved.",
            ]
        ),
        confirm_keyboard(token, yes=f"✅ {verb.title()}"),
    )


@register("user_status")
async def confirmed_status_change(
    callback: CallbackQuery, session: AsyncSession, admin: AdminContext, payload: dict
) -> None:
    status = UserStatus(payload.get("status", "restricted"))
    permission = Permissions.USERS_BAN if status is UserStatus.BANNED else Permissions.USERS_MANAGE
    admin.require(permission)
    await _set_status(
        callback, session, admin, target_uuid(payload), status, payload.get("reason", "")
    )


async def _set_status(
    event,
    session: AsyncSession,
    admin: AdminContext,
    user_id: uuid.UUID,
    status: UserStatus,
    reason: str,
) -> None:
    users = UserRepository(session)
    user = await users.get(user_id)
    if user is None:
        await render(event, "⚠️ User not found.", build([admin_back_row("users")]))
        return
    if user.id == admin.user.id:
        await render(
            event,
            "⚠️ You cannot change your own account status.",
            build([admin_back_row("users")]),
        )
        return

    await users.set_status(user, status)
    await audit(
        session,
        admin,
        AuditAction.USER_BANNED if status is UserStatus.BANNED else AuditAction.USER_RESTRICTED,
        target_type="user",
        target_id=user_id,
        reason=reason,
        details={"status": status.value, "telegram_id": user.telegram_id},
    )
    await _user_detail(event, session, admin, user_id)


# -- support ---------------------------------------------------------------


@router.callback_query(AdminCB.filter(F.section == "support"))
async def support_section(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: AdminContext,
    state: FSMContext,
) -> None:
    admin.require(Permissions.SUPPORT_VIEW)
    action = callback_data.action
    if action == "view":
        await _ticket_detail(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "assign":
        admin.require(Permissions.SUPPORT_ASSIGN)
        ticket = await SupportRepository(session).get_with_messages(uuid.UUID(callback_data.arg))
        if ticket is not None:
            await SupportService(session).assign(ticket=ticket, staff=admin.user)
        await _ticket_detail(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "reply":
        admin.require(Permissions.SUPPORT_REPLY)
        await state.set_state(AdminFlow.support_reply)
        await state.update_data(support_ticket=callback_data.arg)
        await render(
            callback,
            "💬 <b>REPLY</b>\n\nSend your reply to the customer.",
            build([[button("❌ Cancel", adm("support", "view", callback_data.arg))]]),
        )
    elif action == "resolve":
        admin.require(Permissions.SUPPORT_REPLY)
        ticket = await SupportRepository(session).get_with_messages(uuid.UUID(callback_data.arg))
        if ticket is not None:
            await SupportService(session).resolve(ticket=ticket, staff=admin.user)
        await _ticket_detail(callback, session, admin, uuid.UUID(callback_data.arg))
    else:
        await _ticket_list(callback, session, admin, callback_data.page)


async def _ticket_list(event, session: AsyncSession, admin: AdminContext, page: int) -> None:
    result = await SupportRepository(session).list_open(page=page, per_page=8)
    lines = ["🎧 <b>SUPPORT</b>", "", f"{result.total} open ticket(s)", DIVIDER]
    if result.is_empty:
        lines += ["", "✅ No open tickets."]

    rows = []
    for ticket in result.items:
        priority = {"urgent": "🔴", "high": "🟠", "normal": "🟡", "low": "⚪"}[ticket.priority.value]
        lines.append(
            f"{priority} <code>{esc(ticket.reference)}</code> · {ticket.category.value} · {ticket.status.value}"
        )
        rows.append([button(f"🎫 {ticket.reference}", adm("support", "view", ticket.id.hex))])
    rows.append(admin_back_row())
    await render(event, "\n".join(lines), build(rows))


async def _ticket_detail(
    event, session: AsyncSession, admin: AdminContext, ticket_id: uuid.UUID
) -> None:
    tickets = SupportRepository(session)
    ticket = await tickets.get_with_messages(ticket_id)
    if ticket is None:
        await render(event, "⚠️ Ticket not found.", build([admin_back_row("support")]))
        return

    customer = await UserRepository(session).get(ticket.user_id)
    lines = [
        f"🎫 <b>{esc(ticket.reference)}</b>",
        "",
        f"Customer: {esc(customer.display_name) if customer else 'unknown'}",
        f"Category: {ticket.category.value} · Priority: {ticket.priority.value}",
        f"Status: <b>{ticket.status.value}</b>",
        f"Opened: {humanize_datetime(ticket.created_at)}",
        "",
        DIVIDER,
    ]
    for message in ticket.messages[-8:]:
        if message.is_internal:
            author = "📝 Internal"
        elif message.is_staff:
            author = "🎧 Staff"
        else:
            author = "👤 Customer"
        lines += [
            f"<b>{author}</b> · {short_date(message.created_at)}",
            esc(message.body[:600]),
            "",
        ]

    rows = []
    if admin.can(Permissions.SUPPORT_REPLY) and ticket.status is not TicketStatus.CLOSED:
        rows.append([button("💬 Reply", adm("support", "reply", ticket.id.hex))])
    if admin.can(Permissions.SUPPORT_ASSIGN) and ticket.assigned_to_id != admin.user.id:
        rows.append([button("🙋 Assign to me", adm("support", "assign", ticket.id.hex))])
    if admin.can(Permissions.SUPPORT_REPLY) and ticket.status is not TicketStatus.RESOLVED:
        rows.append([button("✅ Resolve", adm("support", "resolve", ticket.id.hex))])
    if ticket.order_id and admin.can(Permissions.ORDERS_VIEW):
        rows.append([button("📦 Order", adm("orders", "view", ticket.order_id.hex))])
    if customer is not None and admin.can(Permissions.USERS_VIEW):
        rows.append([button("👤 Customer", adm("users", "view", customer.id.hex))])
    rows.append(admin_back_row("support"))
    await render(event, "\n".join(lines), build(rows))


@router.message(AdminFlow.support_reply, F.text)
async def support_reply(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    admin.require(Permissions.SUPPORT_REPLY)
    data = await state.get_data()
    await state.clear()
    ticket_ref = data.get("support_ticket")
    if not ticket_ref:
        await render(message, "⚠️ That reply expired.", build([admin_back_row("support")]))
        return

    tickets = SupportRepository(session)
    ticket = await tickets.get_with_messages(uuid.UUID(ticket_ref))
    if ticket is None:
        await render(message, "⚠️ Ticket not found.", build([admin_back_row("support")]))
        return

    body = (message.text or "").strip()[:4000]
    try:
        await SupportService(session).staff_reply(ticket=ticket, staff=admin.user, body=body)
    except AppError as exc:
        await render(message, f"⚠️ {exc.safe_message}", build([admin_back_row("support")]))
        return

    # Push the reply to the customer's chat.
    customer = await UserRepository(session).get(ticket.user_id)
    if customer is not None and not customer.is_bot_blocked:
        try:
            await message.bot.send_message(
                customer.telegram_id,
                "\n".join(
                    [
                        f"🎧 <b>Support reply — {esc(ticket.reference)}</b>",
                        "",
                        esc(body),
                    ]
                ),
            )
        except Exception:
            log.info("admin.support_push_failed", ticket=ticket.reference)

    await render(
        message,
        f"✅ Reply sent on <b>{esc(ticket.reference)}</b>.",
        build([[button("🎫 View ticket", adm("support", "view", ticket_ref))], admin_back_row("support")]),
    )
