"""Admin dashboard, analytics, audit and global search (56, 72-74)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.filters import IsAdmin
from app.admin.keyboards.panels import adm, admin_back_row, dashboard_keyboard
from app.admin.permissions.rbac import Permissions
from app.admin.services.analytics import AnalyticsService
from app.admin.services.context import AdminContext
from app.bot.callbacks import AdminCB
from app.bot.keyboards.common import build, button
from app.bot.services.formatting import DIVIDER, esc, money
from app.bot.services.screen import render
from app.bot.states import AdminFlow
from app.core.logging import get_logger
from app.core.money import format_amount
from app.core.timeutils import humanize_datetime
from app.db.repositories.orders import OrderRepository
from app.db.repositories.payments import PaymentIntentRepository
from app.db.repositories.resellers import ApiKeyRepository
from app.db.repositories.support import AuditRepository, SupportRepository
from app.db.repositories.users import UserRepository

log = get_logger(__name__)
router = Router(name="admin_dashboard")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


@router.message(Command("admin"))
async def admin_command(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    await state.clear()
    await _dashboard(message, session, admin)


@router.callback_query(AdminCB.filter(F.section == "dashboard"))
async def dashboard_callback(
    callback: CallbackQuery, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    await state.clear()
    await _dashboard(callback, session, admin)


async def _dashboard(event, session: AsyncSession, admin: AdminContext) -> None:
    admin.require(Permissions.DASHBOARD_VIEW)
    snapshot = await AnalyticsService(session).dashboard()

    lines = [
        "🛡 <b>ADMIN DASHBOARD</b>",
        "",
        f"💰 Today: <b>{format_amount(snapshot.revenue_today)} USDT</b>",
        f"📦 Orders: <b>{snapshot.orders_today}</b>",
        f"👥 New users: <b>{snapshot.new_users_today}</b>",
        "",
        DIVIDER,
    ]

    # Actionable items first (section 56).
    attention: list[str] = []
    if snapshot.pending_payments:
        attention.append(f"⏳ Pending payments: <b>{snapshot.pending_payments}</b>")
    if snapshot.manual_review:
        attention.append(f"⚠️ Manual review: <b>{snapshot.manual_review}</b>")
    if snapshot.open_reconciliation:
        attention.append(f"🧮 Reconciliation: <b>{snapshot.open_reconciliation}</b>")
    if snapshot.failed_deliveries:
        attention.append(f"❌ Failed delivery: <b>{snapshot.failed_deliveries}</b>")
    if snapshot.low_stock:
        attention.append(f"📦 Low stock: <b>{len(snapshot.low_stock)}</b>")
    if snapshot.open_tickets:
        attention.append(f"🎧 Open tickets: <b>{snapshot.open_tickets}</b>")

    if attention:
        lines += ["", "<b>NEEDS ATTENTION</b>", *attention]
    else:
        lines += ["", "✅ Nothing needs attention."]

    health_icon = "❤️" if snapshot.providers_healthy == snapshot.providers_total else "💔"
    lines += [
        "",
        f"{health_icon} Provider health: <b>{snapshot.providers_healthy}/{snapshot.providers_total}</b>",
    ]

    if snapshot.low_stock:
        lines += ["", "<b>LOW STOCK</b>"]
        for product, count in snapshot.low_stock[:5]:
            lines.append(f"• {esc(product.name)} — {count} left")

    await render(event, "\n".join(lines), dashboard_keyboard(admin))


@router.callback_query(AdminCB.filter(F.section == "analytics"))
async def analytics(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, admin: AdminContext
) -> None:
    admin.require(Permissions.ANALYTICS_VIEW)
    service = AnalyticsService(session)

    view = callback_data.arg or "overview"
    if view == "revenue":
        series = await service.revenue_series(days=7)
        lines = ["📈 <b>REVENUE — LAST 7 DAYS</b>", "", DIVIDER]
        peak = max((amount for _, amount in series), default=0) or 1
        for label, amount in series:
            bars = int((amount / peak) * 12) if peak else 0
            lines.append(f"{label} {'█' * bars or '·'} {format_amount(amount)}")
        total = sum((amount for _, amount in series), start=type(peak)(0))
        lines += ["", f"Total: <b>{format_amount(total)} USDT</b>"]
    elif view == "payments":
        breakdown = await service.payment_method_breakdown()
        lines = ["💳 <b>PAYMENT METHODS — 30 DAYS</b>", "", DIVIDER]
        if not breakdown:
            lines.append("No verified payments in this period.")
        for provider, count, total in breakdown:
            lines.append(f"• {provider}: {count} payments · {format_amount(total)}")
    elif view == "verification":
        stats = await service.verification_stats()
        lines = [
            "🔍 <b>VERIFICATION — 7 DAYS</b>",
            "",
            f"Attempts: <b>{stats['total']}</b>",
            f"Verified: <b>{stats['verified']}</b>",
            f"Success rate: <b>{stats['success_rate']}%</b>",
            f"Avg latency: <b>{stats['avg_latency_ms']} ms</b>",
            "",
            DIVIDER,
        ]
        for outcome, count in sorted(
            stats["outcomes"].items(), key=lambda item: -item[1]
        ):
            lines.append(f"• {outcome}: {count}")
    else:
        conversion = await service.conversion()
        lines = [
            "📈 <b>ANALYTICS</b>",
            "",
            "<b>Conversion — 30 days</b>",
            f"Orders created: <b>{conversion['orders']}</b>",
            f"Paid: <b>{conversion['paid']}</b> ({conversion['paid_rate']}%)",
            f"Completed: <b>{conversion['completed']}</b> ({conversion['completion_rate']}%)",
        ]

    rows = [
        [
            button("📈 Revenue", adm("analytics", arg="revenue")),
            button("💳 Payments", adm("analytics", arg="payments")),
        ],
        [
            button("🔍 Verification", adm("analytics", arg="verification")),
            button("📊 Overview", adm("analytics", arg="overview")),
        ],
        admin_back_row(),
    ]
    await render(callback, "\n".join(lines), build(rows))


@router.callback_query(AdminCB.filter(F.section == "audit"))
async def audit_log(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, admin: AdminContext
) -> None:
    admin.require(Permissions.AUDIT_VIEW)
    page = await AuditRepository(session).list_recent(page=callback_data.page, per_page=8)

    lines = ["🧾 <b>AUDIT LOG</b>", ""]
    if page.is_empty:
        lines.append("No audit entries.")
    for entry in page.items:
        lines += [
            f"<b>{esc(entry.action.value)}</b>",
            f"by {esc(entry.actor_label or 'system')} · {humanize_datetime(entry.created_at)}",
        ]
        if entry.target_type:
            lines.append(f"target: {esc(entry.target_type)} {esc(entry.target_id or '')}")
        if entry.reason:
            lines.append(f"reason: {esc(entry.reason[:120])}")
        lines.append("")

    rows = []
    if page.pages > 1:
        nav = []
        if page.has_prev:
            nav.append(button("◀", adm("audit", page=page.page - 1)))
        nav.append(button(page.label, adm("audit", action="noop")))
        if page.has_next:
            nav.append(button("▶", adm("audit", page=page.page + 1)))
        rows.append(nav)
    rows.append(admin_back_row())
    await render(callback, "\n".join(lines), build(rows))


@router.callback_query(AdminCB.filter(F.section == "search"))
async def search_prompt(
    callback: CallbackQuery, admin: AdminContext, state: FSMContext
) -> None:
    admin.require(Permissions.DASHBOARD_VIEW)
    await state.set_state(AdminFlow.searching)
    lines = [
        "🔎 <b>GLOBAL SEARCH</b>",
        "",
        "Send any of:",
        "• order reference (TG-10284)",
        "• Telegram username or ID",
        "• transaction ID / TXID",
        "• payment reference",
        "• support ticket reference",
    ]
    await render(callback, "\n".join(lines), build([admin_back_row()]))


@router.message(AdminFlow.searching, F.text)
async def search_results(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    """Global search. Every result set is filtered by the operator's rights."""
    await state.clear()
    query = (message.text or "").strip()[:120]
    if not query:
        await _dashboard(message, session, admin)
        return

    lines = [f"🔎 <b>SEARCH: {esc(query)}</b>", ""]
    rows = []
    found = False

    if admin.can(Permissions.ORDERS_VIEW):
        order = await OrderRepository(session).get_by_reference(query)
        if order is not None:
            found = True
            lines += [
                "<b>Order</b>",
                f"#{esc(order.reference)} · {money(order.total, order.currency)} · {order.status.value}",
                "",
            ]
            rows.append([button(f"📦 Open {order.reference}", adm("orders", "view", order.id.hex))])

    if admin.can(Permissions.PAYMENTS_VIEW):
        intents = await PaymentIntentRepository(session).search(query)
        if intents:
            found = True
            lines += ["<b>Payments</b>"]
            for intent in intents[:5]:
                lines.append(
                    f"#{esc(intent.reference)} · {intent.status.value} · "
                    f"{money(intent.expected_amount, intent.asset)}"
                )
                rows.append(
                    [button(f"💳 {intent.reference}", adm("payments", "view", intent.id.hex))]
                )
            lines.append("")

    if admin.can(Permissions.USERS_VIEW):
        users = await UserRepository(session).search(query, per_page=5)
        if not users.is_empty:
            found = True
            lines += ["<b>Users</b>"]
            for user in users.items:
                lines.append(f"{esc(user.display_name)} · {user.telegram_id}")
                rows.append([button(f"👤 {user.display_name[:20]}", adm("users", "view", user.id.hex))])
            lines.append("")

    if admin.can(Permissions.SUPPORT_VIEW):
        tickets = await SupportRepository(session).search(query, limit=5)
        if tickets:
            found = True
            lines += ["<b>Tickets</b>"]
            for ticket in tickets:
                lines.append(f"{esc(ticket.reference)} · {ticket.status.value}")
                rows.append([button(f"🎧 {ticket.reference}", adm("support", "view", ticket.id.hex))])
            lines.append("")

    if admin.can(Permissions.RESELLERS_KEYS):
        # Only the non-secret public id is searchable: the key itself is never
        # stored, so it cannot be looked up even by an administrator.
        candidate = query.split("_")[1] if query.count("_") >= 2 else query
        key = await ApiKeyRepository(session).find_by_public_id(candidate)
        if key is not None:
            found = True
            lines += [
                "<b>API key</b>",
                f"{key.prefix}_{key.public_id} · {esc(key.name)}"
                f" · {'revoked' if key.is_revoked else 'active'}",
                "",
            ]
            rows.append(
                [button("🔗 Open reseller", adm("resellers", "view", key.reseller_id.hex))]
            )

    if not found:
        lines.append("No results.")

    rows.append(admin_back_row())
    await render(message, "\n".join(lines), build(rows))
