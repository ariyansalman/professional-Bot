"""Admin reseller management (section 65)."""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.types import CallbackQuery
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
from app.core.logging import get_logger
from app.core.timeutils import humanize_datetime, short_date
from app.db.repositories.orders import OrderRepository
from app.db.repositories.resellers import (
    ApiKeyRepository,
    ResellerRepository,
    WebhookDeliveryRepository,
    WebhookRepository,
)
from app.domain.enums import AuditAction, ResellerStatus
from app.domain.resellers.service import ResellerService

log = get_logger(__name__)
router = Router(name="admin_resellers")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


@router.callback_query(AdminCB.filter(F.section == "resellers"))
async def resellers_section(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, admin: AdminContext
) -> None:
    admin.require(Permissions.RESELLERS_VIEW)
    action = callback_data.action
    if action == "view":
        await _detail(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "approve":
        await _approve(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "suspend":
        await _request_suspend(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "reinstate":
        await _reinstate(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "keys":
        await _keys(callback, session, admin, uuid.UUID(callback_data.arg))
    else:
        await _list(callback, session, admin, callback_data.arg, callback_data.page)


@router.callback_query(PageCB.filter(F.scope == "adm:resellers"))
async def resellers_page(
    callback: CallbackQuery, callback_data: PageCB, session: AsyncSession, admin: AdminContext
) -> None:
    admin.require(Permissions.RESELLERS_VIEW)
    await _list(callback, session, admin, callback_data.arg, callback_data.page)


async def _list(
    event, session: AsyncSession, admin: AdminContext, filter_key: str, page: int
) -> None:
    status = None
    if filter_key in {s.value for s in ResellerStatus}:
        status = ResellerStatus(filter_key)
    result = await ResellerRepository(session).list_all(status=status, page=page, per_page=8)

    lines = ["🔗 <b>RESELLERS</b>", "", f"{result.total} account(s)", DIVIDER]
    rows = []
    chips = [
        button(f"• {label} •" if key == filter_key else label, adm("resellers", arg=key))
        for key, label in [
            ("", "All"),
            ("pending", "Pending"),
            ("active", "Active"),
            ("suspended", "Suspended"),
        ]
    ]
    rows.extend([chips[i : i + 4] for i in range(0, len(chips), 4)])

    for account in result.items:
        icon = {"pending": "🟡", "active": "🟢", "suspended": "🔴", "revoked": "⚫"}[
            account.status.value
        ]
        name = account.business_name or (account.user.display_name if account.user else "-")
        lines.append(
            f"{icon} {esc(name)} · {account.orders_count} orders · "
            f"{money(account.sales_total or 0, account.sales_currency)}"
        )
        rows.append([button(f"🔗 {name[:22]}", adm("resellers", "view", account.id.hex))])

    if result.pages > 1:
        nav = []
        if result.has_prev:
            nav.append(button("◀", PageCB(scope="adm:resellers", page=result.page - 1, arg=filter_key).pack()))
        nav.append(button(result.label, adm("resellers", "noop")))
        if result.has_next:
            nav.append(button("▶", PageCB(scope="adm:resellers", page=result.page + 1, arg=filter_key).pack()))
        rows.append(nav)
    rows.append(admin_back_row())
    await render(event, "\n".join(lines), build(rows))


async def _detail(
    event, session: AsyncSession, admin: AdminContext, reseller_id: uuid.UUID
) -> None:
    repo = ResellerRepository(session)
    account = await repo.get(reseller_id)
    if account is None:
        await render(event, "⚠️ Reseller not found.", build([admin_back_row("resellers")]))
        return

    keys = await ApiKeyRepository(session).list_for_reseller(account.id)
    endpoints = await WebhookRepository(session).list_for_reseller(account.id)
    failures = await WebhookDeliveryRepository(session).failure_count(account.id)
    orders = await OrderRepository(session).list_for_reseller(account.id, per_page=5)

    name = account.business_name or (account.user.display_name if account.user else "-")
    lines = [
        f"🔗 <b>{esc(name)}</b>",
        "",
        f"Status: <b>{account.status.value}</b>",
        f"Joined: {short_date(account.created_at)}",
        f"Terms: {account.terms_version or '-'} ({humanize_datetime(account.terms_accepted_at)})",
        "",
        DIVIDER,
        "<b>USAGE</b>",
        f"API requests: {account.api_requests_count:,}",
        f"Orders: {account.orders_count:,}",
        f"Sales: {money(account.sales_total or 0, account.sales_currency)}",
        f"Rate limit: {account.rate_limit_per_minute}/min",
        "",
        DIVIDER,
        "<b>INTEGRATION</b>",
        f"Active API keys: {sum(1 for k in keys if not k.is_revoked)}/{len(keys)}",
        f"Webhook endpoints: {len(endpoints)}",
        f"Exhausted deliveries: {failures}",
    ]
    if account.ip_allowlist:
        lines.append(f"IP allowlist: {len(account.ip_allowlist)} entries")
    if account.suspended_reason:
        lines += ["", f"🔴 Suspended: {esc(account.suspended_reason)}"]

    if not orders.is_empty:
        lines += ["", DIVIDER, "<b>RECENT ORDERS</b>"]
        for order in orders.items:
            lines.append(f"• #{esc(order.reference)} · {order.status.value} · {money(order.total, order.currency)}")

    rows = []
    if admin.can(Permissions.RESELLERS_MANAGE):
        if account.status is ResellerStatus.PENDING:
            rows.append([button("✅ Approve", adm("resellers", "approve", account.id.hex))])
        elif account.status is ResellerStatus.ACTIVE:
            rows.append([button("🔴 Suspend", adm("resellers", "suspend", account.id.hex))])
        elif account.status is ResellerStatus.SUSPENDED:
            rows.append([button("🟢 Reinstate", adm("resellers", "reinstate", account.id.hex))])
    if admin.can(Permissions.RESELLERS_KEYS):
        rows.append([button("🔑 API keys", adm("resellers", "keys", account.id.hex))])
    if account.user_id and admin.can(Permissions.USERS_VIEW):
        rows.append([button("👤 User", adm("users", "view", account.user_id.hex))])
    rows.append(admin_back_row("resellers"))
    await render(event, "\n".join(lines), build(rows))


async def _keys(
    event, session: AsyncSession, admin: AdminContext, reseller_id: uuid.UUID
) -> None:
    """List a reseller's keys.

    Only non-secret metadata is shown: the platform stores a hash, so the key
    itself cannot be displayed even here.
    """
    admin.require(Permissions.RESELLERS_KEYS)
    keys = await ApiKeyRepository(session).list_for_reseller(reseller_id)
    lines = ["🔑 <b>API KEYS</b>", ""]
    if not keys:
        lines.append("No API keys.")
    for key in keys:
        state = "⚫ revoked" if key.is_revoked else "🟢 active"
        lines += [
            f"{state} <code>{key.prefix}_{key.public_id}</code>",
            f"{esc(key.name)} · {', '.join(key.scopes) or 'no scopes'}",
            f"Requests: {key.requests_count:,} · last used {humanize_datetime(key.last_used_at)}",
            "",
        ]
    lines.append("<i>Key material is never stored and cannot be displayed.</i>")

    rows = [[button("◀ Back", adm("resellers", "view", reseller_id.hex))], admin_back_row()]
    await render(event, "\n".join(lines), build(rows))


async def _approve(
    event, session: AsyncSession, admin: AdminContext, reseller_id: uuid.UUID
) -> None:
    admin.require(Permissions.RESELLERS_MANAGE)
    repo = ResellerRepository(session)
    account = await repo.get(reseller_id)
    if account is None:
        await render(event, "⚠️ Reseller not found.", build([admin_back_row("resellers")]))
        return
    account.status = ResellerStatus.ACTIVE
    await session.flush()
    await audit(
        session,
        admin,
        AuditAction.RESELLER_APPROVED,
        target_type="reseller",
        target_id=reseller_id,
        details={"business_name": account.business_name},
    )
    await _detail(event, session, admin, reseller_id)


async def _request_suspend(
    event, session: AsyncSession, admin: AdminContext, reseller_id: uuid.UUID
) -> None:
    admin.require(Permissions.RESELLERS_MANAGE)
    account = await ResellerRepository(session).get(reseller_id)
    if account is None:
        await render(event, "⚠️ Reseller not found.", build([admin_back_row("resellers")]))
        return

    token = await create_confirmation(
        actor_id=admin.user.id,
        action="reseller_suspend",
        payload={"target": str(reseller_id), "reason": f"suspended by {admin.label}"},
    )
    await render(
        event,
        "\n".join(
            [
                "🔴 <b>SUSPEND RESELLER</b>",
                "",
                f"<b>{esc(account.business_name or '-')}</b>",
                "",
                "All of their API keys are revoked immediately and further API "
                "requests are rejected.",
                "",
                "Existing orders and payment history are preserved.",
            ]
        ),
        confirm_keyboard(token, yes="✅ Suspend"),
    )


@register("reseller_suspend")
async def confirmed_suspend(
    callback: CallbackQuery, session: AsyncSession, admin: AdminContext, payload: dict
) -> None:
    admin.require(Permissions.RESELLERS_MANAGE)
    reseller_id = target_uuid(payload)
    account = await ResellerRepository(session).get(reseller_id)
    if account is None:
        await render(callback, "⚠️ Reseller not found.", build([admin_back_row("resellers")]))
        return

    await ResellerService(session).suspend(
        account=account, reason=payload.get("reason", ""), actor_id=admin.user.id
    )
    await audit(
        session,
        admin,
        AuditAction.RESELLER_SUSPENDED,
        target_type="reseller",
        target_id=reseller_id,
        reason=payload.get("reason"),
        details={"business_name": account.business_name},
    )
    await _detail(callback, session, admin, reseller_id)


async def _reinstate(
    event, session: AsyncSession, admin: AdminContext, reseller_id: uuid.UUID
) -> None:
    admin.require(Permissions.RESELLERS_MANAGE)
    account = await ResellerRepository(session).get(reseller_id)
    if account is None:
        await render(event, "⚠️ Reseller not found.", build([admin_back_row("resellers")]))
        return
    await ResellerService(session).reinstate(account)
    await audit(
        session,
        admin,
        AuditAction.RESELLER_APPROVED,
        target_type="reseller",
        target_id=reseller_id,
        reason="reinstated",
    )
    await _detail(event, session, admin, reseller_id)
