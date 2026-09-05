"""Admin catalog: products, inventory and coupons (60-62, 64)."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

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
from app.core.money import quantize_money
from app.core.timeutils import short_date
from app.db.models.catalog import Product
from app.db.repositories.catalog import (
    CategoryRepository,
    InventoryRepository,
    ProductRepository,
)
from app.db.repositories.orders import CouponRepository
from app.domain.enums import (
    AuditAction,
    CouponType,
    DeliveryType,
    ProductStatus,
    StockItemStatus,
)
from app.domain.inventory.service import InventoryService

log = get_logger(__name__)
router = Router(name="admin_catalog")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


# -- products --------------------------------------------------------------


@router.callback_query(AdminCB.filter(F.section == "products"))
async def products_section(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: AdminContext,
    state: FSMContext,
) -> None:
    admin.require(Permissions.PRODUCTS_VIEW)
    action = callback_data.action
    if action == "view":
        await _product_detail(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "toggle":
        await _toggle_status(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "archive":
        await _request_archive(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "new":
        await _start_wizard(callback, admin, state)
    elif action == "noop":
        await callback.answer()
    else:
        await _product_list(callback, session, admin, callback_data.arg, callback_data.page)


@router.callback_query(PageCB.filter(F.scope == "adm:products"))
async def products_page(
    callback: CallbackQuery, callback_data: PageCB, session: AsyncSession, admin: AdminContext
) -> None:
    admin.require(Permissions.PRODUCTS_VIEW)
    await _product_list(callback, session, admin, callback_data.arg, callback_data.page)


async def _product_list(
    event, session: AsyncSession, admin: AdminContext, filter_key: str, page: int
) -> None:
    status = None
    if filter_key in {s.value for s in ProductStatus}:
        status = ProductStatus(filter_key)

    result = await ProductRepository(session).list_for_admin(
        status=status, page=page, per_page=6
    )
    inventory = InventoryRepository(session)
    counts = await inventory.available_counts_for([p.id for p in result.items])

    lines = ["🛍 <b>PRODUCTS</b>", "", f"{result.total} product(s)", DIVIDER]
    rows = []
    chips = [
        button(f"• {label} •" if key == filter_key else label, adm("products", arg=key))
        for key, label in [
            ("", "All"),
            ("active", "Active"),
            ("draft", "Draft"),
            ("inactive", "Inactive"),
            ("archived", "Archived"),
        ]
    ]
    rows.extend([chips[i : i + 3] for i in range(0, len(chips), 3)])

    for product in result.items:
        stock = counts.get(product.id, 0)
        stock_label = "∞" if product.delivery_type is not DeliveryType.STOCK_ITEM else str(stock)
        lines += [
            "",
            f"<b>{esc(product.name)}</b> · {product.status.value}",
            f"{money(product.price, product.currency)} · stock {stock_label} · {product.sales_count} sold",
        ]
        rows.append([button(f"🛍 {product.name[:24]}", adm("products", "view", product.id.hex))])

    if admin.can(Permissions.PRODUCTS_MANAGE):
        rows.insert(0, [button("➕ Add product", adm("products", "new"))])

    if result.pages > 1:
        nav = []
        if result.has_prev:
            nav.append(button("◀", PageCB(scope="adm:products", page=result.page - 1, arg=filter_key).pack()))
        nav.append(button(result.label, adm("products", "noop")))
        if result.has_next:
            nav.append(button("▶", PageCB(scope="adm:products", page=result.page + 1, arg=filter_key).pack()))
        rows.append(nav)
    rows.append(admin_back_row())
    await render(event, "\n".join(lines), build(rows))


async def _product_detail(
    event, session: AsyncSession, admin: AdminContext, product_id: uuid.UUID
) -> None:
    products = ProductRepository(session)
    product = await products.get_with_media(product_id)
    if product is None:
        await render(event, "⚠️ Product not found.", build([admin_back_row("products")]))
        return

    inventory = InventoryService(session)
    counts = await inventory.counts(product.id)

    lines = [
        f"🛍 <b>{esc(product.name)}</b>",
        "",
        f"SKU: <code>{esc(product.sku)}</code>",
        f"Status: <b>{product.status.value}</b>",
        f"Price: {money(product.price, product.currency)}",
        f"Category: {esc(product.category.name_en) if product.category else '-'}",
        f"Delivery: {product.delivery_type.value}",
        "",
        DIVIDER,
        "<b>INVENTORY</b>",
        f"Available: {counts.get('available', 0)}",
        f"Reserved: {counts.get('reserved', 0)}",
        f"Sold: {counts.get('sold', 0)}",
        f"Invalid: {counts.get('invalid', 0)}",
        "",
        DIVIDER,
        "<b>VISIBILITY</b>",
        f"Customers: {'yes' if product.available_to_customers else 'no'}",
        f"Resellers: {'yes' if product.available_to_resellers else 'no'}",
        f"Featured: {'yes' if product.is_featured else 'no'}",
        "",
        f"Sales: {product.sales_count} · Views: {product.views_count}",
    ]
    if product.reseller_price:
        lines += [
            "",
            "<b>RESELLER PRICING</b>",
            f"Wholesale: {money(product.reseller_price, product.currency)}",
            f"Minimum: {money(product.reseller_min_price or 0, product.currency)}",
            f"Recommended: {money(product.reseller_recommended_price or 0, product.currency)}",
        ]

    rows = []
    if admin.can(Permissions.INVENTORY_MANAGE) and product.tracks_stock_items:
        rows.append([button("📦 Add stock", adm("inventory", "add", product.id.hex))])
    if admin.can(Permissions.PRODUCTS_MANAGE):
        toggle = "⏸ Deactivate" if product.status is ProductStatus.ACTIVE else "▶️ Activate"
        rows.append([button(toggle, adm("products", "toggle", product.id.hex))])
    if admin.can(Permissions.INVENTORY_VIEW):
        rows.append([button("📋 Inventory", adm("inventory", "view", product.id.hex))])
    if admin.can(Permissions.PRODUCTS_ARCHIVE) and product.status is not ProductStatus.ARCHIVED:
        rows.append([button("🗄 Archive", adm("products", "archive", product.id.hex))])
    rows.append(admin_back_row("products"))
    await render(event, "\n".join(lines), build(rows))


async def _toggle_status(
    event, session: AsyncSession, admin: AdminContext, product_id: uuid.UUID
) -> None:
    """Activate/deactivate. A draft can only go live once it is sellable."""
    admin.require(Permissions.PRODUCTS_MANAGE)
    products = ProductRepository(session)
    product = await products.get_with_media(product_id)
    if product is None:
        await render(event, "⚠️ Product not found.", build([admin_back_row("products")]))
        return

    if product.status is ProductStatus.ACTIVE:
        product.status = ProductStatus.INACTIVE
    else:
        problem = _publish_blocker(product)
        if problem:
            await render(
                event,
                f"⚠️ Cannot activate: {problem}",
                build([[button("◀ Back", adm("products", "view", product_id.hex))]]),
            )
            return
        product.status = ProductStatus.ACTIVE

    await session.flush()
    await audit(
        session,
        admin,
        AuditAction.PRODUCT_UPDATED,
        target_type="product",
        target_id=product_id,
        details={"status": product.status.value, "sku": product.sku},
    )
    await _product_detail(event, session, admin, product_id)


def _publish_blocker(product: Product) -> str | None:
    """Section 61: a partially configured product must not accept orders."""
    if product.price is None or product.price <= 0:
        return "price is not set"
    if not product.name:
        return "name is not set"
    if product.delivery_type is DeliveryType.STATIC_PAYLOAD and not product.delivery_payload:
        return "delivery payload is not configured"
    if product.delivery_type is DeliveryType.FILE and not product.delivery_file_id:
        return "delivery file is not configured"
    return None


async def _request_archive(
    event, session: AsyncSession, admin: AdminContext, product_id: uuid.UUID
) -> None:
    admin.require(Permissions.PRODUCTS_ARCHIVE)
    product = await ProductRepository(session).get_with_media(product_id)
    if product is None:
        await render(event, "⚠️ Product not found.", build([admin_back_row("products")]))
        return
    token = await create_confirmation(
        actor_id=admin.user.id,
        action="product_archive",
        payload={"target": str(product_id), "reason": f"archived by {admin.label}"},
    )
    await render(
        event,
        "\n".join(
            [
                "🗄 <b>ARCHIVE PRODUCT</b>",
                "",
                f"<b>{esc(product.name)}</b>",
                "",
                "The product is hidden from customers and resellers.",
                "Order history and inventory records are preserved.",
            ]
        ),
        confirm_keyboard(token, yes="✅ Archive"),
    )


@register("product_archive")
async def confirmed_archive(
    callback: CallbackQuery, session: AsyncSession, admin: AdminContext, payload: dict
) -> None:
    admin.require(Permissions.PRODUCTS_ARCHIVE)
    product_id = target_uuid(payload)
    products = ProductRepository(session)
    product = await products.get_with_media(product_id)
    if product is None:
        await render(callback, "⚠️ Product not found.", build([admin_back_row("products")]))
        return
    await products.archive(product)
    await audit(
        session,
        admin,
        AuditAction.PRODUCT_ARCHIVED,
        target_type="product",
        target_id=product_id,
        reason=payload.get("reason"),
        details={"sku": product.sku},
    )
    await render(
        callback,
        f"🗄 <b>{esc(product.name)}</b> archived.",
        build([admin_back_row("products")]),
    )


# -- product creation wizard (section 61) ----------------------------------


async def _start_wizard(event, admin: AdminContext, state: FSMContext) -> None:
    admin.require(Permissions.PRODUCTS_MANAGE)
    await state.set_state(AdminFlow.product_name)
    await state.update_data(new_product={})
    await render(
        event,
        "➕ <b>ADD PRODUCT</b>\n\nStep 1 of 4\n\nSend the product name.",
        build([[button("❌ Cancel", adm("products"))]]),
    )


@router.message(AdminFlow.product_name, F.text)
async def wizard_name(message: Message, admin: AdminContext, state: FSMContext) -> None:
    draft = (await state.get_data()).get("new_product", {})
    draft["name"] = (message.text or "").strip()[:160]
    await state.update_data(new_product=draft)
    await state.set_state(AdminFlow.product_price)
    await render(
        message,
        f"➕ <b>ADD PRODUCT</b>\n\nStep 2 of 4\n\nName: <b>{esc(draft['name'])}</b>\n\n"
        "Send the price (for example <code>15.00</code>).",
        build([[button("❌ Cancel", adm("products"))]]),
    )


@router.message(AdminFlow.product_price, F.text)
async def wizard_price(message: Message, admin: AdminContext, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        price = quantize_money(Decimal(raw))
    except (InvalidOperation, ValueError, ArithmeticError):
        await render(message, "⚠️ That is not a valid price. Send a number like 15.00.", None)
        return
    if price <= 0:
        await render(message, "⚠️ Price must be greater than zero.", None)
        return

    draft = (await state.get_data()).get("new_product", {})
    draft["price"] = str(price)
    await state.update_data(new_product=draft)
    await state.set_state(AdminFlow.product_description)
    await render(
        message,
        "➕ <b>ADD PRODUCT</b>\n\nStep 3 of 4\n\nSend a short description.",
        build([[button("❌ Cancel", adm("products"))]]),
    )


@router.message(AdminFlow.product_description, F.text)
async def wizard_description(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    draft = (await state.get_data()).get("new_product", {})
    draft["description"] = (message.text or "").strip()[:255]
    await state.update_data(new_product=draft)
    await state.set_state(AdminFlow.product_category)

    categories = await CategoryRepository(session).list_active()
    rows = [
        [button(f"{c.emoji} {c.name_en}", adm("products", "set_category", c.id.hex))]
        for c in categories
    ]
    rows.append([button("⏭ Skip", adm("products", "set_category", "none"))])
    rows.append([button("❌ Cancel", adm("products"))])
    await render(
        message,
        "➕ <b>ADD PRODUCT</b>\n\nStep 4 of 4\n\nChoose a category.",
        build(rows),
    )


@router.callback_query(AdminCB.filter((F.section == "products") & (F.action == "set_category")))
async def wizard_finish(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: AdminContext,
    state: FSMContext,
) -> None:
    """Create the product as a DRAFT.

    It is deliberately not activated: section 61 requires that a partially
    configured product cannot accidentally accept orders. The operator reviews
    it and activates it explicitly.
    """
    admin.require(Permissions.PRODUCTS_MANAGE)
    draft = (await state.get_data()).get("new_product", {})
    await state.clear()

    if not draft.get("name") or not draft.get("price"):
        await render(callback, "⚠️ That wizard expired.", build([admin_back_row("products")]))
        return

    products = ProductRepository(session)
    sku = f"SKU-{uuid.uuid4().hex[:8].upper()}"
    category_id = None if callback_data.arg == "none" else uuid.UUID(callback_data.arg)

    product = Product(
        sku=sku,
        name=draft["name"],
        short_description=draft.get("description", ""),
        price=Decimal(draft["price"]),
        currency="USDT",
        status=ProductStatus.DRAFT,
        delivery_type=DeliveryType.STOCK_ITEM,
        category_id=category_id,
    )
    await products.add(product)
    await audit(
        session,
        admin,
        AuditAction.PRODUCT_CREATED,
        target_type="product",
        target_id=product.id,
        details={"sku": sku, "name": product.name, "price": str(product.price)},
    )

    await render(
        callback,
        "\n".join(
            [
                "✅ <b>PRODUCT CREATED</b>",
                "",
                f"<b>{esc(product.name)}</b>",
                f"SKU: <code>{esc(sku)}</code>",
                f"Price: {money(product.price, product.currency)}",
                "",
                "Status: <b>draft</b>",
                "",
                "Add stock and activate it when you are ready to sell.",
            ]
        ),
        build(
            [
                [button("📦 Add stock", adm("inventory", "add", product.id.hex))],
                [button("🛍 Open product", adm("products", "view", product.id.hex))],
                admin_back_row("products"),
            ]
        ),
    )


# -- inventory -------------------------------------------------------------


@router.callback_query(AdminCB.filter(F.section == "inventory"))
async def inventory_section(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: AdminContext,
    state: FSMContext,
) -> None:
    admin.require(Permissions.INVENTORY_VIEW)
    action = callback_data.action
    if action == "add":
        admin.require(Permissions.INVENTORY_MANAGE)
        await state.set_state(AdminFlow.adding_stock)
        await state.update_data(stock_product_id=callback_data.arg)
        await render(
            callback,
            "\n".join(
                [
                    "📦 <b>ADD STOCK</b>",
                    "",
                    "Send the stock items, one per line.",
                    "",
                    "Each line becomes one sellable unit and is encrypted at "
                    "rest. Duplicates of existing items are skipped.",
                ]
            ),
            build([[button("❌ Cancel", adm("products", "view", callback_data.arg))]]),
        )
    elif action == "view":
        await _inventory_detail(callback, session, admin, uuid.UUID(callback_data.arg), callback_data.page)
    else:
        await _inventory_overview(callback, session, admin)


async def _inventory_overview(event, session: AsyncSession, admin: AdminContext) -> None:
    low = await InventoryRepository(session).low_stock_products(limit=10)
    lines = ["📦 <b>INVENTORY</b>", "", "<b>LOW STOCK</b>", DIVIDER]
    if not low:
        lines.append("✅ No products are low on stock.")
    rows = []
    for product, count in low:
        lines.append(f"• {esc(product.name)} — <b>{count}</b> available")
        rows.append([button(f"📋 {product.name[:24]}", adm("inventory", "view", product.id.hex))])
    rows.append(admin_back_row())
    await render(event, "\n".join(lines), build(rows))


async def _inventory_detail(
    event, session: AsyncSession, admin: AdminContext, product_id: uuid.UUID, page: int
) -> None:
    products = ProductRepository(session)
    product = await products.get_with_media(product_id)
    if product is None:
        await render(event, "⚠️ Product not found.", build([admin_back_row("inventory")]))
        return

    inventory = InventoryRepository(session)
    counts = await inventory.counts_by_status(product_id)
    items = await inventory.list_for_product(product_id, page=page, per_page=8)

    lines = [
        f"📋 <b>INVENTORY — {esc(product.name)}</b>",
        "",
        f"Available: <b>{counts.get('available', 0)}</b>",
        f"Reserved: {counts.get('reserved', 0)}",
        f"Sold: {counts.get('sold', 0)}",
        f"Invalid: {counts.get('invalid', 0)}",
        "",
        DIVIDER,
    ]
    # Only the non-secret preview is ever displayed; the payload stays encrypted.
    for item in items.items:
        lines.append(
            f"• <code>{esc(item.preview)}</code> · {item.status.value} · {short_date(item.created_at)}"
        )
    if items.is_empty:
        lines.append("No stock items.")

    rows = []
    if admin.can(Permissions.INVENTORY_MANAGE):
        rows.append([button("➕ Add stock", adm("inventory", "add", product_id.hex))])
    rows.append([button("🛍 Product", adm("products", "view", product_id.hex))])
    rows.append(admin_back_row("inventory"))
    await render(event, "\n".join(lines), build(rows))


@router.message(AdminFlow.adding_stock, F.text)
async def add_stock(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    admin.require(Permissions.INVENTORY_MANAGE)
    data = await state.get_data()
    await state.clear()
    product_ref = data.get("stock_product_id")
    if not product_ref:
        await render(message, "⚠️ That flow expired.", build([admin_back_row("inventory")]))
        return

    products = ProductRepository(session)
    product = await products.get_with_media(uuid.UUID(product_ref))
    if product is None:
        await render(message, "⚠️ Product not found.", build([admin_back_row("inventory")]))
        return

    payloads = [line for line in (message.text or "").splitlines() if line.strip()]
    if not payloads:
        await render(message, "⚠️ No stock items found in that message.", None)
        return

    inventory = InventoryService(session)
    added, skipped = await inventory.add_stock(
        product=product, payloads=payloads, actor_id=admin.user.id, reason="admin panel"
    )
    await audit(
        session,
        admin,
        AuditAction.INVENTORY_ADDED,
        target_type="product",
        target_id=product.id,
        details={"added": added, "skipped_duplicates": skipped},
    )

    # The submitted secrets must not linger in the chat history.
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - deletion is best effort
        log.info("admin.stock_message_delete_failed")

    lines = [
        "✅ <b>STOCK ADDED</b>",
        "",
        f"Product: <b>{esc(product.name)}</b>",
        f"Added: <b>{added}</b>",
    ]
    if skipped:
        lines.append(f"Skipped duplicates: {skipped}")
    lines += ["", "<i>Your message was deleted to keep the keys out of the chat history.</i>"]

    await message.answer(
        "\n".join(lines),
        reply_markup=build(
            [
                [button("📋 Inventory", adm("inventory", "view", product.id.hex))],
                [button("🛍 Product", adm("products", "view", product.id.hex))],
                admin_back_row(),
            ]
        ),
    )


# -- coupons ---------------------------------------------------------------


@router.callback_query(AdminCB.filter(F.section == "coupons"))
async def coupons_section(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: AdminContext,
    state: FSMContext,
) -> None:
    admin.require(Permissions.COUPONS_VIEW)
    if callback_data.action == "new":
        admin.require(Permissions.COUPONS_MANAGE)
        await state.set_state(AdminFlow.coupon_code)
        await render(
            callback,
            "🎟 <b>CREATE COUPON</b>\n\nSend the coupon code (for example <code>WELCOME10</code>).",
            build([[button("❌ Cancel", adm("coupons"))]]),
        )
        return
    if callback_data.action == "toggle":
        admin.require(Permissions.COUPONS_MANAGE)
        coupon = await CouponRepository(session).get(uuid.UUID(callback_data.arg))
        if coupon is not None:
            coupon.is_active = not coupon.is_active
            await session.flush()
            await audit(
                session,
                admin,
                AuditAction.COUPON_UPDATED,
                target_type="coupon",
                target_id=coupon.id,
                details={"code": coupon.code, "is_active": coupon.is_active},
            )

    page = await CouponRepository(session).list_all(page=callback_data.page, per_page=8)
    lines = ["🎟 <b>COUPONS</b>", "", f"{page.total} coupon(s)", DIVIDER]
    if page.is_empty:
        lines += ["", "No coupons yet."]

    rows = []
    if admin.can(Permissions.COUPONS_MANAGE):
        rows.append([button("➕ Create coupon", adm("coupons", "new"))])
    for coupon in page.items:
        value = (
            f"{coupon.value}%"
            if coupon.coupon_type is CouponType.PERCENTAGE
            else money(coupon.value, coupon.currency)
        )
        limit = f"{coupon.redemptions_count}/{coupon.max_redemptions or '∞'}"
        state_icon = "🟢" if coupon.is_active else "⚪"
        lines.append(f"{state_icon} <code>{esc(coupon.code)}</code> · {value} · used {limit}")
        if admin.can(Permissions.COUPONS_MANAGE):
            rows.append(
                [button(f"{'⏸' if coupon.is_active else '▶️'} {coupon.code}", adm("coupons", "toggle", coupon.id.hex))]
            )
    rows.append(admin_back_row())
    await render(callback, "\n".join(lines), build(rows))


@router.message(AdminFlow.coupon_code, F.text)
async def coupon_code(message: Message, admin: AdminContext, state: FSMContext) -> None:
    admin.require(Permissions.COUPONS_MANAGE)
    code = (message.text or "").strip().upper()[:32]
    if not code.isalnum():
        await render(message, "⚠️ Use letters and numbers only.", None)
        return
    await state.update_data(coupon_code=code)
    await state.set_state(AdminFlow.coupon_value)
    await render(
        message,
        f"🎟 <b>CREATE COUPON</b>\n\nCode: <code>{esc(code)}</code>\n\n"
        "Send the discount as a percentage (<code>10%</code>) or a fixed amount (<code>5</code>).",
        build([[button("❌ Cancel", adm("coupons"))]]),
    )


@router.message(AdminFlow.coupon_value, F.text)
async def coupon_value(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    admin.require(Permissions.COUPONS_MANAGE)
    data = await state.get_data()
    await state.clear()
    code = data.get("coupon_code")
    raw = (message.text or "").strip()

    is_percentage = raw.endswith("%")
    try:
        value = Decimal(raw.rstrip("%").strip())
    except (InvalidOperation, ValueError):
        await render(message, "⚠️ That is not a valid discount.", build([admin_back_row("coupons")]))
        return
    if value <= 0 or (is_percentage and value > 100):
        await render(message, "⚠️ Discount is out of range.", build([admin_back_row("coupons")]))
        return

    coupons = CouponRepository(session)
    if await coupons.get_by_code(code) is not None:
        await render(
            message, f"⚠️ Coupon <code>{esc(code)}</code> already exists.", build([admin_back_row("coupons")])
        )
        return

    from app.db.models.order import Coupon

    coupon = Coupon(
        code=code,
        coupon_type=CouponType.PERCENTAGE if is_percentage else CouponType.FIXED,
        value=value,
        currency="USDT",
        is_active=True,
        created_by_id=admin.user.id,
    )
    await coupons.add(coupon)
    await audit(
        session,
        admin,
        AuditAction.COUPON_CREATED,
        target_type="coupon",
        target_id=coupon.id,
        details={"code": code, "type": coupon.coupon_type.value, "value": str(value)},
    )
    await render(
        message,
        "\n".join(
            [
                "✅ <b>COUPON CREATED</b>",
                "",
                f"Code: <code>{esc(code)}</code>",
                f"Discount: {value}{'%' if is_percentage else ' USDT'}",
                "",
                "It is active immediately.",
            ]
        ),
        build([[button("🎟 Coupons", adm("coupons"))], admin_back_row()]),
    )
