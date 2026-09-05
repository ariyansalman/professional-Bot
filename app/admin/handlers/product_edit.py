"""Product editing and media management (section 60).

Every editable field goes through one screen and one FSM state; the field being
edited is kept in FSM data rather than in a state per field, so adding a field
is a table entry rather than a new handler.

Validation lives in :data:`FIELDS`, so a bad price or an out-of-range quantity
is rejected before it can reach a product a customer might buy. Publishing
checks stay where they are: a product still cannot go live half-configured.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.filters import IsAdmin
from app.admin.keyboards.panels import adm, admin_back_row
from app.admin.permissions.rbac import Permissions
from app.admin.services.context import AdminContext, audit
from app.bot.callbacks import AdminCB
from app.bot.keyboards.common import build, button
from app.bot.services.formatting import DIVIDER, esc, money
from app.bot.services.screen import render
from app.bot.states import AdminFlow
from app.core.logging import get_logger
from app.core.money import quantize_money
from app.db.models.catalog import Product, ProductMedia
from app.db.repositories.catalog import CategoryRepository, ProductRepository
from app.domain.enums import AuditAction

log = get_logger(__name__)
router = Router(name="admin_product_edit")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


#: Two full uuids do not fit in Telegram's 64-byte callback payload alongside
#: the section and action, so a second entity is addressed by a short prefix of
#: its id and resolved against the rows actually on the screen. A prefix beats a
#: list index here: it still names the same row if the list shifted between the
#: screen being drawn and the button being tapped.
REF_LENGTH = 8


def ref(entity_id: uuid.UUID) -> str:
    return entity_id.hex[:REF_LENGTH]


def resolve(candidates: Sequence[Any], reference: str) -> Any | None:
    """The single candidate whose id starts with ``reference``, if unambiguous."""
    if not reference:
        return None
    matches = [c for c in candidates if c.id.hex.startswith(reference)]
    return matches[0] if len(matches) == 1 else None


class FieldError(ValueError):
    """A value the operator entered is not acceptable for this field."""


@dataclass(frozen=True, slots=True)
class Field:
    """One editable product field."""

    key: str
    label: str
    prompt: str
    parse: Callable[[str], Any]
    #: How the current value is rendered on the edit menu.
    show: Callable[[Product], str]
    multiline: bool = False


def _text(limit: int, *, allow_empty: bool = False) -> Callable[[str], Any]:
    def parse(raw: str) -> str | None:
        value = raw.strip()
        if not value:
            if allow_empty:
                return None
            raise FieldError("This field cannot be empty.")
        if len(value) > limit:
            raise FieldError(f"Too long — keep it under {limit} characters.")
        return value

    return parse


def _price(raw: str) -> Decimal:
    try:
        value = quantize_money(Decimal(raw.strip()))
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise FieldError("That is not a valid number. Send something like 15.00.") from exc
    if value <= 0:
        raise FieldError("Price must be greater than zero.")
    if value > Decimal("1000000"):
        raise FieldError("That price looks wrong — it exceeds 1,000,000.")
    return value


def _optional_price(raw: str) -> Decimal | None:
    if raw.strip() in {"-", "none", "clear"}:
        return None
    return _price(raw)


def _positive_int(maximum: int) -> Callable[[str], int]:
    def parse(raw: str) -> int:
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise FieldError("Send a whole number.") from exc
        if value < 1:
            raise FieldError("Must be at least 1.")
        if value > maximum:
            raise FieldError(f"Must not exceed {maximum}.")
        return value

    return parse


def _optional_int(maximum: int) -> Callable[[str], int | None]:
    def parse(raw: str) -> int | None:
        if raw.strip() in {"-", "none", "clear", "0"}:
            return None
        return _positive_int(maximum)(raw)

    return parse


def _lines(raw: str) -> list[str]:
    """A bullet list, one item per line. ``-`` clears it."""
    if raw.strip() in {"-", "none", "clear"}:
        return []
    items = [line.strip("•- ").strip() for line in raw.splitlines()]
    items = [item for item in items if item]
    if len(items) > 12:
        raise FieldError("At most 12 items.")
    return items


def _money_or_dash(value: Decimal | None, currency: str) -> str:
    return money(value, currency) if value is not None else "not set"


FIELDS: dict[str, Field] = {
    "name": Field(
        key="name",
        label="Name",
        prompt="Send the new product name.",
        parse=_text(160),
        show=lambda p: p.name,
    ),
    "short": Field(
        key="short_description",
        label="Short description",
        prompt="Send a one-line description shown on product cards.",
        parse=_text(255),
        show=lambda p: p.short_description or "not set",
    ),
    "full": Field(
        key="full_description",
        label="Full description",
        prompt="Send the full description shown on the product page.",
        parse=_text(3000, allow_empty=True),
        show=lambda p: (p.full_description[:60] + "…") if p.full_description else "not set",
        multiline=True,
    ),
    "price": Field(
        key="price",
        label="Price",
        prompt="Send the new price, for example <code>15.00</code>.",
        parse=_price,
        show=lambda p: money(p.price, p.currency),
    ),
    "compare": Field(
        key="compare_at_price",
        label="Compare-at price",
        prompt="Send the crossed-out reference price, or <code>-</code> to clear it.",
        parse=_optional_price,
        show=lambda p: _money_or_dash(p.compare_at_price, p.currency),
    ),
    "min_qty": Field(
        key="min_quantity",
        label="Minimum quantity",
        prompt="Send the minimum quantity per order.",
        parse=_positive_int(1000),
        show=lambda p: str(p.min_quantity),
    ),
    "max_qty": Field(
        key="max_quantity",
        label="Maximum quantity",
        prompt="Send the maximum quantity per order, or <code>-</code> for no limit.",
        parse=_optional_int(1000),
        show=lambda p: str(p.max_quantity) if p.max_quantity else "no limit",
    ),
    "low_stock": Field(
        key="low_stock_threshold",
        label="Low-stock threshold",
        prompt="Send the level at which this product is flagged as low on stock.",
        parse=_positive_int(10000),
        show=lambda p: str(p.low_stock_threshold),
    ),
    "features": Field(
        key="features",
        label="Features",
        prompt="Send the features, one per line. Send <code>-</code> to clear.",
        parse=_lines,
        show=lambda p: f"{len(p.features or [])} item(s)",
        multiline=True,
    ),
    "included": Field(
        key="included_items",
        label="What's included",
        prompt="Send what the buyer receives, one per line. <code>-</code> to clear.",
        parse=_lines,
        show=lambda p: f"{len(p.included_items or [])} item(s)",
        multiline=True,
    ),
    "reqs": Field(
        key="requirements",
        label="Requirements",
        prompt="Send the requirements, one per line. <code>-</code> to clear.",
        parse=_lines,
        show=lambda p: f"{len(p.requirements or [])} item(s)",
        multiline=True,
    ),
    "notes": Field(
        key="delivery_instructions",
        label="Delivery note",
        prompt="Send the delivery note shown to the buyer. <code>-</code> to clear.",
        parse=_text(1000, allow_empty=True),
        show=lambda p: (p.delivery_instructions[:50] + "…") if p.delivery_instructions else "not set",
        multiline=True,
    ),
    "payload": Field(
        key="delivery_payload",
        label="Static payload",
        prompt=(
            "Send the payload every buyer receives (a link, instructions). "
            "Used by static-payload products only."
        ),
        parse=_text(3000, allow_empty=True),
        show=lambda p: "configured" if p.delivery_payload else "not set",
        multiline=True,
    ),
    "res_price": Field(
        key="reseller_price",
        label="Reseller wholesale",
        prompt="Send the price resellers pay, or <code>-</code> to disable reseller sales.",
        parse=_optional_price,
        show=lambda p: _money_or_dash(p.reseller_price, p.currency),
    ),
    "res_min": Field(
        key="reseller_min_price",
        label="Reseller minimum",
        prompt="Send the lowest price a reseller may resell at, or <code>-</code>.",
        parse=_optional_price,
        show=lambda p: _money_or_dash(p.reseller_min_price, p.currency),
    ),
    "res_rec": Field(
        key="reseller_recommended_price",
        label="Reseller recommended",
        prompt="Send the recommended resale price, or <code>-</code>.",
        parse=_optional_price,
        show=lambda p: _money_or_dash(p.reseller_recommended_price, p.currency),
    ),
    "sort": Field(
        key="sort_priority",
        label="Sort priority",
        prompt="Send the sort priority. Higher appears first.",
        parse=lambda raw: _positive_int(9999)(raw) if raw.strip() not in {"0", "-"} else 0,
        show=lambda p: str(p.sort_priority),
    ),
}

#: Boolean flags toggled straight from the menu.
TOGGLES: dict[str, tuple[str, str]] = {
    "featured": ("is_featured", "⭐ Featured"),
    "best": ("is_best_seller", "🔥 Best seller"),
    "new": ("is_new_arrival", "🆕 New arrival"),
    "customers": ("available_to_customers", "👤 Sell to customers"),
    "resellers": ("available_to_resellers", "🔗 Sell to resellers"),
}


@router.callback_query(AdminCB.filter(F.section == "pedit"))
async def product_edit_section(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: AdminContext,
    state: FSMContext,
) -> None:
    admin.require(Permissions.PRODUCTS_MANAGE)
    action = callback_data.action

    # arg carries "<product hex>:<key>" for field actions, which is safe here
    # because AdminCB values are validated to exclude the ':' separator; we use
    # '.' instead.
    if action == "menu":
        await _menu(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "field":
        product_hex, _, field_key = callback_data.arg.partition(".")
        await _prompt(callback, session, admin, uuid.UUID(product_hex), field_key, state)
    elif action == "toggle":
        product_hex, _, toggle_key = callback_data.arg.partition(".")
        await _toggle(callback, session, admin, uuid.UUID(product_hex), toggle_key)
    elif action == "category":
        await _category_picker(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "setcat":
        product_hex, _, category_ref = callback_data.arg.partition(".")
        await _set_category(callback, session, admin, uuid.UUID(product_hex), category_ref)
    else:
        await _menu(callback, session, admin, uuid.UUID(callback_data.arg))


async def _menu(
    event, session: AsyncSession, admin: AdminContext, product_id: uuid.UUID
) -> None:
    """The edit menu: every field with its current value."""
    product = await ProductRepository(session).get_with_media(product_id)
    if product is None:
        await render(event, "⚠️ Product not found.", build([admin_back_row("products")]))
        return

    pid = product_id.hex
    lines = [
        f"✏️ <b>EDIT — {esc(product.name)}</b>",
        "",
        f"SKU: <code>{esc(product.sku)}</code> · {product.status.value}",
        f"Delivery: {product.delivery_type.value}",
        f"Category: {esc(product.category.name_en) if product.category else 'none'}",
        f"Media: {len(product.media)} image(s)",
        "",
        DIVIDER,
        "Tap a field to change it.",
    ]

    def field_button(key: str) -> Any:
        field = FIELDS[key]
        return button(
            f"{field.label}: {field.show(product)[:24]}",
            adm("pedit", "field", f"{pid}.{key}"),
        )

    rows = [
        [field_button("name")],
        [field_button("price"), field_button("compare")],
        [field_button("short")],
        [field_button("full")],
        [field_button("features"), field_button("included")],
        [field_button("reqs"), field_button("notes")],
        [field_button("min_qty"), field_button("max_qty")],
        [field_button("low_stock"), field_button("sort")],
    ]

    if product.delivery_type.value == "static_payload":
        rows.append([field_button("payload")])

    rows.append(
        [
            field_button("res_price"),
            field_button("res_min"),
        ]
    )
    rows.append([field_button("res_rec")])
    rows.append([button("📂 Change category", adm("pedit", "category", pid))])

    # Flags, shown with their current state so the button says what it will do.
    toggle_row: list[Any] = []
    for key, (attribute, label) in TOGGLES.items():
        state_icon = "✅" if getattr(product, attribute) else "⬜"
        toggle_row.append(
            button(f"{state_icon} {label}", adm("pedit", "toggle", f"{pid}.{key}"))
        )
        if len(toggle_row) == 2:
            rows.append(toggle_row)
            toggle_row = []
    if toggle_row:
        rows.append(toggle_row)

    rows.append([button("🖼 Media", adm("pmedia", "list", pid))])
    rows.append(
        [
            button("◀ Back", adm("products", "view", pid)),
            button("🛡 Dashboard", adm("dashboard")),
        ]
    )
    await render(event, "\n".join(lines), build(rows))


async def _prompt(
    event,
    session: AsyncSession,
    admin: AdminContext,
    product_id: uuid.UUID,
    field_key: str,
    state: FSMContext,
) -> None:
    field = FIELDS.get(field_key)
    if field is None:
        await _menu(event, session, admin, product_id)
        return

    product = await ProductRepository(session).get_with_media(product_id)
    if product is None:
        await render(event, "⚠️ Product not found.", build([admin_back_row("products")]))
        return

    await state.set_state(AdminFlow.product_edit_value)
    await state.update_data(edit_product=product_id.hex, edit_field=field_key)

    current = field.show(product)
    lines = [
        f"✏️ <b>{field.label.upper()}</b>",
        "",
        f"Product: {esc(product.name)}",
        f"Current: <code>{esc(current)}</code>",
        "",
        field.prompt,
    ]
    if field.multiline:
        lines.append("")
        lines.append("<i>Multiple lines are accepted.</i>")

    await render(
        event,
        "\n".join(lines),
        build([[button("❌ Cancel", adm("pedit", "menu", product_id.hex))]]),
    )


@router.message(AdminFlow.product_edit_value, F.text)
async def receive_value(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    admin.require(Permissions.PRODUCTS_MANAGE)
    data = await state.get_data()
    product_hex = data.get("edit_product")
    field_key = data.get("edit_field")

    if not product_hex or field_key not in FIELDS:
        await state.clear()
        await render(message, "⚠️ That edit expired.", build([admin_back_row("products")]))
        return

    field = FIELDS[field_key]
    try:
        value = field.parse(message.text or "")
    except FieldError as exc:
        # Stay in the state so the operator can simply try again.
        await render(
            message,
            f"⚠️ {esc(str(exc))}\n\nSend the value again, or cancel.",
            build([[button("❌ Cancel", adm("pedit", "menu", product_hex))]]),
        )
        return

    await state.clear()
    product_id = uuid.UUID(product_hex)
    products = ProductRepository(session)
    product = await products.get_with_media(product_id)
    if product is None:
        await render(message, "⚠️ Product not found.", build([admin_back_row("products")]))
        return

    previous = getattr(product, field.key)
    setattr(product, field.key, value)

    # Keep the quantity range coherent rather than saving a product that can
    # never be bought.
    if product.max_quantity is not None and product.max_quantity < product.min_quantity:
        product.max_quantity = product.min_quantity

    await session.flush()
    await audit(
        session,
        admin,
        AuditAction.PRODUCT_UPDATED,
        target_type="product",
        target_id=product_id,
        details={
            "field": field.key,
            "sku": product.sku,
            "from": str(previous)[:120],
            "to": str(value)[:120],
        },
    )
    log.info(
        "admin.product_field_updated",
        product_id=str(product_id),
        field=field.key,
        sku=product.sku,
    )
    await _menu(message, session, admin, product_id)


async def _toggle(
    event, session: AsyncSession, admin: AdminContext, product_id: uuid.UUID, toggle_key: str
) -> None:
    entry = TOGGLES.get(toggle_key)
    if entry is None:
        await _menu(event, session, admin, product_id)
        return
    attribute, _ = entry

    products = ProductRepository(session)
    product = await products.get_with_media(product_id)
    if product is None:
        await render(event, "⚠️ Product not found.", build([admin_back_row("products")]))
        return

    new_value = not getattr(product, attribute)
    setattr(product, attribute, new_value)

    # Offering a product to resellers without a wholesale price would let them
    # buy at the retail price, which is never what the operator meant.
    if attribute == "available_to_resellers" and new_value and product.reseller_price is None:
        setattr(product, attribute, False)
        await render(
            event,
            "⚠️ Set a reseller wholesale price before enabling reseller sales.",
            build([[button("◀ Back", adm("pedit", "menu", product_id.hex))]]),
        )
        return

    await session.flush()
    await audit(
        session,
        admin,
        AuditAction.PRODUCT_UPDATED,
        target_type="product",
        target_id=product_id,
        details={"field": attribute, "to": new_value, "sku": product.sku},
    )
    await _menu(event, session, admin, product_id)


async def _category_picker(
    event, session: AsyncSession, admin: AdminContext, product_id: uuid.UUID
) -> None:
    categories = await CategoryRepository(session).list_active()
    rows = [
        [
            button(
                f"{c.emoji} {c.name_en}",
                adm("pedit", "setcat", f"{product_id.hex}.{ref(c.id)}"),
            )
        ]
        for c in categories
    ]
    rows.append([button("🚫 No category", adm("pedit", "setcat", f"{product_id.hex}.none"))])
    rows.append([button("◀ Back", adm("pedit", "menu", product_id.hex))])

    text = "📂 <b>CHANGE CATEGORY</b>\n\nChoose a category for this product."
    if not categories:
        text = (
            "📂 <b>CHANGE CATEGORY</b>\n\nThere are no active categories yet.\n"
            "Create one under Categories first."
        )
    await render(event, text, build(rows))


async def _set_category(
    event, session: AsyncSession, admin: AdminContext, product_id: uuid.UUID, category_ref: str
) -> None:
    products = ProductRepository(session)
    product = await products.get_with_media(product_id)
    if product is None:
        await render(event, "⚠️ Product not found.", build([admin_back_row("products")]))
        return

    if category_ref == "none":
        product.category_id = None
    else:
        category = resolve(await CategoryRepository(session).list_active(), category_ref)
        if category is None:
            # The category was renamed away or archived while the picker was open.
            await _category_picker(event, session, admin, product_id)
            return
        product.category_id = category.id

    await session.flush()
    await audit(
        session,
        admin,
        AuditAction.PRODUCT_UPDATED,
        target_type="product",
        target_id=product_id,
        details={
            "field": "category_id",
            "to": str(product.category_id),
            "sku": product.sku,
        },
    )
    await _menu(event, session, admin, product_id)


# --- media ----------------------------------------------------------------


@router.callback_query(AdminCB.filter(F.section == "pmedia"))
async def media_section(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: AdminContext,
    state: FSMContext,
) -> None:
    admin.require(Permissions.PRODUCTS_MANAGE)
    action = callback_data.action

    if action == "add":
        await _prompt_media(callback, session, admin, uuid.UUID(callback_data.arg), state)
    elif action == "remove":
        product_hex, _, media_ref = callback_data.arg.partition(".")
        await _remove_media(callback, session, admin, uuid.UUID(product_hex), media_ref)
    elif action == "primary":
        product_hex, _, media_ref = callback_data.arg.partition(".")
        await _make_primary(callback, session, admin, uuid.UUID(product_hex), media_ref)
    else:
        await _media_list(callback, session, admin, uuid.UUID(callback_data.arg))


async def _media_list(
    event, session: AsyncSession, admin: AdminContext, product_id: uuid.UUID
) -> None:
    product = await ProductRepository(session).get_with_media(product_id)
    if product is None:
        await render(event, "⚠️ Product not found.", build([admin_back_row("products")]))
        return

    pid = product_id.hex
    lines = [
        f"🖼 <b>MEDIA — {esc(product.name)}</b>",
        "",
        f"{len(product.media)} image(s).",
    ]
    if not product.media:
        lines += [
            "",
            "No images yet. The first image is shown on the product page.",
        ]
    else:
        lines += ["", DIVIDER]
        for index, media in enumerate(product.media, start=1):
            marker = " (primary)" if index == 1 else ""
            lines.append(f"{index}. {media.media_type}{marker}")

    rows = [[button("➕ Add image", adm("pmedia", "add", pid))]]
    for index, media in enumerate(product.media, start=1):
        row = [button(f"🗑 Remove #{index}", adm("pmedia", "remove", f"{pid}.{ref(media.id)}"))]
        if index != 1:
            row.append(
                button(
                    f"⭐ Make #{index} primary",
                    adm("pmedia", "primary", f"{pid}.{ref(media.id)}"),
                )
            )
        rows.append(row)
    rows.append([button("◀ Back", adm("pedit", "menu", pid))])
    await render(event, "\n".join(lines), build(rows))


async def _prompt_media(
    event, session: AsyncSession, admin: AdminContext, product_id: uuid.UUID, state: FSMContext
) -> None:
    await state.set_state(AdminFlow.product_media_upload)
    await state.update_data(media_product=product_id.hex)
    await render(
        event,
        "\n".join(
            [
                "🖼 <b>ADD IMAGE</b>",
                "",
                "Send a photo.",
                "",
                "It is stored as a Telegram file id, so no image hosting is "
                "needed and it is re-sent instantly on the product page.",
            ]
        ),
        build([[button("❌ Cancel", adm("pmedia", "list", product_id.hex))]]),
    )


@router.message(AdminFlow.product_media_upload, F.photo)
async def receive_photo(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    """Store the highest-resolution version Telegram offers."""
    admin.require(Permissions.PRODUCTS_MANAGE)
    data = await state.get_data()
    await state.clear()
    product_hex = data.get("media_product")
    if not product_hex or not message.photo:
        await render(message, "⚠️ That upload expired.", build([admin_back_row("products")]))
        return

    product_id = uuid.UUID(product_hex)
    products = ProductRepository(session)
    product = await products.get_with_media(product_id)
    if product is None:
        await render(message, "⚠️ Product not found.", build([admin_back_row("products")]))
        return

    # message.photo is ordered smallest to largest.
    largest = message.photo[-1]
    media = ProductMedia(
        product_id=product.id,
        file_id=largest.file_id,
        media_type="photo",
        sort_priority=len(product.media),
    )
    session.add(media)
    await session.flush()

    await audit(
        session,
        admin,
        AuditAction.PRODUCT_MEDIA_ADDED,
        target_type="product",
        target_id=product_id,
        details={"sku": product.sku, "media_type": "photo"},
    )
    log.info("admin.product_media_added", product_id=str(product_id), sku=product.sku)
    await _media_list(message, session, admin, product_id)


@router.message(AdminFlow.product_media_upload)
async def reject_non_photo(message: Message, state: FSMContext) -> None:
    """Anything that is not a photo is refused, with the state kept."""
    data = await state.get_data()
    product_hex = data.get("media_product", "")
    await render(
        message,
        "⚠️ Send a photo, not a file or text.\n\nCompressed photos work best.",
        build([[button("❌ Cancel", adm("pmedia", "list", product_hex))]]),
    )


async def _remove_media(
    event,
    session: AsyncSession,
    admin: AdminContext,
    product_id: uuid.UUID,
    media_ref: str,
) -> None:
    product = await ProductRepository(session).get_with_media(product_id)
    if product is None:
        await render(event, "⚠️ Product not found.", build([admin_back_row("products")]))
        return

    media = resolve(product.media, media_ref)
    if media is not None:
        await session.delete(media)
        await session.flush()
        await audit(
            session,
            admin,
            AuditAction.PRODUCT_MEDIA_REMOVED,
            target_type="product",
            target_id=product_id,
            details={"sku": product.sku},
        )
    await _media_list(event, session, admin, product_id)


async def _make_primary(
    event,
    session: AsyncSession,
    admin: AdminContext,
    product_id: uuid.UUID,
    media_ref: str,
) -> None:
    """Reorder so the chosen image is shown first on the product page."""
    product = await ProductRepository(session).get_with_media(product_id)
    if product is None:
        await render(event, "⚠️ Product not found.", build([admin_back_row("products")]))
        return

    chosen = resolve(product.media, media_ref)
    if chosen is None:
        await _media_list(event, session, admin, product_id)
        return

    ordered = [chosen]
    ordered += [m for m in product.media if m.id != chosen.id]
    for position, media in enumerate(ordered):
        media.sort_priority = position
    await session.flush()
    await _media_list(event, session, admin, product_id)
