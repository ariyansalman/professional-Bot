"""Admin settings, maintenance mode and broadcast (71, 124)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.filters import IsAdmin
from app.admin.handlers.confirmations import register
from app.admin.keyboards.panels import adm, admin_back_row, confirm_keyboard
from app.admin.permissions.rbac import Permissions
from app.admin.services.context import AdminContext, audit, create_confirmation
from app.bot.callbacks import AdminCB
from app.bot.keyboards.common import build, button
from app.bot.middlewares.maintenance import MAINTENANCE_KEY, MAINTENANCE_MESSAGE_KEY
from app.bot.services.formatting import DIVIDER, esc
from app.bot.services.screen import render
from app.bot.states import AdminFlow
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.timeutils import humanize_datetime
from app.db.models.support import Broadcast
from app.db.repositories.support import BroadcastRepository, SettingsRepository
from app.domain.enums import AuditAction, BroadcastAudience, BroadcastStatus

log = get_logger(__name__)
router = Router(name="admin_system")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


@router.callback_query(AdminCB.filter(F.section == "settings"))
async def settings_section(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, admin: AdminContext
) -> None:
    admin.require(Permissions.SETTINGS_MANAGE)
    repo = SettingsRepository(session)

    if callback_data.action == "maintenance":
        await _request_maintenance_toggle(callback, session, admin)
        return

    settings = get_settings()
    stored = await repo.all_settings()
    maintenance = bool(stored.get(MAINTENANCE_KEY, False))

    lines = [
        "⚙️ <b>SETTINGS</b>",
        "",
        f"Environment: <b>{settings.environment}</b>",
        f"Maintenance mode: <b>{'ON' if maintenance else 'off'}</b>",
        "",
        DIVIDER,
        "<b>FEATURES</b>",
        f"Coupons: {'on' if settings.features.coupons_enabled else 'off'}",
        f"Referrals: {'on' if settings.features.referrals_enabled else 'off'}",
        f"Reseller programme: {'on' if settings.features.reseller_enabled else 'off'}",
        f"Reseller self-activation: {'on' if settings.features.reseller_self_activation else 'off'}",
        f"Support: {'on' if settings.features.support_enabled else 'off'}",
        f"Restock alerts: {'on' if settings.features.restock_notifications_enabled else 'off'}",
        "",
        DIVIDER,
        "<b>PAYMENTS</b>",
        f"Default window: {settings.payments.default_window_seconds}s",
        f"Reservation TTL: {settings.payments.reservation_ttl_seconds}s",
        f"Underpayment tolerance: {settings.payments.underpayment_tolerance}",
        f"Overpayment tolerance: {settings.payments.overpayment_tolerance}",
        f"Late payment grace: {settings.payments.late_payment_grace_seconds}s",
        "",
        "<i>Feature flags and payment policy are environment configuration. "
        "Change them in the deployment, not at runtime.</i>",
    ]

    rows = []
    if admin.can(Permissions.MAINTENANCE_TOGGLE):
        label = "▶️ Disable maintenance" if maintenance else "🔧 Enable maintenance"
        rows.append([button(label, adm("settings", "maintenance"))])
    rows.append(admin_back_row())
    await render(callback, "\n".join(lines), build(rows))


async def _request_maintenance_toggle(
    event, session: AsyncSession, admin: AdminContext
) -> None:
    admin.require(Permissions.MAINTENANCE_TOGGLE)
    current = bool(await SettingsRepository(session).get_value(MAINTENANCE_KEY, False))
    target = not current

    token = await create_confirmation(
        actor_id=admin.user.id, action="maintenance_toggle", payload={"enabled": target}
    )
    if target:
        body = [
            "🔧 <b>ENABLE MAINTENANCE MODE</b>",
            "",
            "Customers will see a maintenance notice and cannot shop.",
            "",
            "Staff keep full access, and the workers keep running: payments "
            "already in flight continue to be verified and delivered, so no "
            "active order is left in a broken state.",
        ]
    else:
        body = ["▶️ <b>DISABLE MAINTENANCE MODE</b>", "", "The store reopens to customers."]

    await render(event, "\n".join(body), confirm_keyboard(token, yes="✅ Confirm"))


@register("maintenance_toggle")
async def confirmed_maintenance(
    callback: CallbackQuery, session: AsyncSession, admin: AdminContext, payload: dict
) -> None:
    admin.require(Permissions.MAINTENANCE_TOGGLE)
    enabled = bool(payload.get("enabled"))
    repo = SettingsRepository(session)
    await repo.set_value(
        MAINTENANCE_KEY,
        enabled,
        description="Storefront maintenance mode",
        updated_by_id=admin.user.id,
    )
    await audit(
        session,
        admin,
        AuditAction.MAINTENANCE_TOGGLED,
        target_type="system",
        target_id=MAINTENANCE_KEY,
        details={"enabled": enabled},
    )
    log.warning("admin.maintenance_toggled", enabled=enabled, actor=str(admin.user.id))
    await render(
        callback,
        f"{'🔧 Maintenance mode is ON.' if enabled else '▶️ Maintenance mode is off.'}",
        build([[button("⚙️ Settings", adm("settings"))], admin_back_row()]),
    )


# -- broadcast (section 71) ------------------------------------------------


@router.callback_query(AdminCB.filter(F.section == "broadcast"))
async def broadcast_section(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: AdminContext,
    state: FSMContext,
) -> None:
    admin.require(Permissions.BROADCAST_SEND)
    action = callback_data.action

    if action == "compose":
        await state.set_state(AdminFlow.broadcast_message)
        await state.update_data(broadcast_audience=callback_data.arg or "all")
        await render(
            callback,
            "\n".join(
                [
                    "📣 <b>BROADCAST</b>",
                    "",
                    f"Audience: <b>{callback_data.arg or 'all'}</b>",
                    "",
                    "Send the message text. HTML formatting is supported.",
                ]
            ),
            build([[button("❌ Cancel", adm("broadcast"))]]),
        )
        return

    recent = await BroadcastRepository(session).list_recent(limit=5)
    lines = ["📣 <b>BROADCAST</b>", "", "Choose an audience.", ""]
    if recent:
        lines += [DIVIDER, "<b>RECENT</b>"]
        for broadcast in recent:
            lines.append(
                f"• {broadcast.status.value} · {broadcast.sent_count}/{broadcast.total_recipients} sent · "
                f"{humanize_datetime(broadcast.created_at)}"
            )

    rows = [
        [
            button("👥 All users", adm("broadcast", "compose", "all")),
            button("🟢 Active", adm("broadcast", "compose", "active")),
        ],
        [
            button("🔗 Resellers", adm("broadcast", "compose", "resellers")),
            button("🇧🇩 Bengali", adm("broadcast", "compose", "bn")),
        ],
        admin_back_row(),
    ]
    await render(callback, "\n".join(lines), build(rows))


@router.message(AdminFlow.broadcast_message, F.text)
async def broadcast_preview(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    """Preview and confirm before anything is queued (section 71)."""
    admin.require(Permissions.BROADCAST_SEND)
    data = await state.get_data()
    await state.clear()
    audience = data.get("broadcast_audience", "all")
    body = (message.text or "").strip()[:4000]

    if len(body) < 3:
        await render(message, "⚠️ The message is too short.", build([admin_back_row()]))
        return

    token = await create_confirmation(
        actor_id=admin.user.id,
        action="broadcast_send",
        payload={"audience": audience, "body": body},
        ttl=900,
    )
    await message.answer(
        "\n".join(
            [
                "📣 <b>BROADCAST PREVIEW</b>",
                "",
                f"Audience: <b>{esc(audience)}</b>",
                "",
                DIVIDER,
                body,
                DIVIDER,
                "",
                "Sending respects Telegram rate limits and runs in the "
                "background. It can be interrupted safely and resumes from "
                "where it stopped.",
            ]
        ),
        reply_markup=confirm_keyboard(token, yes="📣 Send"),
    )


@register("broadcast_send")
async def confirmed_broadcast(
    callback: CallbackQuery, session: AsyncSession, admin: AdminContext, payload: dict
) -> None:
    """Queue the broadcast. The worker performs the actual sending."""
    admin.require(Permissions.BROADCAST_SEND)
    audience_key = payload.get("audience", "all")
    language_filter = audience_key if audience_key in {"en", "bn"} else None
    try:
        audience = BroadcastAudience(audience_key)
    except ValueError:
        audience = BroadcastAudience.LANGUAGE if language_filter else BroadcastAudience.ALL

    broadcast = Broadcast(
        created_by_id=admin.user.id,
        audience=audience,
        audience_filter={"language": language_filter} if language_filter else {},
        body=payload.get("body", ""),
        status=BroadcastStatus.QUEUED,
    )
    session.add(broadcast)
    await session.flush()

    await audit(
        session,
        admin,
        AuditAction.BROADCAST_SENT,
        target_type="broadcast",
        target_id=broadcast.id,
        details={"audience": audience.value, "length": len(broadcast.body)},
    )
    log.info(
        "admin.broadcast_queued",
        broadcast_id=str(broadcast.id),
        audience=audience.value,
        actor=str(admin.user.id),
    )
    await render(
        callback,
        "\n".join(
            [
                "✅ <b>BROADCAST QUEUED</b>",
                "",
                f"Audience: <b>{esc(audience.value)}</b>",
                "",
                "The broadcast worker will start sending shortly. Progress is "
                "shown on the broadcast screen.",
            ]
        ),
        build([[button("📣 Broadcast", adm("broadcast"))], admin_back_row()]),
    )
