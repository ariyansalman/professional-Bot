"""Product details screen and restock subscriptions (section 14)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import ProductCB, unpack_uuid
from app.bot.keyboards.common import build, nav_button
from app.bot.keyboards.customer import product_details_keyboard
from app.bot.services.formatting import product_details
from app.bot.services.screen import render
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.user import User
from app.db.repositories.catalog import ProductRepository
from app.db.repositories.users import RestockRepository
from app.domain.enums import Language
from app.domain.inventory.service import InventoryService
from app.i18n import t

log = get_logger(__name__)
router = Router(name="products")


@router.callback_query(ProductCB.filter(F.action == "view"))
async def product_view(
    callback: CallbackQuery, callback_data: ProductCB, session: AsyncSession, lang: Language
) -> None:
    products = ProductRepository(session)
    inventory = InventoryService(session)

    product = await products.get_active(unpack_uuid(callback_data.pid))
    if product is None:
        await render(
            callback,
            t("error.not_found", lang),
            build([[nav_button(t("btn.back", lang), "shop"), nav_button(t("btn.home", lang), "home")]]),
        )
        return

    await products.increment_views(product.id)
    status = await inventory.stock_status(product)
    back_to = "shop"
    photo = None
    if product.media:
        first = product.media[0]
        photo = first.file_id or first.url

    await render(
        callback,
        product_details(product, status, lang),
        product_details_keyboard(
            lang,
            product,
            status,
            back_to=back_to,
            restock_enabled=get_settings().features.restock_notifications_enabled,
        ),
        photo=photo,
    )


@router.callback_query(ProductCB.filter(F.action == "notify"))
async def notify_me(
    callback: CallbackQuery,
    callback_data: ProductCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    """Register for a restock alert. Idempotent per (user, product)."""
    if not get_settings().features.restock_notifications_enabled:
        await callback.answer(t("error.generic", lang), show_alert=True)
        return

    product_id = unpack_uuid(callback_data.pid)
    product = await ProductRepository(session).get_active(product_id)
    if product is None:
        await callback.answer(t("error.not_found", lang), show_alert=True)
        return

    await RestockRepository(session).subscribe(user.id, product_id)
    log.info("product.restock_subscribed", user_id=str(user.id), product_id=str(product_id))
    await callback.answer(t("success.subscribed", lang), show_alert=True)
