"""Shop, category, listing and search screens (sections 11-13)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import Nav, PageCB, ShopCB, unpack_uuid
from app.bot.keyboards.common import build, nav_button
from app.bot.keyboards.customer import product_list_keyboard, shop_keyboard
from app.bot.services.formatting import DIVIDER, esc, product_card
from app.bot.services.screen import render
from app.bot.states import CheckoutFlow
from app.core.logging import get_logger
from app.db.repositories.base import Page
from app.db.repositories.catalog import CategoryRepository, ProductRepository
from app.domain.enums import Language
from app.domain.inventory.service import InventoryService
from app.i18n import t

log = get_logger(__name__)
router = Router(name="shop")

PER_PAGE = 4


@router.message(Command("shop"))
async def shop_command(
    message: Message, session: AsyncSession, lang: Language, state: FSMContext
) -> None:
    await state.clear()
    await _shop(message, session, lang)


@router.callback_query(Nav.filter(F.to == "shop"))
async def shop_nav(
    callback: CallbackQuery, session: AsyncSession, lang: Language, state: FSMContext
) -> None:
    await state.clear()
    await _shop(callback, session, lang)


@router.callback_query(ShopCB.filter(F.action == "categories"))
async def shop_categories(callback: CallbackQuery, session: AsyncSession, lang: Language) -> None:
    await _shop(callback, session, lang)


async def _shop(event, session: AsyncSession, lang: Language) -> None:
    categories = CategoryRepository(session)
    active = await categories.list_active()
    counts = await categories.product_counts()

    listed = [c for c in active if counts.get(c.id, 0) > 0]
    if not listed:
        # Empty state (section 80) rather than an empty keyboard.
        await render(
            event,
            t("shop.empty", lang),
            build([[nav_button(t("btn.home", lang), "home")]]),
        )
        return

    text = "\n".join([t("shop.title", lang), "", t("shop.choose_category", lang)])
    await render(event, text, shop_keyboard(lang, listed, counts))


@router.callback_query(ShopCB.filter(F.action == "category"))
async def category_screen(
    callback: CallbackQuery, callback_data: ShopCB, session: AsyncSession, lang: Language
) -> None:
    await _category(callback, session, lang, callback_data.ref, callback_data.page)


@router.callback_query(PageCB.filter(F.scope == "category"))
async def category_page(
    callback: CallbackQuery, callback_data: PageCB, session: AsyncSession, lang: Language
) -> None:
    await _category(callback, session, lang, callback_data.arg, callback_data.page)


async def _category(event, session: AsyncSession, lang: Language, ref: str, page: int) -> None:
    categories = CategoryRepository(session)
    products = ProductRepository(session)
    inventory = InventoryService(session)

    category = await categories.get(unpack_uuid(ref))
    if category is None:
        await render(event, t("error.not_found", lang), build([[nav_button(t("btn.back", lang), "shop")]]))
        return

    result = await products.list_by_category(category.id, page=page, per_page=PER_PAGE)
    if result.is_empty:
        await render(
            event,
            f"{category.emoji} <b>{esc(category.display_name(lang.value)).upper()}</b>\n\n"
            + t("shop.no_results", lang),
            build([[nav_button(t("btn.back", lang), "shop"), nav_button(t("btn.home", lang), "home")]]),
        )
        return

    stock = await inventory.stock_map(list(result.items))
    header = [
        f"{category.emoji} <b>{esc(category.display_name(lang.value)).upper()}</b>",
        "",
        f"{result.total} products",
        DIVIDER,
    ]
    body = [product_card(p, stock[p.id], lang) for p in result.items]
    await render(
        event,
        "\n".join([*header, "", "\n\n".join(body)]),
        product_list_keyboard(lang, result, stock, scope="category", scope_arg=ref),
    )


@router.callback_query(ShopCB.filter(F.action == "flag"))
async def flagged_products(
    callback: CallbackQuery, callback_data: ShopCB, session: AsyncSession, lang: Language
) -> None:
    await _flagged(callback, session, lang, callback_data.ref, callback_data.page)


@router.callback_query(PageCB.filter(F.scope == "flag"))
async def flagged_page(
    callback: CallbackQuery, callback_data: PageCB, session: AsyncSession, lang: Language
) -> None:
    await _flagged(callback, session, lang, callback_data.arg, callback_data.page)


async def _flagged(event, session: AsyncSession, lang: Language, flag: str, page: int) -> None:
    if flag not in {"best_sellers", "new_arrivals", "featured"}:
        flag = "best_sellers"
    products = ProductRepository(session)
    inventory = InventoryService(session)
    result = await products.list_flagged(flag, page=page, per_page=PER_PAGE)

    title = {
        "best_sellers": t("shop.best_sellers", lang),
        "new_arrivals": t("shop.new_arrivals", lang),
        "featured": t("home.featured", lang),
    }[flag]

    if result.is_empty:
        await render(
            event,
            f"<b>{title.upper()}</b>\n\n" + t("shop.no_results", lang),
            build([[nav_button(t("btn.back", lang), "shop"), nav_button(t("btn.home", lang), "home")]]),
        )
        return

    stock = await inventory.stock_map(list(result.items))
    body = [product_card(p, stock[p.id], lang) for p in result.items]
    await render(
        event,
        "\n".join([f"<b>{title.upper()}</b>", DIVIDER, "", "\n\n".join(body)]),
        product_list_keyboard(lang, result, stock, scope="flag", scope_arg=flag),
    )


@router.callback_query(ShopCB.filter(F.action == "search"))
async def search_prompt(callback: CallbackQuery, lang: Language, state: FSMContext) -> None:
    await state.set_state(CheckoutFlow.searching)
    await render(
        callback,
        t("shop.search_prompt", lang),
        build([[nav_button(t("btn.back", lang), "shop")]]),
    )


@router.message(CheckoutFlow.searching, F.text)
async def search_results(
    message: Message, session: AsyncSession, lang: Language, state: FSMContext
) -> None:
    await state.clear()
    query = (message.text or "").strip()[:64]
    await _search(message, session, lang, query, page=1)


@router.callback_query(PageCB.filter(F.scope == "search"))
async def search_page(
    callback: CallbackQuery, callback_data: PageCB, session: AsyncSession, lang: Language
) -> None:
    await _search(callback, session, lang, callback_data.arg, callback_data.page)


async def _search(event, session: AsyncSession, lang: Language, query: str, page: int) -> None:
    products = ProductRepository(session)
    inventory = InventoryService(session)
    result: Page = await products.search(query, page=page, per_page=PER_PAGE)

    if result.is_empty:
        await render(
            event,
            t("shop.no_results", lang),
            build(
                [
                    [nav_button(t("btn.back", lang), "shop")],
                    [nav_button(t("btn.home", lang), "home")],
                ]
            ),
        )
        return

    stock = await inventory.stock_map(list(result.items))
    body = [product_card(p, stock[p.id], lang) for p in result.items]
    header = f"🔎 <b>SEARCH</b>\n\n{result.total} result(s) for “{esc(query)}”\n{DIVIDER}"
    await render(
        event,
        "\n".join([header, "", "\n\n".join(body)]),
        product_list_keyboard(lang, result, stock, scope="search", scope_arg=query[:32]),
    )
