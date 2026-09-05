"""Payment method, payment instruction and verification screens (18-34)."""

from __future__ import annotations

import io

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import OrderCB, PayCB, pack_uuid, unpack_uuid
from app.bot.keyboards.common import build, button, nav_button
from app.bot.keyboards.customer import (
    payment_method_keyboard,
    payment_screen_keyboard,
    payment_status_keyboard,
)
from app.bot.services.formatting import (
    blockchain_payment_screen,
    exchange_payment_screen,
    payment_status_screen,
    payment_submitted_screen,
)
from app.bot.services.screen import loading, render
from app.bot.states import CheckoutFlow
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.db.models.order import Order
from app.db.models.user import User
from app.db.repositories.orders import OrderRepository
from app.db.repositories.payments import PaymentIntentRepository, PaymentMethodRepository
from app.domain.enums import Language, PaymentProviderKind, PaymentStatus
from app.domain.payments.registry import requires_customer_reference
from app.domain.payments.service import PaymentService
from app.i18n import t

log = get_logger(__name__)
router = Router(name="payments")


async def _load_order(session: AsyncSession, user: User, ref: str) -> Order | None:
    """Load an order and verify it belongs to the requesting customer."""
    order = await OrderRepository(session).get_with_items(unpack_uuid(ref))
    if order is None or order.user_id != user.id:
        return None
    return order


@router.callback_query(PayCB.filter(F.action == "methods"))
async def payment_methods(
    callback: CallbackQuery,
    callback_data: PayCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    order = await _load_order(session, user, callback_data.oid)
    if order is None:
        await render(callback, t("error.not_found", lang), build([[nav_button(t("btn.home", lang), "home")]]))
        return
    await show_payment_methods(callback, session, order, lang)


async def show_payment_methods(
    event, session: AsyncSession, order: Order, lang: Language
) -> None:
    """Only enabled, configured and healthy methods are offered (section 18)."""
    methods = await PaymentMethodRepository(session).list_available()
    if not methods:
        log.error("payment.no_methods_available", order=order.reference)
        await render(
            event,
            t("payment.none_available", lang),
            build(
                [
                    [nav_button(t("btn.support", lang), "support")],
                    [nav_button(t("btn.home", lang), "home")],
                ]
            ),
        )
        return

    lines = [
        t("payment.method_title", lang),
        "",
        f"{t('payment.order', lang)}: #{order.reference}",
        f"{t('checkout.total', lang)}: <b>{order.total} {order.currency}</b>",
    ]
    exchange = [m for m in methods if m.provider.kind is PaymentProviderKind.EXCHANGE]
    blockchain = [m for m in methods if m.provider.kind is PaymentProviderKind.BLOCKCHAIN]
    if exchange:
        lines += ["", f"<b>{t('payment.exchange', lang)}</b>"]
    if blockchain:
        lines += ["", f"<b>{t('payment.blockchain', lang)}</b>"]

    await render(event, "\n".join(lines), payment_method_keyboard(lang, methods, order))


@router.callback_query(PayCB.filter(F.action == "select"))
async def select_method(
    callback: CallbackQuery,
    callback_data: PayCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    """Create the payment intent and show the instruction screen."""
    order = await _load_order(session, user, callback_data.oid)
    if order is None:
        await render(callback, t("error.not_found", lang), build([[nav_button(t("btn.home", lang), "home")]]))
        return

    method = await PaymentMethodRepository(session).get_by_code(callback_data.ref)
    if method is None:
        await callback.answer(t("error.not_found", lang), show_alert=True)
        return

    payments = PaymentService(session)
    try:
        intent = await payments.create_intent(order=order, method=method)
    except AppError as exc:
        log.info("payment.intent_rejected", order=order.reference, code=exc.code, detail=exc.detail[:200])
        await render(
            callback,
            f"⚠️ {exc.safe_message}",
            build(
                [
                    [button(t("btn.back", lang), PayCB(action="methods", oid=callback_data.oid).pack())],
                    [nav_button(t("btn.support", lang), "support")],
                ]
            ),
        )
        return

    await _render_payment_instructions(callback, intent, order, lang)


async def _render_payment_instructions(event, intent, order: Order, lang: Language) -> None:
    is_blockchain = intent.method.provider.kind is PaymentProviderKind.BLOCKCHAIN
    text = (
        blockchain_payment_screen(intent, lang)
        if is_blockchain
        else exchange_payment_screen(intent, lang)
    )
    await render(
        event,
        text,
        payment_screen_keyboard(
            lang,
            intent,
            order,
            needs_reference=requires_customer_reference(intent.provider_code),
        ),
    )


@router.callback_query(PayCB.filter(F.action == "screen"))
async def payment_screen(
    callback: CallbackQuery,
    callback_data: PayCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    order = await _load_order(session, user, callback_data.oid)
    if order is None:
        await render(callback, t("error.not_found", lang), build([[nav_button(t("btn.home", lang), "home")]]))
        return
    intent = await PaymentIntentRepository(session).active_for_order(order.id)
    if intent is None:
        await show_payment_methods(callback, session, order, lang)
        return
    await _render_payment_instructions(callback, intent, order, lang)


@router.callback_query(PayCB.filter(F.action == "qr"))
async def payment_qr(
    callback: CallbackQuery,
    callback_data: PayCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    """Render the receiving address as a QR code image."""
    order = await _load_order(session, user, callback_data.oid)
    if order is None:
        await callback.answer(t("error.not_found", lang), show_alert=True)
        return
    intent = await PaymentIntentRepository(session).active_for_order(order.id)
    if intent is None:
        await callback.answer(t("error.expired_session", lang), show_alert=True)
        return

    try:
        import qrcode

        image = qrcode.make(intent.destination)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
    except Exception:
        log.exception("payment.qr_failed", intent_id=str(intent.id))
        await callback.answer(t("error.generic", lang), show_alert=True)
        return

    caption = "\n".join(
        [
            f"📱 <b>{intent.method.display_name}</b>",
            "",
            f"<code>{intent.destination}</code>",
            "",
            f"{t('payment.amount', lang)}: <b>{intent.expected_amount} {intent.asset}</b>",
        ]
    )
    if intent.memo:
        caption += f"\n\n{t('payment.memo_required', lang)}\n<code>{intent.memo}</code>"

    await callback.answer()
    await callback.message.answer_photo(
        BufferedInputFile(buffer.read(), filename=f"{intent.reference}.png"),
        caption=caption,
        reply_markup=build(
            [[button(t("btn.back", lang), PayCB(action="screen", oid=callback_data.oid).pack())]]
        ),
    )


@router.callback_query(PayCB.filter(F.action == "paid"))
async def i_have_paid(
    callback: CallbackQuery,
    callback_data: PayCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    """Record the customer's claim. This does not credit anything."""
    order = await _load_order(session, user, callback_data.oid)
    if order is None:
        await render(callback, t("error.not_found", lang), build([[nav_button(t("btn.home", lang), "home")]]))
        return

    intent = await PaymentIntentRepository(session).active_for_order(order.id)
    if intent is None:
        await _render_status(callback, session, order, lang)
        return

    payments = PaymentService(session)
    try:
        await payments.submit_payment(intent=intent, source="telegram")
    except AppError as exc:
        await render(
            callback,
            f"⚠️ {exc.safe_message}",
            payment_status_keyboard(lang, intent, order),
        )
        return

    await render(
        callback,
        payment_submitted_screen(intent, lang),
        payment_status_keyboard(lang, intent, order),
    )


@router.callback_query(PayCB.filter(F.action == "submit"))
async def submit_reference_prompt(
    callback: CallbackQuery, callback_data: PayCB, lang: Language, state: FSMContext
) -> None:
    await state.set_state(CheckoutFlow.entering_payment_reference)
    await state.update_data(payment_order_id=callback_data.oid)
    text = "\n".join(
        [
            t("payment.submit_reference_title", lang),
            "",
            t("payment.submit_reference_prompt", lang),
        ]
    )
    await render(
        callback,
        text,
        build([[button(t("btn.back", lang), PayCB(action="screen", oid=callback_data.oid).pack())]]),
    )


@router.message(CheckoutFlow.entering_payment_reference, F.text)
async def submit_reference(
    message: Message,
    session: AsyncSession,
    user: User,
    lang: Language,
    state: FSMContext,
) -> None:
    """Accept a transaction reference.

    The reference is validated for shape and stored as a *lookup hint*. It is
    never treated as proof of payment: the verification worker still has to
    find and validate the transaction independently.
    """
    data = await state.get_data()
    order_ref = data.get("payment_order_id")
    await state.clear()
    reference = (message.text or "").strip()

    if not order_ref:
        await render(message, t("error.expired_session", lang), build([[nav_button(t("btn.home", lang), "home")]]))
        return
    order = await _load_order(session, user, order_ref)
    if order is None:
        await render(message, t("error.not_found", lang), build([[nav_button(t("btn.home", lang), "home")]]))
        return

    intent = await PaymentIntentRepository(session).active_for_order(order.id)
    if intent is None:
        await _render_status(message, session, order, lang)
        return

    payments = PaymentService(session)
    try:
        await payments.submit_payment(intent=intent, reference=reference, source="telegram")
    except AppError as exc:
        await render(
            message,
            f"⚠️ {exc.safe_message}",
            build(
                [
                    [button(t("btn.try_again", lang), PayCB(action="submit", oid=order_ref).pack())],
                    [button(t("btn.back", lang), PayCB(action="screen", oid=order_ref).pack())],
                ]
            ),
        )
        return

    log.info(
        "payment.reference_submitted",
        order=order.reference,
        user_id=str(user.id),
        reference_length=len(reference),
    )
    await render(
        message,
        payment_submitted_screen(intent, lang),
        payment_status_keyboard(lang, intent, order),
    )


@router.callback_query(PayCB.filter(F.action == "status"))
async def check_status(
    callback: CallbackQuery,
    callback_data: PayCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    """Customer-triggered status refresh.

    Runs a real verification pass so the answer is current rather than a cached
    status, then renders whichever screen matches the resulting state.
    """
    order = await _load_order(session, user, callback_data.oid)
    if order is None:
        await render(callback, t("error.not_found", lang), build([[nav_button(t("btn.home", lang), "home")]]))
        return

    await loading(callback, t("loading.checking_payment", lang))
    intent = await PaymentIntentRepository(session).latest_for_order(order.id)
    if intent is None:
        await show_payment_methods(callback, session, order, lang)
        return

    if intent.status.is_open and intent.status is not PaymentStatus.AWAITING_PAYMENT:
        payments = PaymentService(session)
        try:
            await payments.verify(intent, triggered_by="customer")
        except AppError as exc:
            log.info("payment.customer_check_failed", intent_id=str(intent.id), code=exc.code)

    await _render_status(callback, session, order, lang, intent=intent)


@router.callback_query(PayCB.filter(F.action == "new"))
async def new_payment(
    callback: CallbackQuery,
    callback_data: PayCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    """Start a fresh payment for an expired or failed attempt.

    A new intent is created rather than reviving the old one, so the expected
    amount and window are re-quoted honestly.
    """
    order = await _load_order(session, user, callback_data.oid)
    if order is None:
        await render(callback, t("error.not_found", lang), build([[nav_button(t("btn.home", lang), "home")]]))
        return
    if order.status.is_paid:
        await _render_status(callback, session, order, lang)
        return

    from app.domain.enums import OrderStatus
    from app.domain.orders.service import OrderService

    if order.status is not OrderStatus.PAYMENT_PENDING:
        # An expired order returns to PAYMENT_PENDING so a new intent can be
        # created; the transition table refuses anything illegitimate.
        try:
            await OrderService(session).transition(order, OrderStatus.PAYMENT_PENDING)
        except AppError as exc:
            log.info(
                "payment.reopen_rejected", order=order.reference, detail=exc.detail[:200]
            )
            await _render_status(callback, session, order, lang)
            return
    await show_payment_methods(callback, session, order, lang)


async def _render_status(
    event, session: AsyncSession, order: Order, lang: Language, intent=None
) -> None:
    intent = intent or await PaymentIntentRepository(session).latest_for_order(order.id)
    if intent is None:
        await show_payment_methods(event, session, order, lang)
        return
    await render(
        event,
        payment_status_screen(intent, lang),
        payment_status_keyboard(lang, intent, order),
    )


@router.callback_query(OrderCB.filter(F.action == "pay"))
async def pay_order(
    callback: CallbackQuery,
    callback_data: OrderCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    """Continue an existing payment, or choose a method if none is open."""
    order = await _load_order(session, user, callback_data.oid)
    if order is None:
        await render(callback, t("error.not_found", lang), build([[nav_button(t("btn.home", lang), "home")]]))
        return
    if order.status.is_paid:
        await _render_status(callback, session, order, lang)
        return

    intent = await PaymentIntentRepository(session).active_for_order(order.id)
    if intent is None:
        await show_payment_methods(callback, session, order, lang)
        return
    if intent.status is PaymentStatus.AWAITING_PAYMENT:
        await _render_payment_instructions(callback, intent, order, lang)
        return
    await _render_status(callback, session, order, lang, intent=intent)
