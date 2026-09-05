"""Checkout, coupon and order confirmation screens (sections 15-17)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import CheckoutCB, unpack_uuid
from app.bot.keyboards.common import build, button, nav_button
from app.bot.keyboards.customer import (
    checkout_keyboard,
    coupon_invalid_keyboard,
    coupon_prompt_keyboard,
)
from app.bot.services.formatting import checkout_screen, coupon_applied
from app.bot.services.screen import loading, render
from app.bot.states import CheckoutFlow
from app.core.config import get_settings
from app.core.exceptions import AppError, CouponError
from app.core.logging import get_logger
from app.db.models.user import User
from app.db.repositories.catalog import ProductRepository
from app.domain.enums import Language, OrderStatus
from app.domain.orders.service import OrderService
from app.i18n import t

log = get_logger(__name__)
router = Router(name="checkout")

#: FSM keys for the in-progress checkout.
KEY_PRODUCT = "checkout_product_id"
KEY_QUANTITY = "checkout_quantity"
KEY_COUPON = "checkout_coupon"


@router.callback_query(CheckoutCB.filter(F.action.in_({"open", "qty"})))
async def checkout_screen_handler(
    callback: CallbackQuery,
    callback_data: CheckoutCB,
    session: AsyncSession,
    user: User,
    lang: Language,
    state: FSMContext,
) -> None:
    await _render_checkout(
        callback, session, user, lang, state, callback_data.pid, callback_data.qty
    )


async def _render_checkout(
    event,
    session: AsyncSession,
    user: User,
    lang: Language,
    state: FSMContext,
    product_ref: str,
    quantity: int,
    *,
    confirm: bool = False,
) -> None:
    products = ProductRepository(session)
    product = await products.get_active(unpack_uuid(product_ref))
    if product is None:
        await render(
            event,
            t("error.not_found", lang),
            build([[nav_button(t("btn.back", lang), "shop")]]),
        )
        return

    data = await state.get_data()
    coupon_code = data.get(KEY_COUPON)
    quantity = max(product.min_quantity, min(quantity, product.max_quantity or quantity))

    orders = OrderService(session)
    try:
        quote = await orders.quote(
            product=product, quantity=quantity, user=user, coupon_code=coupon_code
        )
    except CouponError:
        # A coupon that became invalid between screens is dropped silently and
        # the customer sees the undiscounted, still-correct total.
        await state.update_data({KEY_COUPON: None})
        quote = await orders.quote(product=product, quantity=quantity, user=user)
    except AppError as exc:
        await render(
            event,
            f"⚠️ {exc.safe_message}",
            build([[nav_button(t("btn.back", lang), "shop"), nav_button(t("btn.home", lang), "home")]]),
        )
        return

    await state.update_data(
        {
            KEY_PRODUCT: product_ref,
            KEY_QUANTITY: quantity,
            KEY_COUPON: quote.coupon_code,
        }
    )

    text = checkout_screen(
        product_name=product.display_name(lang.value),
        quantity=quantity,
        subtotal=quote.subtotal,
        discount=quote.discount,
        total=quote.total,
        currency=quote.currency,
        lang=lang,
        confirm=confirm,
    )
    await render(
        event,
        text,
        checkout_keyboard(
            lang,
            product.id,
            quantity,
            has_coupon=bool(quote.coupon_code),
            max_quantity=product.max_quantity,
            coupons_enabled=get_settings().features.coupons_enabled,
        ),
    )


@router.callback_query(CheckoutCB.filter(F.action == "coupon"))
async def coupon_prompt(
    callback: CallbackQuery, callback_data: CheckoutCB, lang: Language, state: FSMContext
) -> None:
    await state.set_state(CheckoutFlow.entering_coupon)
    await state.update_data({KEY_PRODUCT: callback_data.pid, KEY_QUANTITY: callback_data.qty})
    text = "\n".join([t("coupon.title", lang), "", t("coupon.prompt", lang)])
    await render(
        callback, text, coupon_prompt_keyboard(lang, unpack_uuid(callback_data.pid), callback_data.qty)
    )


@router.message(CheckoutFlow.entering_coupon, F.text)
async def coupon_submitted(
    message: Message, session: AsyncSession, user: User, lang: Language, state: FSMContext
) -> None:
    data = await state.get_data()
    product_ref = data.get(KEY_PRODUCT)
    quantity = int(data.get(KEY_QUANTITY, 1))
    code = (message.text or "").strip()[:32]
    await state.set_state(None)

    if not product_ref:
        await render(message, t("error.expired_session", lang), build([[nav_button(t("btn.shop", lang), "shop")]]))
        return

    products = ProductRepository(session)
    product = await products.get_active(unpack_uuid(product_ref))
    if product is None:
        await render(message, t("error.not_found", lang), build([[nav_button(t("btn.shop", lang), "shop")]]))
        return

    orders = OrderService(session)
    try:
        quote = await orders.quote(product=product, quantity=quantity, user=user, coupon_code=code)
    except CouponError as exc:
        # The customer is told the coupon is unusable, never why - the reason
        # would leak campaign configuration.
        log.info("coupon.rejected", code=code, detail=exc.detail[:200], user_id=str(user.id))
        text = "\n".join([t("coupon.invalid_title", lang), "", t("coupon.invalid_body", lang)])
        await render(message, text, coupon_invalid_keyboard(lang, product.id, quantity))
        return
    except AppError as exc:
        await render(message, f"⚠️ {exc.safe_message}", build([[nav_button(t("btn.shop", lang), "shop")]]))
        return

    await state.update_data({KEY_COUPON: quote.coupon_code})
    await render(
        message,
        coupon_applied(quote.discount, quote.total, quote.currency, lang),
        build(
            [
                [
                    button(
                        t("btn.continue_checkout", lang),
                        CheckoutCB(action="open", pid=product_ref, qty=quantity).pack(),
                    )
                ]
            ]
        ),
    )


@router.callback_query(CheckoutCB.filter(F.action == "clear_coupon"))
async def clear_coupon(
    callback: CallbackQuery,
    callback_data: CheckoutCB,
    session: AsyncSession,
    user: User,
    lang: Language,
    state: FSMContext,
) -> None:
    await state.update_data({KEY_COUPON: None})
    await _render_checkout(
        callback, session, user, lang, state, callback_data.pid, callback_data.qty
    )


@router.callback_query(CheckoutCB.filter(F.action == "confirm"))
async def confirm_order(
    callback: CallbackQuery,
    callback_data: CheckoutCB,
    session: AsyncSession,
    user: User,
    lang: Language,
    state: FSMContext,
) -> None:
    """Create the order transactionally, then hand off to payment method choice.

    The total written to the order is the same quote the customer just saw; it
    is recomputed here only to re-validate stock and coupon at commit time.
    """
    await loading(callback, t("loading.creating_order", lang))

    products = ProductRepository(session)
    product = await products.get_active(unpack_uuid(callback_data.pid))
    if product is None:
        await render(callback, t("error.not_found", lang), build([[nav_button(t("btn.shop", lang), "shop")]]))
        return

    data = await state.get_data()
    coupon_code = data.get(KEY_COUPON)
    quantity = callback_data.qty

    orders = OrderService(session)
    try:
        quote = await orders.quote(
            product=product, quantity=quantity, user=user, coupon_code=coupon_code
        )
        order = await orders.create_order(quote=quote, user=user, channel="telegram")
        await orders.transition(order, OrderStatus.PAYMENT_PENDING)
    except AppError as exc:
        log.info("checkout.failed", user_id=str(user.id), code=exc.code, detail=exc.detail[:200])
        await render(
            callback,
            f"⚠️ {exc.safe_message}",
            build(
                [
                    [button(t("btn.back", lang), CheckoutCB(action="open", pid=callback_data.pid, qty=quantity).pack())],
                    [nav_button(t("btn.shop", lang), "shop"), nav_button(t("btn.home", lang), "home")],
                ]
            ),
        )
        return

    await state.clear()
    log.info("checkout.order_created", order=order.reference, user_id=str(user.id))

    # Straight into the payment method screen (section 18).
    from app.bot.handlers.payments import show_payment_methods

    await show_payment_methods(callback, session, order, lang)


@router.callback_query(CheckoutCB.filter(F.action == "cancel"))
async def cancel_checkout(callback: CallbackQuery, lang: Language, state: FSMContext) -> None:
    await state.clear()
    await render(
        callback,
        "Checkout cancelled.",
        build([[nav_button(t("btn.shop", lang), "shop"), nav_button(t("btn.home", lang), "home")]]),
    )
