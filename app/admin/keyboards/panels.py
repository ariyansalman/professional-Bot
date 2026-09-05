"""Admin keyboards. Every button is gated on the operator's permissions."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.admin.permissions.rbac import Permissions
from app.admin.services.context import AdminContext
from app.bot.callbacks import AdminCB, ConfirmCB, PageCB
from app.bot.keyboards.common import build, button
from app.db.repositories.base import Page


def adm(section: str, action: str = "open", arg: str = "", page: int = 1) -> str:
    return AdminCB(section=section, action=action, arg=arg, page=page).pack()


def dashboard_button(text: str = "🛡 Dashboard") -> InlineKeyboardButton:
    return button(text, adm("dashboard"))


def admin_back_row(section: str = "dashboard", arg: str = "") -> list[InlineKeyboardButton]:
    return [button("◀ Back", adm(section, arg=arg)), dashboard_button()]


def dashboard_keyboard(context: AdminContext) -> InlineKeyboardMarkup:
    """Only sections the operator can actually use are shown."""
    rows: list[list[InlineKeyboardButton]] = []

    row: list[InlineKeyboardButton] = []
    if context.can(Permissions.ORDERS_VIEW):
        row.append(button("📦 Orders", adm("orders")))
    if context.can(Permissions.PAYMENTS_VIEW):
        row.append(button("💳 Payments", adm("payments")))
    if row:
        rows.append(row)

    row = []
    if context.can(Permissions.PRODUCTS_VIEW):
        row.append(button("🛍 Products", adm("products")))
    if context.can(Permissions.INVENTORY_VIEW):
        row.append(button("📦 Inventory", adm("inventory")))
    if row:
        rows.append(row)

    row = []
    if context.can(Permissions.USERS_VIEW):
        row.append(button("👥 Users", adm("users")))
    if context.can(Permissions.RESELLERS_VIEW):
        row.append(button("🔗 Resellers", adm("resellers")))
    if row:
        rows.append(row)

    row = []
    if context.can(Permissions.SUPPORT_VIEW):
        row.append(button("🎧 Support", adm("support")))
    if context.can(Permissions.COUPONS_VIEW):
        row.append(button("🎟 Coupons", adm("coupons")))
    if row:
        rows.append(row)

    row = []
    if context.can(Permissions.ANALYTICS_VIEW):
        row.append(button("📈 Analytics", adm("analytics")))
    if context.can(Permissions.PROVIDERS_VIEW):
        row.append(button("💠 Providers", adm("providers")))
    if row:
        rows.append(row)

    row = []
    if context.can(Permissions.RECONCILIATION_RESOLVE):
        row.append(button("🧮 Reconciliation", adm("reconciliation")))
    if context.can(Permissions.AUDIT_VIEW):
        row.append(button("🧾 Audit", adm("audit")))
    if row:
        rows.append(row)

    row = []
    if context.can(Permissions.BROADCAST_SEND):
        row.append(button("📣 Broadcast", adm("broadcast")))
    if context.can(Permissions.SETTINGS_MANAGE):
        row.append(button("⚙️ Settings", adm("settings")))
    if row:
        rows.append(row)

    rows.append([button("🔎 Search", adm("search"))])
    return build(rows)


def list_keyboard(
    *,
    section: str,
    page: Page,
    filters: list[tuple[str, str]] | None = None,
    active_filter: str = "",
    rows_from_items=None,
    back_section: str = "dashboard",
) -> InlineKeyboardMarkup:
    """Generic admin list: filter chips, item rows, pagination, back."""
    rows: list[list[InlineKeyboardButton]] = []
    if filters:
        chips = [
            button(f"• {label} •" if key == active_filter else label, adm(section, arg=key))
            for key, label in filters
        ]
        rows.extend([chips[i : i + 3] for i in range(0, len(chips), 3)])
    if rows_from_items is not None:
        rows.extend(rows_from_items(page.items))
    pagination = []
    if page.pages > 1:
        if page.has_prev:
            pagination.append(
                button("◀", PageCB(scope=f"adm_{section}", page=page.page - 1, arg=active_filter).pack())
            )
        pagination.append(button(page.label, adm(section, action="noop")))
        if page.has_next:
            pagination.append(
                button("▶", PageCB(scope=f"adm_{section}", page=page.page + 1, arg=active_filter).pack())
            )
    if pagination:
        rows.append(pagination)
    rows.append(admin_back_row(back_section))
    return build(rows)


def confirm_keyboard(token: str, *, yes: str = "✅ Confirm", no: str = "❌ Cancel") -> InlineKeyboardMarkup:
    return build(
        [
            [
                button(yes, ConfirmCB(token=token, decision="yes").pack()),
                button(no, ConfirmCB(token=token, decision="no").pack()),
            ]
        ]
    )


def payment_review_keyboard(context: AdminContext, intent_id: str) -> InlineKeyboardMarkup:
    """Section 59: recheck / approve / reject / audit, permission-gated."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    if context.can(Permissions.PAYMENTS_RECHECK):
        row.append(button("🔄 Recheck", adm("payments", "recheck", intent_id)))
    if context.can(Permissions.PAYMENTS_APPROVE):
        row.append(button("✅ Approve", adm("payments", "approve", intent_id)))
    if row:
        rows.append(row)
    row = []
    if context.can(Permissions.PAYMENTS_REJECT):
        row.append(button("❌ Reject", adm("payments", "reject", intent_id)))
    if context.can(Permissions.AUDIT_VIEW):
        row.append(button("📋 Audit", adm("payments", "audit", intent_id)))
    if row:
        rows.append(row)
    rows.append(admin_back_row("payments"))
    return build(rows)
