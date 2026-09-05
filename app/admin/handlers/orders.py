"""Admin order management (section 57)."""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.filters import IsAdmin
from app.admin.handlers.confirmations import register, target_uuid
from app.admin.keyboards.panels import adm, admin_back_row
from app.admin.permissions.rbac import Permissions
from app.admin.services.context import AdminContext, audit
from app.bot.callbacks import AdminCB, PageCB
from app.bot.keyboards.common import build, button
from app.bot.services.formatting import DIVIDER, esc, money
from app.bot.services.screen import loading, render
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.timeutils import humanize_datetime
from app.db.repositories.orders import DeliveryRepository, LedgerRepository, OrderRepository
from app.db.repositories.payments import PaymentIntentRepository
from app.domain.enums import AuditAction, DeliveryStatus, OrderStatus
from app.domain.orders.delivery import DeliveryService
from app.domain.orders.service import OrderService

log = get_logger(__name__)
router = Router(name="admin_orders")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

FILTERS: dict[str, list[OrderStatus] | None] = {
    "all": None,
    "pending": [OrderStatus.CREATED, OrderStatus.PAYMENT_PENDING],
    "paid": [OrderStatus.PAYMENT_VERIFIED],
    "fulfilling": [OrderStatus.FULFILLING, OrderStatus.DELIVERY_FAILED],
    "completed": [OrderStatus.DELIVERED, OrderStatus.COMPLETED],
    "review": [OrderStatus.MANUAL_REVIEW],
    "cancelled": [OrderStatus.CANCELLED, OrderStatus.EXPIRED],
    "refunded": [OrderStatus.REFUNDED],
}

FILTER_LABELS = [
    ("all", "All"),
    ("pending", "Pending"),
    ("paid", "Paid"),
    ("fulfilling", "Fulfilling"),
    ("completed", "Completed"),
    ("review", "Review"),
    ("cancelled", "Cancelled"),
    ("refunded", "Refunded"),
]


@router.callback_query(AdminCB.filter(F.section == "orders"))
async def orders_section(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, admin: AdminContext
) -> None:
    admin.require(Permissions.ORDERS_VIEW)
    action = callback_data.action
    if action == "view":
        await _detail(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "retry_delivery":
        await _retry_delivery(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "cancel":
        await _request_cancel(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "noop":
        await callback.answer()
    else:
        await _list(callback, session, admin, callback_data.arg or "all", callback_data.page)


@router.callback_query(PageCB.filter(F.scope == "adm:orders"))
async def orders_page(
    callback: CallbackQuery, callback_data: PageCB, session: AsyncSession, admin: AdminContext
) -> None:
    admin.require(Permissions.ORDERS_VIEW)
    await _list(callback, session, admin, callback_data.arg or "all", callback_data.page)


async def _list(
    event, session: AsyncSession, admin: AdminContext, filter_key: str, page: int
) -> None:
    statuses = FILTERS.get(filter_key)
    result = await OrderRepository(session).list_for_admin(
        statuses=statuses, page=page, per_page=6
    )

    lines = ["📦 <b>ORDERS</b>", "", f"Filter: <b>{filter_key}</b> · {result.total} total", DIVIDER]
    if result.is_empty:
        lines += ["", "No orders in this filter."]

    rows = []
    chips = [
        button(f"• {label} •" if key == filter_key else label, adm("orders", arg=key))
        for key, label in FILTER_LABELS
    ]
    rows.extend([chips[i : i + 4] for i in range(0, len(chips), 4)])

    for order in result.items:
        item = order.items[0] if order.items else None
        lines += [
            "",
            f"#{esc(order.reference)} · <b>{order.status.value}</b>",
            f"{money(order.total, order.currency)} · {esc(item.product_name if item else '-')}",
        ]
        rows.append([button(f"📦 {order.reference}", adm("orders", "view", order.id.hex))])

    if result.pages > 1:
        nav = []
        if result.has_prev:
            nav.append(button("◀", PageCB(scope="adm:orders", page=result.page - 1, arg=filter_key).pack()))
        nav.append(button(result.label, adm("orders", "noop")))
        if result.has_next:
            nav.append(button("▶", PageCB(scope="adm:orders", page=result.page + 1, arg=filter_key).pack()))
        rows.append(nav)
    rows.append(admin_back_row())
    await render(event, "\n".join(lines), build(rows))


async def _detail(
    event, session: AsyncSession, admin: AdminContext, order_id: uuid.UUID
) -> None:
    order = await OrderRepository(session).get_with_items(order_id)
    if order is None:
        await render(event, "⚠️ Order not found.", build([admin_back_row("orders")]))
        return

    intent = await PaymentIntentRepository(session).latest_for_order(order.id)
    deliveries = await DeliveryRepository(session).list_for_order(order.id)
    ledger = await LedgerRepository(session).list_for_order(order.id)

    lines = [
        f"📦 <b>ORDER #{esc(order.reference)}</b>",
        "",
        f"Status: <b>{order.status.value}</b>",
        f"Customer: {esc(order.user.display_name) if order.user else 'reseller/api'}",
        f"Channel: {esc(order.channel)}",
        f"Created: {humanize_datetime(order.created_at)}",
        "",
        DIVIDER,
        "<b>ITEMS</b>",
    ]
    for item in order.items:
        lines.append(
            f"• {esc(item.product_name)} × {item.quantity} — {money(item.line_total, item.currency)}"
        )
    lines += [
        "",
        f"Subtotal: {money(order.subtotal, order.currency)}",
        f"Discount: {money(order.discount_total, order.currency)}",
        f"<b>Total: {money(order.total, order.currency)}</b>",
    ]
    if order.coupon_code:
        lines.append(f"Coupon: {esc(order.coupon_code)}")

    lines += ["", DIVIDER, "<b>PAYMENT</b>"]
    if intent is None:
        lines.append("No payment intent yet.")
    else:
        lines += [
            f"{intent.status.value} · {money(intent.expected_amount, intent.asset)} · {intent.network.value}",
            f"Received: {money(intent.received_amount or 0, intent.asset)}",
        ]

    lines += ["", DIVIDER, "<b>DELIVERY</b>"]
    if not deliveries:
        lines.append("Not started.")
    for delivery in deliveries:
        lines.append(
            f"• {delivery.status.value} · attempts {delivery.attempts}"
            + (f" · {esc(delivery.last_error[:60])}" if delivery.last_error else "")
        )

    if ledger:
        lines += ["", DIVIDER, "<b>LEDGER</b>"]
        for entry in ledger[-5:]:
            lines.append(
                f"• {entry.entry_type.value} {money(entry.amount, entry.currency)} · "
                f"{humanize_datetime(entry.created_at)}"
            )

    rows = []
    if intent is not None and admin.can(Permissions.PAYMENTS_VIEW):
        rows.append([button("💳 Payment", adm("payments", "view", intent.id.hex))])
    if order.status in (OrderStatus.DELIVERY_FAILED, OrderStatus.FULFILLING) and admin.can(
        Permissions.ORDERS_FORCE_DELIVERY
    ):
        rows.append([button("🔁 Retry delivery", adm("orders", "retry_delivery", order.id.hex))])
    if not order.status.is_paid and not order.status.is_terminal and admin.can(
        Permissions.ORDERS_CANCEL
    ):
        rows.append([button("❌ Cancel order", adm("orders", "cancel", order.id.hex))])
    if admin.can(Permissions.AUDIT_VIEW):
        rows.append([button("🧾 Audit", adm("audit", arg=order.id.hex))])
    rows.append(admin_back_row("orders"))
    await render(event, "\n".join(lines), build(rows))


async def _retry_delivery(
    event, session: AsyncSession, admin: AdminContext, order_id: uuid.UUID
) -> None:
    """Re-run delivery for a paid order whose fulfilment failed.

    The payment check still applies: an unpaid order can never be delivered,
    not even by an administrator.
    """
    admin.require(Permissions.ORDERS_FORCE_DELIVERY)
    if isinstance(event, CallbackQuery):
        await loading(event, "⏳ Retrying delivery...")

    order = await OrderRepository(session).get_with_items(order_id)
    if order is None:
        await render(event, "⚠️ Order not found.", build([admin_back_row("orders")]))
        return

    service = DeliveryService(session)
    try:
        await service.assert_paid(order)
        deliveries = await service.prepare(order)
        for delivery in deliveries:
            if delivery.status is not DeliveryStatus.COMPLETED:
                # Clear the backoff so the worker picks it up immediately.
                delivery.status = DeliveryStatus.PENDING
                delivery.next_attempt_at = None
        await session.flush()
    except AppError as exc:
        await render(
            event,
            f"⚠️ {exc.safe_message}",
            build([[button("◀ Back", adm("orders", "view", order_id.hex))]]),
        )
        return

    await audit(
        session,
        admin,
        AuditAction.ORDER_FORCED_DELIVERY,
        target_type="order",
        target_id=order_id,
        details={"reference": order.reference},
    )
    await render(
        event,
        f"🔁 Delivery re-queued for <b>#{esc(order.reference)}</b>.",
        build([[button("📦 View order", adm("orders", "view", order_id.hex))], admin_back_row()]),
    )


async def _request_cancel(
    event, session: AsyncSession, admin: AdminContext, order_id: uuid.UUID
) -> None:
    """Cancelling an order is destructive, so it is confirmed first."""
    admin.require(Permissions.ORDERS_CANCEL)
    order = await OrderRepository(session).get_with_items(order_id)
    if order is None:
        await render(event, "⚠️ Order not found.", build([admin_back_row("orders")]))
        return

    from app.admin.keyboards.panels import confirm_keyboard
    from app.admin.services.context import create_confirmation

    token = await create_confirmation(
        actor_id=admin.user.id,
        action="order_cancel",
        payload={"target": str(order_id), "reason": f"cancelled by {admin.label}"},
    )
    await render(
        event,
        "\n".join(
            [
                "⚠️ <b>CANCEL ORDER</b>",
                "",
                f"Order: <b>#{esc(order.reference)}</b>",
                f"Total: {money(order.total, order.currency)}",
                "",
                "Reserved stock will be released and any coupon returned.",
                "This is recorded in the audit log.",
            ]
        ),
        confirm_keyboard(token, yes="✅ Cancel order", no="◀ Back"),
    )


@register("order_cancel")
async def confirmed_cancel(
    callback: CallbackQuery, session: AsyncSession, admin: AdminContext, payload: dict
) -> None:
    admin.require(Permissions.ORDERS_CANCEL)
    order_id = target_uuid(payload)
    reason = payload.get("reason", "cancelled by admin")

    order = await OrderRepository(session).get_with_items(order_id)
    if order is None:
        await render(callback, "⚠️ Order not found.", build([admin_back_row("orders")]))
        return
    try:
        await OrderService(session).cancel(order, reason=reason, actor_id=admin.user.id)
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
        AuditAction.ORDER_CANCELLED,
        target_type="order",
        target_id=order_id,
        reason=reason,
        details={"reference": order.reference},
    )
    await render(
        callback,
        f"❌ Order <b>#{esc(order.reference)}</b> cancelled.",
        build([admin_back_row("orders")]),
    )
