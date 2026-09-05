"""Reseller center, activation and API key management (sections 45-48)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import Nav, ResellerCB, pack_uuid, unpack_uuid
from app.bot.keyboards.common import build, button, nav_button
from app.bot.keyboards.customer import reseller_center_keyboard
from app.bot.services.formatting import DIVIDER, esc, money
from app.bot.services.screen import render
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.security import mask_secret
from app.core.timeutils import short_date
from app.db.models.user import User
from app.db.repositories.resellers import ApiKeyRepository
from app.domain.enums import ApiScope, Language, ResellerStatus
from app.domain.resellers.service import SELF_SERVICE_SCOPES, TERMS_TEXT, ResellerService
from app.i18n import t

log = get_logger(__name__)
router = Router(name="reseller")

#: FSM-free scope selection: the chosen set is encoded in the callback arg as a
#: bitmask over SELF_SERVICE_SCOPES, which keeps it under the 64-byte limit.
def _mask_to_scopes(mask: int) -> list[ApiScope]:
    return [scope for index, scope in enumerate(SELF_SERVICE_SCOPES) if mask & (1 << index)]


def _scopes_to_mask(scopes: list[ApiScope]) -> int:
    mask = 0
    for index, scope in enumerate(SELF_SERVICE_SCOPES):
        if scope in scopes:
            mask |= 1 << index
    return mask


DEFAULT_MASK = _scopes_to_mask(
    [ApiScope.PRODUCTS_READ, ApiScope.ORDERS_CREATE, ApiScope.ORDERS_READ, ApiScope.PAYMENTS_READ]
)


def _docs_url() -> str | None:
    settings = get_settings()
    base = settings.telegram.webhook_base_url
    return f"{base.rstrip('/')}/api/v1/docs" if base else None


@router.callback_query(Nav.filter(F.to == "reseller"))
async def reseller_nav(
    callback: CallbackQuery, session: AsyncSession, user: User, lang: Language
) -> None:
    await _center(callback, session, user, lang)


@router.callback_query(ResellerCB.filter(F.action == "center"))
async def reseller_center(
    callback: CallbackQuery, session: AsyncSession, user: User, lang: Language
) -> None:
    await _center(callback, session, user, lang)


async def _center(event, session: AsyncSession, user: User, lang: Language) -> None:
    if not get_settings().features.reseller_enabled:
        await render(event, t("error.not_found", lang), build([[nav_button(t("btn.home", lang), "home")]]))
        return

    service = ResellerService(session)
    account = await service.get_account(user)
    active = account is not None and account.is_active

    lines = [t("reseller.center_title", lang), "", t("reseller.center_body", lang)]
    if account is not None:
        lines += ["", f"{t('reseller.status', lang)}: {_status_label(account.status)}"]
        if account.status is ResellerStatus.PENDING:
            lines.append("Your application is awaiting approval.")
        elif account.status is ResellerStatus.SUSPENDED:
            lines.append(f"Reason: {esc(account.suspended_reason or 'not specified')}")

    await render(event, "\n".join(lines), reseller_center_keyboard(lang, is_active=active, docs_url=_docs_url()))


def _status_label(status: ResellerStatus) -> str:
    return {
        ResellerStatus.PENDING: "🟡 Pending approval",
        ResellerStatus.ACTIVE: "🟢 Active",
        ResellerStatus.SUSPENDED: "🔴 Suspended",
        ResellerStatus.REVOKED: "⚫ Revoked",
    }[status]


@router.callback_query(ResellerCB.filter(F.action == "terms"))
async def show_terms(callback: CallbackQuery, lang: Language) -> None:
    await render(
        callback,
        "\n".join(["🚀 <b>BECOME A RESELLER</b>", "", TERMS_TEXT]),
        build(
            [
                [button(t("reseller.activate", lang), ResellerCB(action="accept").pack())],
                [button(t("btn.back", lang), ResellerCB(action="center").pack())],
            ]
        ),
    )


@router.callback_query(ResellerCB.filter(F.action == "accept"))
async def accept_terms(
    callback: CallbackQuery, session: AsyncSession, user: User, lang: Language
) -> None:
    service = ResellerService(session)
    try:
        account = await service.activate(user)
    except AppError as exc:
        await render(
            callback,
            f"⚠️ {exc.safe_message}",
            build([[button(t("btn.back", lang), ResellerCB(action="center").pack())]]),
        )
        return

    if account.status is ResellerStatus.PENDING:
        await render(
            callback,
            "🟡 <b>APPLICATION RECEIVED</b>\n\nYour reseller account is awaiting approval.\n"
            "We will notify you once it is reviewed.",
            build([[nav_button(t("btn.home", lang), "home")]]),
        )
        return

    await _dashboard(callback, session, user, lang)


@router.callback_query(ResellerCB.filter(F.action == "dashboard"))
async def dashboard(
    callback: CallbackQuery, session: AsyncSession, user: User, lang: Language
) -> None:
    await _dashboard(callback, session, user, lang)


async def _dashboard(event, session: AsyncSession, user: User, lang: Language) -> None:
    service = ResellerService(session)
    account = await service.get_account(user)
    if account is None or not account.is_active:
        await _center(event, session, user, lang)
        return

    stats = await service.dashboard_stats(account)
    health_icon = {"healthy": "🟢 Healthy", "degraded": "🟡 Degraded", "none": "⚪ Not configured"}[
        stats["webhook_health"]
    ]
    lines = [
        "🔗 <b>RESELLER DASHBOARD</b>",
        "",
        f"{t('reseller.status', lang)}:",
        _status_label(account.status),
        "",
        f"{t('reseller.api_requests', lang)}:",
        f"{stats['api_requests']:,}",
        "",
        "Orders:",
        f"{stats['orders']:,}",
        "",
        f"{t('reseller.sales', lang)}:",
        money(stats["sales_total"], stats["sales_currency"]),
        "",
        f"{t('reseller.webhooks', lang)}:",
        health_icon,
    ]
    if stats["webhook_failures"]:
        lines.append(f"⚠️ {stats['webhook_failures']} failed deliveries")

    rows = [
        [button(t("reseller.api_keys", lang), ResellerCB(action="keys").pack())],
        [button("🔔 Webhooks", ResellerCB(action="webhooks").pack())],
    ]
    docs = _docs_url()
    if docs:
        from aiogram.types import InlineKeyboardButton

        rows.append([InlineKeyboardButton(text=t("reseller.api_docs", lang), url=docs)])
    rows.append(
        [
            button(t("btn.back", lang), ResellerCB(action="center").pack()),
            nav_button(t("btn.home", lang), "home"),
        ]
    )
    await render(event, "\n".join(lines), build(rows))


@router.callback_query(ResellerCB.filter(F.action == "keys"))
async def api_keys(
    callback: CallbackQuery, session: AsyncSession, user: User, lang: Language
) -> None:
    await _keys(callback, session, user, lang)


async def _keys(event, session: AsyncSession, user: User, lang: Language) -> None:
    service = ResellerService(session)
    account = await service.get_account(user)
    if account is None or not account.is_active:
        await _center(event, session, user, lang)
        return

    keys = await ApiKeyRepository(session).list_for_reseller(account.id)
    active = [k for k in keys if not k.is_revoked]

    lines = ["🔑 <b>API KEYS</b>", ""]
    if not active:
        lines.append(t("reseller.no_keys", lang))
    else:
        lines.append(DIVIDER)
        for key in active:
            lines += [
                f"<b>{esc(key.name)}</b>",
                f"<code>{key.prefix}_{key.public_id}_{mask_secret('x' * 8)}</code>",
                f"Scopes: {', '.join(key.scopes) or 'none'}",
                f"Created: {short_date(key.created_at)}",
                f"Requests: {key.requests_count:,}",
                "",
            ]

    rows = [[button("➕ Create API Key", ResellerCB(action="new_key", arg=str(DEFAULT_MASK)).pack())]]
    for key in active:
        rows.append(
            [
                button(
                    f"🗑 Revoke {key.name[:20]}",
                    ResellerCB(action="revoke_key", arg=pack_uuid(key.id)).pack(),
                )
            ]
        )
    rows.append(
        [
            button(t("btn.back", lang), ResellerCB(action="dashboard").pack()),
            nav_button(t("btn.home", lang), "home"),
        ]
    )
    await render(event, "\n".join(lines), build(rows))


@router.callback_query(ResellerCB.filter(F.action == "new_key"))
async def new_key_scopes(
    callback: CallbackQuery, callback_data: ResellerCB, lang: Language
) -> None:
    """Scope picker (section 48). Toggles are encoded in the callback arg."""
    try:
        mask = int(callback_data.arg or DEFAULT_MASK)
    except ValueError:
        mask = DEFAULT_MASK

    rows = []
    for index, scope in enumerate(SELF_SERVICE_SCOPES):
        checked = bool(mask & (1 << index))
        rows.append(
            [
                button(
                    f"{'☑' if checked else '☐'} {scope.value}",
                    ResellerCB(action="new_key", arg=str(mask ^ (1 << index))).pack(),
                )
            ]
        )
    rows.append([button("✅ Create API Key", ResellerCB(action="create_key", arg=str(mask)).pack())])
    rows.append([button(t("btn.back", lang), ResellerCB(action="keys").pack())])

    lines = [
        "🔑 <b>CREATE API KEY</b>",
        "",
        "Select the scopes this key should have.",
        "",
        "<i>Administrative and financial scopes cannot be self-granted.</i>",
    ]
    await render(callback, "\n".join(lines), build(rows))


@router.callback_query(ResellerCB.filter(F.action == "create_key"))
async def create_key(
    callback: CallbackQuery,
    callback_data: ResellerCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    """Mint the key and show the plaintext exactly once."""
    service = ResellerService(session)
    account = await service.get_account(user)
    if account is None or not account.is_active:
        await _center(callback, session, user, lang)
        return

    try:
        mask = int(callback_data.arg or DEFAULT_MASK)
    except ValueError:
        mask = DEFAULT_MASK
    scopes = _mask_to_scopes(mask)
    if not scopes:
        await callback.answer("Select at least one scope.", show_alert=True)
        return

    try:
        created = await service.create_api_key(
            account=account, name=f"Key {account.api_requests_count + 1}", scopes=scopes
        )
    except AppError as exc:
        await render(
            callback,
            f"⚠️ {exc.safe_message}",
            build([[button(t("btn.back", lang), ResellerCB(action="keys").pack())]]),
        )
        return

    lines = [
        t("reseller.key_created", lang),
        "",
        "API Key:",
        f"<code>{esc(created.plaintext)}</code>",
        "",
        f"Scopes: {', '.join(s.value for s in scopes)}",
        "",
        t("reseller.store_securely", lang),
    ]
    await render(
        callback,
        "\n".join(lines),
        build([[button(t("btn.done", lang), ResellerCB(action="keys").pack())]]),
    )


@router.callback_query(ResellerCB.filter(F.action == "revoke_key"))
async def revoke_key(
    callback: CallbackQuery,
    callback_data: ResellerCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    """Revoking a key is destructive, so it is confirmed first (section 81)."""
    service = ResellerService(session)
    account = await service.get_account(user)
    if account is None:
        await _center(callback, session, user, lang)
        return

    await render(
        callback,
        "⚠️ <b>REVOKE API KEY</b>\n\nAny integration using this key will stop "
        "working immediately.\n\nThis cannot be undone.",
        build(
            [
                [
                    button(
                        "✅ Revoke",
                        ResellerCB(action="revoke_confirm", arg=callback_data.arg).pack(),
                    ),
                    button(t("btn.cancel", lang), ResellerCB(action="keys").pack()),
                ]
            ]
        ),
    )


@router.callback_query(ResellerCB.filter(F.action == "revoke_confirm"))
async def revoke_confirm(
    callback: CallbackQuery,
    callback_data: ResellerCB,
    session: AsyncSession,
    user: User,
    lang: Language,
) -> None:
    service = ResellerService(session)
    account = await service.get_account(user)
    if account is None:
        await _center(callback, session, user, lang)
        return
    try:
        await service.revoke_api_key(
            account=account, key_id=unpack_uuid(callback_data.arg), actor_id=user.id
        )
    except AppError as exc:
        await callback.answer(exc.safe_message, show_alert=True)
    await _keys(callback, session, user, lang)


@router.callback_query(ResellerCB.filter(F.action == "webhooks"))
async def webhooks(
    callback: CallbackQuery, session: AsyncSession, user: User, lang: Language
) -> None:
    service = ResellerService(session)
    account = await service.get_account(user)
    if account is None or not account.is_active:
        await _center(callback, session, user, lang)
        return

    endpoints = await service.webhooks.list_for_reseller(account.id)
    lines = ["🔔 <b>WEBHOOKS</b>", ""]
    if not endpoints:
        lines += [
            "No webhook endpoints configured.",
            "",
            "Register one through the API:",
            "<code>POST /api/v1/webhooks</code>",
            "",
            "You will receive order, payment and delivery events, each signed "
            "with your endpoint secret.",
        ]
    else:
        for endpoint in endpoints:
            icon = {"healthy": "🟢", "degraded": "🟡", "failing": "🔴", "disabled": "⚫"}[endpoint.health]
            lines += [
                f"{icon} <code>{esc(endpoint.url[:60])}</code>",
                f"Events: {', '.join(endpoint.events) if endpoint.events else 'all'}",
                f"Failures: {endpoint.consecutive_failures}",
                "",
            ]

    await render(
        callback,
        "\n".join(lines),
        build(
            [
                [button(t("btn.back", lang), ResellerCB(action="dashboard").pack())],
                [nav_button(t("btn.home", lang), "home")],
            ]
        ),
    )


@router.callback_query(ResellerCB.filter(F.action == "docs"))
async def api_docs(callback: CallbackQuery, lang: Language) -> None:
    lines = [
        "📚 <b>API DOCUMENTATION</b>",
        "",
        "<b>Base URL</b>",
        "<code>/api/v1</code>",
        "",
        "<b>Authentication</b>",
        "<code>Authorization: Bearer rt_live_...</code>",
        "",
        "<b>Endpoints</b>",
        "<code>GET  /products</code>",
        "<code>GET  /products/{id}</code>",
        "<code>POST /orders</code>",
        "<code>GET  /orders/{id}</code>",
        "<code>GET  /orders/{id}/delivery</code>",
        "<code>POST /webhooks</code>",
        "",
        "<b>Idempotency</b>",
        "Send <code>Idempotency-Key</code> on POST /orders. Replaying the same "
        "key returns the original order instead of creating a second one.",
        "",
        "<b>Webhooks</b>",
        "Every delivery is signed:",
        "<code>X-Signature: v1=hex(hmac_sha256(secret, ts.event_id.body))</code>",
        "",
        "Full OpenAPI schema is available at <code>/api/v1/docs</code>.",
    ]
    await render(
        callback,
        "\n".join(lines),
        build(
            [
                [button(t("btn.back", lang), ResellerCB(action="center").pack())],
                [nav_button(t("btn.home", lang), "home")],
            ]
        ),
    )
