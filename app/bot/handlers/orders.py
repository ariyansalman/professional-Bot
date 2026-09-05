"""My Orders, order details, delivery status and product access (35-39)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import CheckoutCB, Nav, OrderCB, PageCB, pack_uuid, unpack_uuid
from app.bot.keyboards.common import build, button, nav_button
from app.bot.keyboards.customer import (
    delivery_status_keyboard,
    delivery_status_labels,
    empty_orders_keyboard,
    order_details_keyboard,
    order_list_keyboard,
    product_delivery_keyboard,
)
from app.bot.services.formatting import (
    delivered_product,
    esc,
    order_details,
    order_row,
    receipt,
)
from app.bot.services.screen import render
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.db.models.order import Order
from app.db.models.user import User
from app.db.repositories.orders import DeliveryRepository, OrderRepository
from app.db.repositories.payments import PaymentIntentRepository
from app.domain.enums import DeliveryStatus, Language, OrderStatus
from app.domain.orders.delivery import DeliveryService
from app.domain.orders.service import OrderService
from app.i18n import t

log = get_logger(__name__)
router = Router(name="orders")

PER_PAGE = 5

STATUS_FILTERS: dict[str, list[OrderStatus] | None] = {
    "all": None,
    "pending": [OrderStatus.CREATED, OrderStatus.PAYMENT_PENDING, OrderStatus.EXPIRED],
    "paid": [
        OrderStatus.PAYMENT_VERIFIED,
        OrderStatus.FULFILLING,
        OrderStatus.DELIVERY_FAILED,
        OrderStatus.MANUAL_REVIEW,
    ],
    "completed": [OrderStatus.DELIVERED, OrderStatus.COMPLETED],
    "cancelled": [OrderStatus.CANCELLED, OrderStatus.REFUNDED],
}


async def _load_order(session: AsyncSession, user: User, ref: str) -> Order | None:
    order = await OrderRepository(session).get_with_items(unpack_uuid(ref))
    if order is None or order.user_id != user.id:
        return None
    return order


@router.message(Command("orders"))
async def orders_command(
    message: Message, session: AsyncSession, user: User, lang: Language
) -> None:
    await _list(message, session, user, lang, "all", 1)


@router.callback_query(Nav.filter(F.to == "orders"))
async def orders_nav(
    callback: CallbackQuery, session: AsyncSession, user: User, lang: Language
) -> None:
    await _list(callback, session, user, lang, "all", 1)


@router.callback_query(OrderCB.filter(F.action == "list"))
async def orders_list(
    callback: CallbackQuery,
    callback_data: OrderCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    await _list(callback, session, user, lang, callback_data.arg or "all", callback_data.page)


@router.callback_query(PageCB.filter(F.scope == "orders"))
async def orders_page(
    callback: CallbackQuery,
    callback_data: PageCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    await _list(callback, session, user, lang, callback_data.arg or "all", callback_data.page)


async def _list(
    event, session: AsyncSession, user: User, lang: Language, filter_key: str, page: int
) -> None:
    statuses = STATUS_FILTERS.get(filter_key, None)
    result = await OrderRepository(session).list_for_user(
        user.id, statuses=statuses, page=page, per_page=PER_PAGE
    )

    if result.is_empty and filter_key == "all":
        await render(event, t("orders.empty", lang), empty_orders_keyboard(lang))
        return

    lines = [t("orders.title", lang), ""]
    if result.is_empty:
        lines.append("No orders in this filter.")
    else:
        lines.append("\n\n".join(order_row(order) for order in result.items))

    await render(event, "\n".join(lines), order_list_keyboard(lang, result, active_filter=filter_key))


@router.callback_query(OrderCB.filter(F.action == "view"))
async def order_view(
    callback: CallbackQuery,
    callback_data: OrderCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    order = await _load_order(session, user, callback_data.oid)
    if order is None:
        await render(callback, t("error.not_found", lang), empty_orders_keyboard(lang))
        return

    delivery_service = DeliveryService(session)
    summary = await delivery_service.status_summary(order.id)
    intent = await PaymentIntentRepository(session).active_for_order(order.id)

    await render(
        callback,
        order_details(order, lang, delivery_summary=summary),
        order_details_keyboard(
            lang,
            order,
            has_delivery=bool(summary["total"]),
            delivery_ready=bool(summary["ready"]),
            has_open_payment=intent is not None,
        ),
    )


@router.callback_query(OrderCB.filter(F.action == "delivery"))
async def delivery_status(
    callback: CallbackQuery,
    callback_data: OrderCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    """Delivery progress screen (section 35), including the delayed state."""
    order = await _load_order(session, user, callback_data.oid)
    if order is None:
        await render(callback, t("error.not_found", lang), empty_orders_keyboard(lang))
        return

    deliveries = await DeliveryRepository(session).list_for_order(order.id)
    if not deliveries:
        lines = [
            t("delivery.preparing_title", lang),
            "",
            t("delivery.payment_confirmed", lang) if order.status.is_paid else t("order.pending", lang),
            "",
            t("delivery.preparing", lang),
        ]
        await render(
            callback,
            "\n".join(lines),
            delivery_status_keyboard(lang, order, ready=False, failed=False),
        )
        return

    ready = all(d.status is DeliveryStatus.COMPLETED for d in deliveries)
    failed = any(d.status is DeliveryStatus.FAILED for d in deliveries)

    if failed and not ready:
        # Payment is safe; only delivery is delayed (section 37).
        await render(
            callback,
            "\n".join([t("delivery.delayed_title", lang), "", t("delivery.delayed_body", lang)]),
            delivery_status_keyboard(lang, order, ready=False, failed=True),
        )
        return

    title = t("delivery.ready_title", lang) if ready else t("delivery.preparing_title", lang)
    lines = [
        title,
        "",
        f"{t('payment.order', lang)}:",
        f"#{esc(order.reference)}",
        "",
        t("delivery.payment_confirmed", lang),
        "",
        t("delivery.inventory_allocated", lang) if ready else t("delivery.preparing", lang),
    ]
    for delivery in deliveries:
        lines.append(f"• {delivery_status_labels(delivery.status)}")
    if ready:
        lines += ["", t("delivery.completed", lang)]

    await render(
        callback,
        "\n".join(lines),
        delivery_status_keyboard(lang, order, ready=ready, failed=False),
    )


@router.callback_query(OrderCB.filter(F.action == "product"))
async def view_product(
    callback: CallbackQuery,
    callback_data: OrderCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    """Reveal the delivered digital product (section 36)."""
    order = await _load_order(session, user, callback_data.oid)
    if order is None:
        await render(callback, t("error.not_found", lang), empty_orders_keyboard(lang))
        return

    deliveries = await DeliveryRepository(session).list_for_order(order.id)
    completed = [d for d in deliveries if d.status is DeliveryStatus.COMPLETED]
    if not completed:
        await delivery_status(callback, callback_data, session, user, lang)
        return

    service = DeliveryService(session)
    payloads: list[str] = []
    file_ids: list[str] = []
    for delivery in completed:
        try:
            revealed = service.reveal(delivery)
        except AppError:
            continue
        payloads.extend(revealed.items)
        if revealed.file_id:
            file_ids.append(revealed.file_id)

    item = order.items[0] if order.items else None
    product_name = item.product_name if item else "-"

    if not payloads and not file_ids:
        # A manually-fulfilled order has no automatic payload to show.
        await render(
            callback,
            "\n".join(
                [
                    t("product.your_product", lang),
                    "",
                    f"<b>{esc(product_name)}</b>",
                    "",
                    t("product.manual_delivery", lang),
                ]
            ),
            product_delivery_keyboard(lang, order),
        )
        return

    if file_ids:
        await callback.answer()
        for file_id in file_ids:
            await callback.message.answer_document(file_id)

    await render(
        callback,
        delivered_product(
            product_name=product_name,
            payloads=payloads,
            order_reference=order.reference,
            lang=lang,
        ),
        product_delivery_keyboard(lang, order),
    )


@router.callback_query(OrderCB.filter(F.action == "receipt"))
async def order_receipt(
    callback: CallbackQuery,
    callback_data: OrderCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    order = await _load_order(session, user, callback_data.oid)
    if order is None:
        await render(callback, t("error.not_found", lang), empty_orders_keyboard(lang))
        return
    intent = await PaymentIntentRepository(session).verified_for_order(order.id)
    if intent is not None:
        intent = await PaymentIntentRepository(session).get_full(intent.id)
    await render(
        callback,
        receipt(order, intent, lang),
        build(
            [
                [button(t("btn.order_details", lang), OrderCB(action="view", oid=callback_data.oid).pack())],
                [nav_button(t("btn.home", lang), "home")],
            ]
        ),
    )


@router.callback_query(OrderCB.filter(F.action == "cancel"))
async def cancel_order(
    callback: CallbackQuery,
    callback_data: OrderCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    """Cancel an unpaid order, with a confirmation step (section 81)."""
    order = await _load_order(session, user, callback_data.oid)
    if order is None:
        await render(callback, t("error.not_found", lang), empty_orders_keyboard(lang))
        return

    if callback_data.arg != "confirm":
        await render(
            callback,
            "\n".join(
                [
                    "⚠️ <b>CANCEL ORDER</b>",
                    "",
                    f"Order #{esc(order.reference)} will be cancelled and the "
                    "reserved stock released.",
                    "",
                    "This cannot be undone.",
                ]
            ),
            build(
                [
                    [
                        button(
                            "✅ Yes, cancel",
                            OrderCB(action="cancel", oid=callback_data.oid, arg="confirm").pack(),
                        ),
                        button(
                            t("btn.back", lang),
                            OrderCB(action="view", oid=callback_data.oid).pack(),
                        ),
                    ]
                ]
            ),
        )
        return

    try:
        await OrderService(session).cancel(order, reason="cancelled by customer", actor_id=user.id)
    except AppError as exc:
        await render(
            callback,
            f"⚠️ {exc.safe_message}",
            build([[button(t("btn.order_details", lang), OrderCB(action="view", oid=callback_data.oid).pack())]]),
        )
        return

    await render(
        callback,
        f"❌ Order #{esc(order.reference)} cancelled.",
        build(
            [
                [nav_button(t("btn.shop", lang), "shop")],
                [button(t("btn.my_orders", lang), OrderCB(action="list").pack())],
            ]
        ),
    )


@router.callback_query(OrderCB.filter(F.action == "reorder"))
async def reorder(
    callback: CallbackQuery,
    callback_data: OrderCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    """Offer a fresh checkout for the same product.

    A new order is never created silently: the customer re-confirms price and
    quantity, because both may have changed since the original order.
    """
    order = await _load_order(session, user, callback_data.oid)
    if order is None or not order.items:
        await render(callback, t("error.not_found", lang), empty_orders_keyboard(lang))
        return

    item = order.items[0]
    if item.product_id is None:
        await callback.answer(t("error.not_found", lang), show_alert=True)
        return

    from app.db.repositories.catalog import ProductRepository

    product = await ProductRepository(session).get_active(item.product_id)
    if product is None:
        await render(
            callback,
            "⚠️ This product is no longer available.",
            build([[nav_button(t("btn.shop", lang), "shop"), nav_button(t("btn.home", lang), "home")]]),
        )
        return

    await render(
        callback,
        "\n".join(
            [
                "🔁 <b>REORDER</b>",
                "",
                esc(product.display_name(lang.value)),
                f"{product.price} {product.currency}",
                "",
                "Confirm the details on the next screen.",
            ]
        ),
        build(
            [
                [
                    button(
                        t("btn.buy_now", lang),
                        CheckoutCB(
                            action="open", pid=pack_uuid(product.id), qty=item.quantity
                        ).pack(),
                    )
                ],
                [
                    button(t("btn.back", lang), OrderCB(action="view", oid=callback_data.oid).pack()),
                    nav_button(t("btn.home", lang), "home"),
                ],
            ]
        ),
    )
