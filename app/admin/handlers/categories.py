"""Category management (section 60).

Categories are what the shop screen is built from, so the operator needs to
create and rename them without a redeploy. Everything here is soft: a category
is archived, never deleted, and archiving is refused while products still point
at it — orphaning products would silently remove them from the shop.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
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
from app.bot.services.formatting import DIVIDER, esc
from app.bot.services.screen import render
from app.bot.states import AdminFlow
from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.db.models.catalog import Category
from app.db.repositories.catalog import CategoryRepository
from app.domain.enums import AuditAction

log = get_logger(__name__)
router = Router(name="admin_categories")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

#: Emoji are not a closed set we can validate against, so we only bound the
#: length: Telegram renders whatever the operator sends.
_MAX_EMOJI_CHARS = 4


class FieldError(ValueError):
    """A value the operator entered is not acceptable for this field."""


@dataclass(frozen=True, slots=True)
class Field:
    """One editable category field."""

    key: str
    label: str
    prompt: str
    parse: Callable[[str], Any]
    show: Callable[[Category], str]


def slugify(name: str) -> str:
    """A URL-safe, ASCII slug. Non-Latin names fall back to a short uuid.

    The slug is only an internal stable key (it carries the unique constraint),
    so a Bengali-only category name must still produce something usable rather
    than an empty string.
    """
    normalised = unicodedata.normalize("NFKD", name)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    slug = slug[:56]
    return slug or f"cat-{uuid.uuid4().hex[:8]}"


async def _unique_slug(repository: CategoryRepository, name: str) -> str:
    """Append a counter until the slug is free. The column is UNIQUE."""
    base = slugify(name)
    candidate = base
    for suffix in range(2, 50):
        if await repository.get_by_slug(candidate) is None:
            return candidate
        candidate = f"{base[:56]}-{suffix}"
    return f"{base[:52]}-{uuid.uuid4().hex[:6]}"


def _name(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise FieldError("The name cannot be empty.")
    if len(value) > 128:
        raise FieldError("Keep the name under 128 characters.")
    return value


def _optional_name(raw: str) -> str | None:
    if raw.strip() in {"-", "none", "clear"}:
        return None
    return _name(raw)


def _description(raw: str) -> str | None:
    value = raw.strip()
    if value in {"-", "none", "clear"}:
        return None
    if len(value) > 500:
        raise FieldError("Keep the description under 500 characters.")
    return value or None


def _emoji(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise FieldError("Send one emoji.")
    if len(value) > _MAX_EMOJI_CHARS:
        raise FieldError("Send a single emoji, not text.")
    return value


def _sort(raw: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise FieldError("Send a whole number, for example 10.") from exc
    if not 0 <= value <= 9999:
        raise FieldError("Use a number between 0 and 9999.")
    return value


FIELDS: dict[str, Field] = {
    "name": Field(
        key="name_en",
        label="Name",
        prompt="Send the category name.",
        parse=_name,
        show=lambda c: c.name_en,
    ),
    "bn": Field(
        key="name_bn",
        label="Bengali name",
        prompt="Send the Bengali name, or <code>-</code> to clear it.",
        parse=_optional_name,
        show=lambda c: c.name_bn or "not set",
    ),
    "desc": Field(
        key="description",
        label="Description",
        prompt="Send a short description, or <code>-</code> to clear it.",
        parse=_description,
        show=lambda c: (c.description[:40] + "…") if c.description else "not set",
    ),
    "emoji": Field(
        key="emoji",
        label="Emoji",
        prompt="Send the emoji shown next to this category.",
        parse=_emoji,
        show=lambda c: c.emoji,
    ),
    "sort": Field(
        key="sort_priority",
        label="Sort priority",
        prompt="Send the sort priority. Higher appears first.",
        parse=_sort,
        show=lambda c: str(c.sort_priority),
    ),
}


@router.callback_query(AdminCB.filter(F.section == "categories"))
async def categories_section(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: AdminContext,
    state: FSMContext,
) -> None:
    admin.require(Permissions.CATEGORIES_MANAGE)
    action = callback_data.action

    # ``arg`` uses '.' as its inner separator: ':' is aiogram's callback-data
    # separator and is rejected by AdminCB.
    if action == "view":
        await _detail(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "new":
        await _prompt_new(callback, state)
    elif action == "field":
        category_hex, _, field_key = callback_data.arg.partition(".")
        await _prompt_field(callback, session, admin, uuid.UUID(category_hex), field_key, state)
    elif action == "toggle":
        await _toggle_active(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "archive":
        await _archive(callback, session, admin, uuid.UUID(callback_data.arg))
    else:
        await _list(callback, session, admin)


async def _list(event, session: AsyncSession, admin: AdminContext) -> None:
    repository = CategoryRepository(session)
    categories = await repository.list_all()
    counts = await repository.assigned_product_counts()

    lines = ["📂 <b>CATEGORIES</b>", "", f"{len(categories)} category(ies)", DIVIDER]
    rows: list[list[Any]] = [[button("➕ New category", adm("categories", "new"))]]

    if not categories:
        lines += ["", "No categories yet. Create one to group products in the shop."]
    for category in categories:
        marker = "✅" if category.is_active else "⬜"
        count = counts.get(category.id, 0)
        lines.append(
            f"{marker} {category.emoji} <b>{esc(category.name_en)}</b> · {count} product(s)"
        )
        rows.append(
            [
                button(
                    f"{marker} {category.emoji} {category.name_en[:24]}",
                    adm("categories", "view", category.id.hex),
                )
            ]
        )

    rows.append(admin_back_row())
    await render(event, "\n".join(lines), build(rows))


async def _detail(
    event, session: AsyncSession, admin: AdminContext, category_id: uuid.UUID
) -> None:
    repository = CategoryRepository(session)
    category = await repository.get(category_id)
    if category is None or category.is_deleted:
        await render(event, "⚠️ Category not found.", build([admin_back_row("categories")]))
        return

    counts = await repository.assigned_product_counts()
    count = counts.get(category.id, 0)
    cid = category_id.hex

    lines = [
        f"{category.emoji} <b>{esc(category.name_en)}</b>",
        "",
        f"Slug: <code>{esc(category.slug)}</code>",
        f"Bengali: {esc(category.name_bn) if category.name_bn else '-'}",
        f"Visible in shop: {'yes' if category.is_active else 'no'}",
        f"Sort priority: {category.sort_priority}",
        f"Products: {count}",
    ]
    if category.description:
        lines += ["", esc(category.description)]

    rows = [
        [
            button(
                f"{field.label}: {field.show(category)[:20]}",
                adm("categories", "field", f"{cid}.{key}"),
            )
        ]
        for key, field in FIELDS.items()
    ]
    rows.append(
        [
            button(
                "⏸ Hide from shop" if category.is_active else "▶️ Show in shop",
                adm("categories", "toggle", cid),
            )
        ]
    )
    if count == 0:
        rows.append([button("🗄 Archive", adm("categories", "archive", cid))])
    rows.append(admin_back_row("categories"))
    await render(event, "\n".join(lines), build(rows))


# -- create ----------------------------------------------------------------


async def _prompt_new(event, state: FSMContext) -> None:
    await state.set_state(AdminFlow.category_name)
    await render(
        event,
        "\n".join(
            [
                "➕ <b>NEW CATEGORY</b>",
                "",
                "Send the category name.",
                "",
                "You can add an emoji, a Bengali name and a description "
                "afterwards on the category screen.",
            ]
        ),
        build([[button("❌ Cancel", adm("categories"))]]),
    )


@router.message(AdminFlow.category_name, F.text)
async def receive_name(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    admin.require(Permissions.CATEGORIES_MANAGE)
    try:
        name = _name(message.text or "")
    except FieldError as exc:
        # Stay in the state so the operator can just send a better name.
        await render(
            message,
            f"⚠️ {esc(str(exc))}\n\nSend the name again, or cancel.",
            build([[button("❌ Cancel", adm("categories"))]]),
        )
        return

    await state.clear()
    repository = CategoryRepository(session)
    category = Category(
        slug=await _unique_slug(repository, name),
        name_en=name,
        is_active=True,
    )
    session.add(category)
    await session.flush()

    await audit(
        session,
        admin,
        AuditAction.CATEGORY_CREATED,
        target_type="category",
        target_id=category.id,
        details={"name": name, "slug": category.slug},
    )
    log.info("admin.category_created", category_id=str(category.id), slug=category.slug)
    await _detail(message, session, admin, category.id)


# -- edit ------------------------------------------------------------------


async def _prompt_field(
    event,
    session: AsyncSession,
    admin: AdminContext,
    category_id: uuid.UUID,
    field_key: str,
    state: FSMContext,
) -> None:
    field = FIELDS.get(field_key)
    if field is None:
        await _detail(event, session, admin, category_id)
        return

    category = await CategoryRepository(session).get(category_id)
    if category is None or category.is_deleted:
        await render(event, "⚠️ Category not found.", build([admin_back_row("categories")]))
        return

    await state.set_state(AdminFlow.category_edit_value)
    await state.update_data(category_id=category_id.hex, category_field=field_key)
    await render(
        event,
        "\n".join(
            [
                f"✏️ <b>{field.label.upper()}</b>",
                "",
                f"Category: {esc(category.name_en)}",
                f"Current: <code>{esc(field.show(category))}</code>",
                "",
                field.prompt,
            ]
        ),
        build([[button("❌ Cancel", adm("categories", "view", category_id.hex))]]),
    )


@router.message(AdminFlow.category_edit_value, F.text)
async def receive_field(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    admin.require(Permissions.CATEGORIES_MANAGE)
    data = await state.get_data()
    category_hex = data.get("category_id")
    field_key = data.get("category_field")

    if not category_hex or field_key not in FIELDS:
        await state.clear()
        await render(message, "⚠️ That edit expired.", build([admin_back_row("categories")]))
        return

    field = FIELDS[field_key]
    try:
        value = field.parse(message.text or "")
    except FieldError as exc:
        await render(
            message,
            f"⚠️ {esc(str(exc))}\n\nSend the value again, or cancel.",
            build([[button("❌ Cancel", adm("categories", "view", category_hex))]]),
        )
        return

    await state.clear()
    category_id = uuid.UUID(category_hex)
    category = await CategoryRepository(session).get(category_id)
    if category is None or category.is_deleted:
        await render(message, "⚠️ Category not found.", build([admin_back_row("categories")]))
        return

    previous = getattr(category, field.key)
    setattr(category, field.key, value)
    await session.flush()

    await audit(
        session,
        admin,
        AuditAction.CATEGORY_UPDATED,
        target_type="category",
        target_id=category_id,
        details={
            "field": field.key,
            "slug": category.slug,
            "from": str(previous)[:120],
            "to": str(value)[:120],
        },
    )
    await _detail(message, session, admin, category_id)


async def _toggle_active(
    event, session: AsyncSession, admin: AdminContext, category_id: uuid.UUID
) -> None:
    category = await CategoryRepository(session).get(category_id)
    if category is None or category.is_deleted:
        await render(event, "⚠️ Category not found.", build([admin_back_row("categories")]))
        return

    category.is_active = not category.is_active
    await session.flush()
    await audit(
        session,
        admin,
        AuditAction.CATEGORY_UPDATED,
        target_type="category",
        target_id=category_id,
        details={"field": "is_active", "to": category.is_active, "slug": category.slug},
    )
    await _detail(event, session, admin, category_id)


async def _archive(
    event, session: AsyncSession, admin: AdminContext, category_id: uuid.UUID
) -> None:
    """Soft-delete, and only once nothing points at it."""
    repository = CategoryRepository(session)
    category = await repository.get(category_id)
    if category is None or category.is_deleted:
        await render(event, "⚠️ Category not found.", build([admin_back_row("categories")]))
        return

    counts = await repository.assigned_product_counts()
    remaining = counts.get(category_id, 0)
    if remaining:
        await render(
            event,
            (
                f"⚠️ {remaining} product(s) still use this category.\n\n"
                "Move them to another category first, or hide this one from the "
                "shop instead of archiving it."
            ),
            build([[button("◀ Back", adm("categories", "view", category_id.hex))]]),
        )
        return

    category.deleted_at = utcnow()
    category.is_active = False
    await session.flush()
    await audit(
        session,
        admin,
        AuditAction.CATEGORY_ARCHIVED,
        target_type="category",
        target_id=category_id,
        details={"slug": category.slug, "name": category.name_en},
    )
    log.info("admin.category_archived", category_id=str(category_id), slug=category.slug)
    await _list(event, session, admin)
