"""Admin refund management (sections 104, 114).

Refunds follow the same shape as every other high-risk action: request with a
written reason, an explicit confirmation, and an audit entry. Completing one
requires the external reference of the transfer the operator actually sent, so
a refund is never closed without evidence behind it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.filters import IsAdmin
from app.admin.handlers.confirmations import register, register_reason, target_uuid
from app.admin.keyboards.panels import adm, admin_back_row, confirm_keyboard
from app.admin.permissions.rbac import Permissions
from app.admin.services.context import AdminContext, audit, create_confirmation
from app.bot.callbacks import AdminCB
from app.bot.keyboards.common import build, button
from app.bot.services.formatting import DIVIDER, esc, money
from app.bot.services.screen import render
from app.bot.states import AdminFlow
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.timeutils import humanize_datetime
from app.db.repositories.orders import OrderRepository, RefundRepository
from app.domain.enums import AuditAction, RefundStatus
from app.domain.orders.refunds import RefundService

log = get_logger(__name__)
router = Router(name="admin_refunds")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

STATUS_ICON = {
    RefundStatus.REQUESTED: "🟡",
    RefundStatus.APPROVED: "🔵",
    RefundStatus.PROCESSING: "🔵",
    RefundStatus.COMPLETED: "🟢",
    RefundStatus.REJECTED: "⚫",
    RefundStatus.FAILED: "🔴",
}


@router.callback_query(AdminCB.filter(F.section == "refunds"))
async def refunds_section(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: AdminContext,
    state: FSMContext,
) -> None:
    admin.require(Permissions.PAYMENTS_VIEW)
    action = callback_data.action

    if action == "new":
        await _prompt_amount(callback, session, admin, uuid.UUID(callback_data.arg), state)
    elif action == "view":
        await _detail(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "approve":
        await _request_approval(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "complete":
        await _prompt_reference(callback, session, admin, uuid.UUID(callback_data.arg), state)
    elif action == "reject":
        await _reject(callback, session, admin, uuid.UUID(callback_data.arg))
    else:
        await _list(callback, session, admin, callback_data.page)


async def _list(event, session: AsyncSession, admin: AdminContext, page: int) -> None:
    result = await RefundRepository(session).list_pending(page=page, per_page=8)
    lines = ["↩️ <b>REFUNDS</b>", "", f"{result.total} open refund(s)", DIVIDER]
    if result.is_empty:
        lines += ["", "✅ No refunds awaiting action."]

    rows = []
    orders = OrderRepository(session)
    for refund in result.items:
        order = await orders.get_with_items(refund.order_id)
        reference = order.reference if order else str(refund.order_id)[:8]
        lines += [
            "",
            f"{STATUS_ICON[refund.status]} <b>#{esc(reference)}</b> · {refund.status.value}",
            f"{money(refund.amount, refund.currency)} · {esc(refund.reason[:60])}",
        ]
        rows.append(
            [button(f"↩️ #{reference} · {refund.amount}", adm("refunds", "view", refund.id.hex))]
        )
    rows.append(admin_back_row())
    await render(event, "\n".join(lines), build(rows))


async def _detail(
    event, session: AsyncSession, admin: AdminContext, refund_id: uuid.UUID
) -> None:
    service = RefundService(session)
    refund = await service.refunds.get(refund_id)
    if refund is None:
        await render(event, "⚠️ Refund not found.", build([admin_back_row("refunds")]))
        return

    order = await OrderRepository(session).get_with_items(refund.order_id)
    lines = [
        "↩️ <b>REFUND</b>",
        "",
        f"Order: <b>#{esc(order.reference) if order else '-'}</b>",
        f"Amount: <b>{money(refund.amount, refund.currency)}</b>",
        f"Status: <b>{refund.status.value}</b>",
        "",
        f"Reason: {esc(refund.reason)}",
        f"Requested: {humanize_datetime(refund.created_at)}",
    ]
    if refund.destination:
        lines.append(f"Destination: <code>{esc(refund.destination)}</code>")
    if refund.external_reference:
        lines.append(f"Reference: <code>{esc(refund.external_reference)}</code>")
    if refund.processed_at:
        lines.append(f"Completed: {humanize_datetime(refund.processed_at)}")
    if refund.notes:
        lines.append(f"Notes: {esc(refund.notes)}")

    lines += [
        "",
        DIVIDER,
        "<i>This platform records refunds; it never moves funds itself. Send the "
        "transfer from your own wallet or exchange, then attach the reference.</i>",
    ]

    rows = []
    if refund.status is RefundStatus.REQUESTED and admin.can(Permissions.REFUNDS_CREATE):
        rows.append([button("✅ Approve", adm("refunds", "approve", refund.id.hex))])
        rows.append([button("❌ Reject", adm("refunds", "reject", refund.id.hex))])
    if refund.status is RefundStatus.APPROVED and admin.can(Permissions.REFUNDS_COMPLETE):
        rows.append([button("💸 Mark sent", adm("refunds", "complete", refund.id.hex))])
    if order is not None and admin.can(Permissions.ORDERS_VIEW):
        rows.append([button("📦 Order", adm("orders", "view", order.id.hex))])
    rows.append(admin_back_row("refunds"))
    await render(event, "\n".join(lines), build(rows))


async def _prompt_amount(
    event, session: AsyncSession, admin: AdminContext, order_id: uuid.UUID, state: FSMContext
) -> None:
    """Start a refund from an order. The refundable remainder is shown up front."""
    admin.require(Permissions.REFUNDS_CREATE)
    order = await OrderRepository(session).get_with_items(order_id)
    if order is None:
        await render(event, "⚠️ Order not found.", build([admin_back_row("orders")]))
        return

    service = RefundService(session)
    remaining = await service.refundable_amount(order)
    if remaining <= 0:
        await render(
            event,
            "⚠️ This order has nothing left to refund.",
            build([[button("◀ Back", adm("orders", "view", order_id.hex))]]),
        )
        return

    await state.set_state(AdminFlow.action_reason)
    await state.update_data(pending_action="refund_create", pending_target=str(order_id))
    await render(
        event,
        "\n".join(
            [
                "↩️ <b>CREATE REFUND</b>",
                "",
                f"Order: <b>#{esc(order.reference)}</b>",
                f"Refundable: <b>{money(remaining, order.currency)}</b>",
                "",
                "Send the amount and a reason, for example:",
                "<code>10.00 duplicate payment</code>",
                "",
                "Send just a reason to refund the full remaining amount.",
            ]
        ),
        build([[button("❌ Cancel", adm("orders", "view", order_id.hex))]]),
    )


@register("refund_create")
async def confirmed_create(
    callback: CallbackQuery, session: AsyncSession, admin: AdminContext, payload: dict
) -> None:
    admin.require(Permissions.REFUNDS_CREATE)
    order_id = target_uuid(payload)
    order = await OrderRepository(session).get_with_items(order_id)
    if order is None:
        await render(callback, "⚠️ Order not found.", build([admin_back_row("orders")]))
        return

    amount = payload.get("amount")
    service = RefundService(session)
    try:
        refund = await service.request(
            order=order,
            amount=Decimal(amount) if amount else None,
            reason=payload.get("reason", ""),
            requested_by_id=admin.user.id,
        )
    except AppError as exc:
        await render(
            callback,
            f"⚠️ {exc.safe_message}",
            build([[button("◀ Back", adm("orders", "view", order_id.hex))]]),
        )
        return

    await audit(
        session,
        admin,
        AuditAction.REFUND_CREATED,
        target_type="refund",
        target_id=refund.id,
        reason=refund.reason,
        details={"order": order.reference, "amount": str(refund.amount)},
    )
    await _detail(callback, session, admin, refund.id)


async def _request_approval(
    event, session: AsyncSession, admin: AdminContext, refund_id: uuid.UUID
) -> None:
    admin.require(Permissions.REFUNDS_CREATE)
    service = RefundService(session)
    refund = await service.refunds.get(refund_id)
    if refund is None:
        await render(event, "⚠️ Refund not found.", build([admin_back_row("refunds")]))
        return

    token = await create_confirmation(
        actor_id=admin.user.id,
        action="refund_approve",
        payload={"target": str(refund_id)},
    )
    await render(
        event,
        "\n".join(
            [
                "⚠️ <b>APPROVE REFUND</b>",
                "",
                f"Amount: <b>{money(refund.amount, refund.currency)}</b>",
                f"Reason: {esc(refund.reason)}",
                "",
                "Approving authorises you to send the funds. The platform does "
                "not move money; you will attach the transfer reference "
                "afterwards.",
            ]
        ),
        confirm_keyboard(token, yes="✅ Approve refund"),
    )


@register("refund_approve")
async def confirmed_approve(
    callback: CallbackQuery, session: AsyncSession, admin: AdminContext, payload: dict
) -> None:
    admin.require(Permissions.REFUNDS_CREATE)
    refund_id = target_uuid(payload)
    service = RefundService(session)
    refund = await service.refunds.get(refund_id)
    if refund is None:
        await render(callback, "⚠️ Refund not found.", build([admin_back_row("refunds")]))
        return
    try:
        await service.approve(refund=refund, actor_id=admin.user.id)
    except AppError as exc:
        await render(callback, f"⚠️ {exc.safe_message}", build([admin_back_row("refunds")]))
        return

    await audit(
        session,
        admin,
        AuditAction.REFUND_CREATED,
        target_type="refund",
        target_id=refund_id,
        reason="approved",
        details={"amount": str(refund.amount)},
    )
    await _detail(callback, session, admin, refund_id)


async def _reject(
    event, session: AsyncSession, admin: AdminContext, refund_id: uuid.UUID
) -> None:
    admin.require(Permissions.REFUNDS_CREATE)
    service = RefundService(session)
    refund = await service.refunds.get(refund_id)
    if refund is None:
        await render(event, "⚠️ Refund not found.", build([admin_back_row("refunds")]))
        return
    try:
        await service.reject(
            refund=refund, actor_id=admin.user.id, reason=f"rejected by {admin.label}"
        )
    except AppError as exc:
        await render(event, f"⚠️ {exc.safe_message}", build([admin_back_row("refunds")]))
        return
    await audit(
        session,
        admin,
        AuditAction.REFUND_CREATED,
        target_type="refund",
        target_id=refund_id,
        reason="rejected",
    )
    await _detail(event, session, admin, refund_id)


async def _prompt_reference(
    event, session: AsyncSession, admin: AdminContext, refund_id: uuid.UUID, state: FSMContext
) -> None:
    admin.require(Permissions.REFUNDS_COMPLETE)
    await state.set_state(AdminFlow.action_reason)
    await state.update_data(pending_action="refund_complete", pending_target=str(refund_id))
    await render(
        event,
        "\n".join(
            [
                "💸 <b>MARK REFUND SENT</b>",
                "",
                "Send the transaction reference of the transfer you made.",
                "",
                "It is recorded as the evidence for this refund, so a refund is "
                "never closed without proof behind it.",
            ]
        ),
        build([[button("❌ Cancel", adm("refunds", "view", refund_id.hex))]]),
    )


@register("refund_complete")
async def confirmed_complete(
    callback: CallbackQuery, session: AsyncSession, admin: AdminContext, payload: dict
) -> None:
    admin.require(Permissions.REFUNDS_COMPLETE)
    refund_id = target_uuid(payload)
    service = RefundService(session)
    refund = await service.refunds.get(refund_id)
    if refund is None:
        await render(callback, "⚠️ Refund not found.", build([admin_back_row("refunds")]))
        return
    try:
        await service.complete(
            refund=refund,
            actor_id=admin.user.id,
            external_reference=payload.get("reason", ""),
        )
    except AppError as exc:
        await render(callback, f"⚠️ {exc.safe_message}", build([admin_back_row("refunds")]))
        return

    await audit(
        session,
        admin,
        AuditAction.REFUND_COMPLETED,
        target_type="refund",
        target_id=refund_id,
        reason=refund.external_reference,
        details={"amount": str(refund.amount), "currency": refund.currency},
    )
    await _detail(callback, session, admin, refund_id)


@register_reason("refund_create")
async def _refund_create_reason(
    message: Message,
    session: AsyncSession,
    admin: AdminContext,
    action: str,
    target: str,
    text: str,
) -> None:
    """Parse ``"<amount> <reason>"`` and ask for confirmation."""
    amount, reason = _parse_amount_and_reason(text)
    if not reason:
        await render(
            message,
            "⚠️ A reason is required.",
            build([[button("◀ Back", adm("orders", "view", target))]]),
        )
        return

    token = await create_confirmation(
        actor_id=admin.user.id,
        action="refund_create",
        payload={"target": target, "amount": str(amount) if amount else "", "reason": reason},
    )
    await render(
        message,
        "\n".join(
            [
                "⚠️ <b>CONFIRM REFUND</b>",
                "",
                f"Amount: <b>{amount if amount else 'full remaining'}</b>",
                f"Reason: {esc(reason)}",
                "",
                "This is a financial action and is recorded in the audit log.",
            ]
        ),
        confirm_keyboard(token, yes="✅ Create refund"),
    )


@register_reason("refund_complete")
async def _refund_complete_reason(
    message: Message,
    session: AsyncSession,
    admin: AdminContext,
    action: str,
    target: str,
    text: str,
) -> None:
    """Capture the external transfer reference and ask for confirmation."""
    if len(text) < 4:
        await render(
            message,
            "⚠️ That does not look like a transaction reference.",
            build([[button("◀ Back", adm("refunds", "view", target))]]),
        )
        return

    token = await create_confirmation(
        actor_id=admin.user.id,
        action="refund_complete",
        payload={"target": target, "reason": text},
    )
    await render(
        message,
        "\n".join(
            [
                "⚠️ <b>CONFIRM REFUND SENT</b>",
                "",
                f"Reference: <code>{esc(text)}</code>",
                "",
                "The refund is journalled and the customer is notified.",
            ]
        ),
        confirm_keyboard(token, yes="✅ Mark sent"),
    )


def _parse_amount_and_reason(text: str) -> tuple[Decimal | None, str]:
    """``"10.00 duplicate payment"`` -> ``(Decimal("10.00"), "duplicate payment")``.

    A leading number is treated as the amount; without one the whole message is
    the reason and the full remaining balance is refunded.
    """
    parts = text.split(maxsplit=1)
    if not parts:
        return None, ""
    try:
        amount = Decimal(parts[0])
    except (InvalidOperation, ValueError):
        return None, text.strip()
    return amount, (parts[1].strip() if len(parts) > 1 else "")
