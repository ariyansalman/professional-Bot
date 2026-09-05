"""Admin payment provider, blockchain and credential management (66-69).

Credential handling rules enforced here:

* secrets are written straight to encrypted storage and never echoed back
* the message the operator typed is deleted so the secret leaves the chat
* changing a receiving address or a token contract is a high-risk action that
  requires an elevated permission, a confirmation and an audit entry
"""

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
from app.bot.callbacks import AdminCB
from app.bot.keyboards.common import build, button
from app.bot.services.formatting import DIVIDER, esc, money
from app.bot.services.screen import loading, render
from app.bot.states import AdminFlow
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.redis import redis_health
from app.core.security import get_secret_box, mask_address
from app.core.timeutils import humanize_datetime, utcnow
from app.db.repositories.payments import PaymentMethodRepository, PaymentProviderRepository
from app.domain.enums import AuditAction, PaymentProviderKind
from app.domain.payments.addresses import validate_address
from app.domain.payments.methods import (
    RATE_STALE_AFTER_HOURS,
    is_rate_stale,
    readiness_blocker,
)
from app.domain.payments.registry import build_adapter
from app.domain.payments.verification import quote_expected_amount

log = get_logger(__name__)
router = Router(name="admin_providers")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


@router.callback_query(AdminCB.filter(F.section == "providers"))
async def providers_section(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: AdminContext,
    state: FSMContext,
) -> None:
    admin.require(Permissions.PROVIDERS_VIEW)
    action = callback_data.action
    if action == "view":
        await _provider_detail(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "toggle":
        await _toggle_provider(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "test":
        await _test_connection(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "creds":
        await _prompt_credentials(callback, session, admin, uuid.UUID(callback_data.arg), state)
    elif action == "methods":
        await _method_list(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "health":
        await _health(callback, session, admin)
    else:
        await _provider_list(callback, session, admin)


async def _provider_list(event, session: AsyncSession, admin: AdminContext) -> None:
    providers = await PaymentProviderRepository(session).list_all()
    lines = ["💠 <b>PAYMENT PROVIDERS</b>", ""]

    exchange = [p for p in providers if p.kind is PaymentProviderKind.EXCHANGE]
    chains = [p for p in providers if p.kind is PaymentProviderKind.BLOCKCHAIN]
    rows = []

    def section(title: str, group: list) -> None:
        nonlocal lines
        if not group:
            return
        lines += [f"<b>{title}</b>", DIVIDER]
        for provider in group:
            state_icon = "🟢" if provider.is_enabled else "⚪"
            health = {
                "healthy": "❤️",
                "unhealthy": "💔",
                "unknown": "❔",
            }.get(provider.health_status, "❔")
            creds = "🔑" if provider.has_credentials else "⚠️"
            lines.append(f"{state_icon} {health} {creds} {esc(provider.display_name)}")
            rows.append(
                [button(f"{provider.display_name}", adm("providers", "view", provider.id.hex))]
            )
        lines.append("")

    section("EXCHANGE", exchange)
    section("BLOCKCHAIN", chains)

    if not providers:
        lines.append("No providers configured. Run the seed command to create them.")

    rows.append([button("❤️ Health", adm("providers", "health"))])
    rows.append(admin_back_row())
    await render(event, "\n".join(lines), build(rows))


async def _provider_detail(
    event, session: AsyncSession, admin: AdminContext, provider_id: uuid.UUID
) -> None:
    repo = PaymentProviderRepository(session)
    provider = await repo.get(provider_id)
    if provider is None:
        await render(event, "⚠️ Provider not found.", build([admin_back_row("providers")]))
        return

    methods = await PaymentMethodRepository(session).list_all()
    own = [m for m in methods if m.provider_id == provider.id]

    lines = [
        f"💠 <b>{esc(provider.display_name)}</b>",
        "",
        f"Code: <code>{provider.code.value}</code>",
        f"Kind: {provider.kind.value}",
        f"Enabled: {'yes' if provider.is_enabled else 'no'}",
        "",
        DIVIDER,
        "<b>CREDENTIALS</b>",
        f"API key: {'****' + provider.api_key_hint if provider.api_key_hint else 'not set'}",
        f"Secret: {'configured' if provider.encrypted_api_secret else 'not set'}",
        f"Passphrase: {'configured' if provider.encrypted_passphrase else 'not set'}",
        "",
        DIVIDER,
        "<b>HEALTH</b>",
        f"Status: {provider.health_status}",
        f"Checked: {humanize_datetime(provider.health_checked_at)}",
        f"Latency: {provider.health_latency_ms or 0} ms",
        f"Consecutive failures: {provider.consecutive_failures}",
        f"Last success: {humanize_datetime(provider.last_success_at)}",
    ]
    if provider.health_message and provider.health_status != "healthy":
        lines.append(f"Message: {esc(provider.health_message[:160])}")
    lines += ["", DIVIDER, f"<b>METHODS</b> ({len(own)})"]
    for method in own:
        lines.append(
            f"• {method.emoji} {esc(method.display_name)} — {'on' if method.is_enabled else 'off'}"
        )

    rows = []
    if admin.can(Permissions.PROVIDERS_CREDENTIALS):
        rows.append([button("🔐 Set credentials", adm("providers", "creds", provider.id.hex))])
    if admin.can(Permissions.PROVIDERS_VIEW):
        rows.append([button("🩺 Test connection", adm("providers", "test", provider.id.hex))])
    if admin.can(Permissions.PROVIDERS_MANAGE):
        toggle = "⏸ Disable" if provider.is_enabled else "▶️ Enable"
        rows.append([button(toggle, adm("providers", "toggle", provider.id.hex))])
    if own and admin.can(Permissions.PAYMENT_METHODS_MANAGE):
        rows.append([button("💳 Methods", adm("providers", "methods", provider.id.hex))])
    rows.append(admin_back_row("providers"))
    await render(event, "\n".join(lines), build(rows))


async def _toggle_provider(
    event, session: AsyncSession, admin: AdminContext, provider_id: uuid.UUID
) -> None:
    """Disabling a provider hides its methods; it never touches live payments."""
    admin.require(Permissions.PROVIDERS_MANAGE)
    repo = PaymentProviderRepository(session)
    provider = await repo.get(provider_id)
    if provider is None:
        await render(event, "⚠️ Provider not found.", build([admin_back_row("providers")]))
        return

    if not provider.is_enabled and provider.kind is PaymentProviderKind.EXCHANGE:
        if not provider.has_credentials:
            await render(
                event,
                "⚠️ Configure credentials before enabling this provider.",
                build([[button("◀ Back", adm("providers", "view", provider_id.hex))]]),
            )
            return

    provider.is_enabled = not provider.is_enabled
    await session.flush()
    await audit(
        session,
        admin,
        AuditAction.PROVIDER_TOGGLED,
        target_type="payment_provider",
        target_id=provider_id,
        details={"code": provider.code.value, "enabled": provider.is_enabled},
    )
    await _provider_detail(event, session, admin, provider_id)


async def _test_connection(
    event, session: AsyncSession, admin: AdminContext, provider_id: uuid.UUID
) -> None:
    """Live probe. Reports connectivity and authentication without secrets."""
    admin.require(Permissions.PROVIDERS_VIEW)
    if isinstance(event, CallbackQuery):
        await loading(event, "⏳ Testing provider connection...")

    repo = PaymentProviderRepository(session)
    provider = await repo.get(provider_id)
    if provider is None:
        await render(event, "⚠️ Provider not found.", build([admin_back_row("providers")]))
        return

    methods = await PaymentMethodRepository(session).list_all()
    method = next((m for m in methods if m.provider_id == provider.id), None)

    adapter = None
    try:
        adapter = build_adapter(provider, method)
        health = await adapter.health_check()
        await repo.record_health(
            provider,
            healthy=health.healthy,
            latency_ms=health.latency_ms,
            message=health.message,
        )
        icon = "✅" if health.healthy else "❌"
        lines = [
            f"{icon} <b>CONNECTION TEST</b>",
            "",
            f"Provider: {esc(provider.display_name)}",
            f"Result: <b>{health.message}</b>",
            f"Latency: {health.latency_ms} ms",
        ]
        if health.authenticated is not None:
            lines.append(f"Authenticated: {'yes' if health.authenticated else 'no'}")
        if health.details:
            safe = {k: v for k, v in health.details.items() if k != "detail"}
            if safe:
                lines.append(f"Details: {esc(str(safe)[:160])}")
        if not health.healthy and "detail" in health.details:
            # The technical reason is logged, and shown only to operators.
            log.warning(
                "admin.provider_test_failed",
                provider=provider.code.value,
                detail=str(health.details["detail"])[:300],
            )
            lines.append(f"<i>{esc(str(health.details['detail'])[:200])}</i>")
    except AppError as exc:
        lines = [
            "❌ <b>CONNECTION TEST</b>",
            "",
            f"Provider: {esc(provider.display_name)}",
            f"Result: {esc(exc.safe_message)}",
            "",
            f"<i>{esc(exc.detail[:200])}</i>",
        ]
    finally:
        if adapter is not None:
            await adapter.aclose()

    # Show the adapter's declared capabilities so operators know what this
    # integration can and cannot verify.
    if adapter is not None and hasattr(adapter, "capabilities"):
        caps = adapter.capabilities
        lines += ["", DIVIDER, "<b>CAPABILITIES</b>"]
        lines.append(f"Lookup by id: {'yes' if caps.lookup_by_id else 'no'}")
        lines.append(f"List recent: {'yes' if caps.list_recent else 'no'}")
        lines.append(f"Confirmations: {'yes' if caps.reports_confirmations else 'no'}")
        lines.append(f"Memo/tag: {'yes' if caps.reports_memo else 'no'}")
        for note in caps.notes:
            lines.append(f"• {esc(note)}")

    await render(
        event,
        "\n".join(lines),
        build([[button("◀ Back", adm("providers", "view", provider_id.hex))], admin_back_row()]),
    )


async def _prompt_credentials(
    event, session: AsyncSession, admin: AdminContext, provider_id: uuid.UUID, state: FSMContext
) -> None:
    admin.require(Permissions.PROVIDERS_CREDENTIALS)
    provider = await PaymentProviderRepository(session).get(provider_id)
    if provider is None:
        await render(event, "⚠️ Provider not found.", build([admin_back_row("providers")]))
        return

    await state.set_state(AdminFlow.provider_api_key)
    await state.update_data(provider_id=str(provider_id))
    await render(
        event,
        "\n".join(
            [
                "🔐 <b>PROVIDER CREDENTIALS</b>",
                "",
                f"Provider: <b>{esc(provider.display_name)}</b>",
                "",
                "Send the <b>API key</b>.",
                "",
                "⚠️ Use a <b>read-only</b> key. Withdrawal permission is never "
                "required and must not be granted.",
                "",
                "<i>Your message is deleted immediately after it is stored.</i>",
            ]
        ),
        build([[button("❌ Cancel", adm("providers", "view", provider_id.hex))]]),
    )


@router.message(AdminFlow.provider_api_key, F.text)
async def receive_api_key(message: Message, admin: AdminContext, state: FSMContext) -> None:
    admin.require(Permissions.PROVIDERS_CREDENTIALS)
    api_key = (message.text or "").strip()
    await _delete_secret_message(message)
    await state.update_data(pending_api_key=api_key)
    await state.set_state(AdminFlow.provider_api_secret)
    await message.answer(
        "🔐 Now send the <b>API secret</b>.",
        reply_markup=build([[button("❌ Cancel", adm("providers"))]]),
    )


@router.message(AdminFlow.provider_api_secret, F.text)
async def receive_api_secret(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    admin.require(Permissions.PROVIDERS_CREDENTIALS)
    secret = (message.text or "").strip()
    await _delete_secret_message(message)
    data = await state.get_data()
    provider_id = data.get("provider_id")

    provider = await PaymentProviderRepository(session).get(uuid.UUID(provider_id))
    if provider is None:
        await state.clear()
        await message.answer("⚠️ Provider not found.", reply_markup=build([admin_back_row()]))
        return

    # OKX additionally requires a passphrase.
    if provider.code.value == "okx":
        await state.update_data(pending_api_secret=secret)
        await state.set_state(AdminFlow.provider_passphrase)
        await message.answer(
            "🔐 Now send the <b>passphrase</b>.",
            reply_markup=build([[button("❌ Cancel", adm("providers"))]]),
        )
        return

    await _store_credentials(
        message, session, admin, state, provider, data.get("pending_api_key", ""), secret, None
    )


@router.message(AdminFlow.provider_passphrase, F.text)
async def receive_passphrase(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    admin.require(Permissions.PROVIDERS_CREDENTIALS)
    passphrase = (message.text or "").strip()
    await _delete_secret_message(message)
    data = await state.get_data()
    provider = await PaymentProviderRepository(session).get(uuid.UUID(data.get("provider_id")))
    if provider is None:
        await state.clear()
        await message.answer("⚠️ Provider not found.", reply_markup=build([admin_back_row()]))
        return
    await _store_credentials(
        message,
        session,
        admin,
        state,
        provider,
        data.get("pending_api_key", ""),
        data.get("pending_api_secret", ""),
        passphrase,
    )


async def _store_credentials(
    message: Message,
    session: AsyncSession,
    admin: AdminContext,
    state: FSMContext,
    provider,
    api_key: str,
    api_secret: str,
    passphrase: str | None,
) -> None:
    """Encrypt and store. The plaintext never touches the database or the log."""
    await state.clear()
    box = get_secret_box()
    provider.encrypted_api_key = box.encrypt(api_key)
    provider.encrypted_api_secret = box.encrypt(api_secret)
    provider.encrypted_passphrase = box.encrypt(passphrase) if passphrase else None
    provider.api_key_hint = api_key[-4:] if len(api_key) >= 4 else None
    await session.flush()

    await audit(
        session,
        admin,
        AuditAction.PROVIDER_CREDENTIALS_UPDATED,
        target_type="payment_provider",
        target_id=provider.id,
        details={"code": provider.code.value, "has_passphrase": bool(passphrase)},
    )
    log.info("admin.provider_credentials_updated", provider=provider.code.value)

    await message.answer(
        "\n".join(
            [
                "✅ <b>CREDENTIALS SAVED</b>",
                "",
                f"Provider: <b>{esc(provider.display_name)}</b>",
                f"API key: ****{provider.api_key_hint or ''}",
                "",
                "Test the connection before enabling the provider.",
            ]
        ),
        reply_markup=build(
            [
                [button("🩺 Test connection", adm("providers", "test", provider.id.hex))],
                [button("💠 Provider", adm("providers", "view", provider.id.hex))],
            ]
        ),
    )


async def _delete_secret_message(message: Message) -> None:
    """Remove a message containing a secret from the chat history."""
    try:
        await message.delete()
    except Exception:
        log.info("admin.secret_message_delete_failed")


# -- payment methods / blockchain configuration (section 67) ---------------


async def _method_list(
    event, session: AsyncSession, admin: AdminContext, provider_id: uuid.UUID
) -> None:
    admin.require(Permissions.PAYMENT_METHODS_MANAGE)
    methods = [
        m for m in await PaymentMethodRepository(session).list_all() if m.provider_id == provider_id
    ]
    lines = ["💳 <b>PAYMENT METHODS</b>", ""]
    rows = []
    for method in methods:
        icon = "🟢" if method.is_enabled else "⚪"
        lines += [
            f"{icon} {method.emoji} <b>{esc(method.display_name)}</b>",
            f"  {method.asset} · {method.network.value} · {method.required_confirmations} conf",
            f"  → <code>{esc(mask_address(method.receiving_address))}</code>",
        ]
        if method.token_contract:
            lines.append(f"  contract <code>{esc(mask_address(method.token_contract))}</code>")
        lines.append("")
        rows.append([button(f"{method.emoji} {method.display_name}", adm("method", "view", method.id.hex))])
    if not methods:
        lines.append("No methods configured for this provider.")
    rows.append([button("◀ Back", adm("providers", "view", provider_id.hex))])
    rows.append(admin_back_row())
    await render(event, "\n".join(lines), build(rows))


@router.callback_query(AdminCB.filter(F.section == "method"))
async def method_section(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: AdminContext,
    state: FSMContext,
) -> None:
    admin.require(Permissions.PAYMENT_METHODS_MANAGE)
    method_id = uuid.UUID(callback_data.arg)
    action = callback_data.action

    if action == "toggle":
        await _toggle_method(callback, session, admin, method_id)
    elif action == "address":
        await _prompt_address(callback, session, admin, method_id, state)
    elif action == "contract":
        await _prompt_contract(callback, session, admin, method_id, state)
    elif action == "rate":
        await _prompt_rate(callback, session, admin, method_id, state)
    else:
        await _method_detail(callback, session, admin, method_id)


async def _method_detail(
    event, session: AsyncSession, admin: AdminContext, method_id: uuid.UUID
) -> None:
    method = await PaymentMethodRepository(session).get(method_id)
    if method is None:
        await render(event, "⚠️ Method not found.", build([admin_back_row("providers")]))
        return

    lines = [
        f"{method.emoji} <b>{esc(method.display_name)}</b>",
        "",
        f"Code: <code>{esc(method.code)}</code>",
        f"Enabled: {'yes' if method.is_enabled else 'no'}",
        f"Asset: {esc(method.asset)} ({method.asset_decimals} decimals)",
        f"Network: {method.network.value}",
        "",
        DIVIDER,
        "<b>VERIFICATION</b>",
        f"Receiving address:\n<code>{esc(method.receiving_address or 'not set')}</code>",
    ]
    if method.token_contract:
        lines.append(f"Token contract:\n<code>{esc(method.token_contract)}</code>")
    else:
        lines.append("Token contract: not set (native asset)")
    lines += [
        f"Required confirmations: {method.required_confirmations}",
        f"Payment window: {method.payment_window_seconds}s",
        f"Requires memo: {'yes' if method.requires_memo else 'no'}",
        f"Quote rate: {method.quote_rate} per 1 {esc(method.asset)}"
        if method.quote_rate
        else "Quote rate: not set",
        f"Rate set: {humanize_datetime(method.quote_rate_updated_at)}"
        if method.quote_rate_updated_at
        else "Rate set: never",
    ]
    blocker = readiness_blocker(method)
    if blocker:
        lines += ["", f"⚠️ Not ready to enable: {blocker}"]
    elif is_rate_stale(method):
        lines += [
            "",
            f"⚠️ The {esc(method.asset)} rate was set over "
            f"{RATE_STALE_AFTER_HOURS}h ago. Every new order is priced from it.",
        ]
    if method.min_amount:
        lines.append(f"Min: {money(method.min_amount, method.asset)}")
    if method.max_amount:
        lines.append(f"Max: {money(method.max_amount, method.asset)}")

    rows = [
        [button("📍 Change address", adm("method", "address", method_id.hex))],
    ]
    if method.provider.kind is PaymentProviderKind.BLOCKCHAIN:
        rows.append([button("📜 Change token contract", adm("method", "contract", method_id.hex))])
    rows.append([button("💱 Set quote rate", adm("method", "rate", method_id.hex))])
    toggle = "⏸ Disable" if method.is_enabled else "▶️ Enable"
    rows.append([button(toggle, adm("method", "toggle", method_id.hex))])
    rows.append([button("◀ Back", adm("providers", "methods", method.provider_id.hex))])
    rows.append(admin_back_row())
    await render(event, "\n".join(lines), build(rows))


async def _toggle_method(
    event, session: AsyncSession, admin: AdminContext, method_id: uuid.UUID
) -> None:
    method = await PaymentMethodRepository(session).get(method_id)
    if method is None:
        await render(event, "⚠️ Method not found.", build([admin_back_row("providers")]))
        return
    if not method.is_enabled:
        blocker = readiness_blocker(method)
        if blocker:
            await render(
                event,
                f"⚠️ Cannot enable this method: {blocker}",
                build([[button("◀ Back", adm("method", "view", method_id.hex))]]),
            )
            return

    method.is_enabled = not method.is_enabled
    await session.flush()
    await audit(
        session,
        admin,
        AuditAction.PAYMENT_METHOD_UPDATED,
        target_type="payment_method",
        target_id=method_id,
        details={"code": method.code, "enabled": method.is_enabled},
    )
    await _method_detail(event, session, admin, method_id)


async def _prompt_address(
    event, session: AsyncSession, admin: AdminContext, method_id: uuid.UUID, state: FSMContext
) -> None:
    admin.require(Permissions.BLOCKCHAIN_MANAGE)
    await state.set_state(AdminFlow.method_address)
    await state.update_data(method_id=str(method_id))
    await render(
        event,
        "\n".join(
            [
                "📍 <b>CHANGE RECEIVING ADDRESS</b>",
                "",
                "⚠️ This determines where customer funds arrive.",
                "",
                "Send the new address.",
            ]
        ),
        build([[button("❌ Cancel", adm("method", "view", method_id.hex))]]),
    )


@router.message(AdminFlow.method_address, F.text)
async def receive_address(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    admin.require(Permissions.BLOCKCHAIN_MANAGE)
    data = await state.get_data()
    await state.clear()
    address = (message.text or "").strip()
    method_id = uuid.UUID(data.get("method_id"))

    method = await PaymentMethodRepository(session).get(method_id)
    if method is None:
        await message.answer("⚠️ Method not found.", reply_markup=build([admin_back_row()]))
        return

    problem = validate_address(address, method.network)
    if problem:
        await message.answer(
            f"⚠️ {problem}",
            reply_markup=build([[button("◀ Back", adm("method", "view", method_id.hex))]]),
        )
        return

    token = await create_confirmation(
        actor_id=admin.user.id,
        action="method_address",
        payload={"target": str(method_id), "address": address},
    )
    await message.answer(
        "\n".join(
            [
                "⚠️ <b>CONFIRM ADDRESS CHANGE</b>",
                "",
                f"Method: <b>{esc(method.display_name)}</b>",
                "",
                "Current:",
                f"<code>{esc(method.receiving_address or 'not set')}</code>",
                "",
                "New:",
                f"<code>{esc(address)}</code>",
                "",
                "All future payments for this method will be sent here.",
            ]
        ),
        reply_markup=confirm_keyboard(token, yes="✅ Change address"),
    )


@register("method_address")
async def confirmed_address(
    callback: CallbackQuery, session: AsyncSession, admin: AdminContext, payload: dict
) -> None:
    admin.require(Permissions.BLOCKCHAIN_MANAGE)
    method_id = target_uuid(payload)
    method = await PaymentMethodRepository(session).get(method_id)
    if method is None:
        await render(callback, "⚠️ Method not found.", build([admin_back_row("providers")]))
        return

    previous = method.receiving_address
    method.receiving_address = payload.get("address")
    await session.flush()
    await audit(
        session,
        admin,
        AuditAction.PAYMENT_ADDRESS_CHANGED,
        target_type="payment_method",
        target_id=method_id,
        details={
            "code": method.code,
            "previous": mask_address(previous),
            "new": mask_address(method.receiving_address),
        },
    )
    log.warning(
        "admin.payment_address_changed",
        method=method.code,
        actor=str(admin.user.id),
    )
    await _method_detail(callback, session, admin, method_id)


async def _prompt_contract(
    event, session: AsyncSession, admin: AdminContext, method_id: uuid.UUID, state: FSMContext
) -> None:
    admin.require(Permissions.BLOCKCHAIN_MANAGE)
    await state.set_state(AdminFlow.method_contract)
    await state.update_data(method_id=str(method_id))
    await render(
        event,
        "\n".join(
            [
                "📜 <b>CHANGE TOKEN CONTRACT</b>",
                "",
                "⚠️ The contract is what proves a payment is real USDT and not "
                "a counterfeit token with the same symbol.",
                "",
                "Send the contract / mint / jetton master address.",
            ]
        ),
        build([[button("❌ Cancel", adm("method", "view", method_id.hex))]]),
    )


@router.message(AdminFlow.method_contract, F.text)
async def receive_contract(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    admin.require(Permissions.BLOCKCHAIN_MANAGE)
    data = await state.get_data()
    await state.clear()
    contract = (message.text or "").strip()
    method_id = uuid.UUID(data.get("method_id"))

    method = await PaymentMethodRepository(session).get(method_id)
    if method is None:
        await message.answer("⚠️ Method not found.", reply_markup=build([admin_back_row()]))
        return

    token = await create_confirmation(
        actor_id=admin.user.id,
        action="method_contract",
        payload={"target": str(method_id), "contract": contract},
    )
    await message.answer(
        "\n".join(
            [
                "⚠️ <b>CONFIRM CONTRACT CHANGE</b>",
                "",
                f"Method: <b>{esc(method.display_name)}</b>",
                "",
                "Current:",
                f"<code>{esc(method.token_contract or 'not set')}</code>",
                "",
                "New:",
                f"<code>{esc(contract)}</code>",
                "",
                "An incorrect contract will cause every payment to be rejected.",
            ]
        ),
        reply_markup=confirm_keyboard(token, yes="✅ Change contract"),
    )


@register("method_contract")
async def confirmed_contract(
    callback: CallbackQuery, session: AsyncSession, admin: AdminContext, payload: dict
) -> None:
    admin.require(Permissions.BLOCKCHAIN_MANAGE)
    method_id = target_uuid(payload)
    method = await PaymentMethodRepository(session).get(method_id)
    if method is None:
        await render(callback, "⚠️ Method not found.", build([admin_back_row("providers")]))
        return

    previous = method.token_contract
    method.token_contract = payload.get("contract")
    await session.flush()
    await audit(
        session,
        admin,
        AuditAction.TOKEN_CONTRACT_CHANGED,
        target_type="payment_method",
        target_id=method_id,
        details={"code": method.code, "previous": previous, "new": method.token_contract},
    )
    log.warning("admin.token_contract_changed", method=method.code, actor=str(admin.user.id))
    await _method_detail(callback, session, admin, method_id)


async def _prompt_rate(
    event, session: AsyncSession, admin: AdminContext, method_id: uuid.UUID, state: FSMContext
) -> None:
    """Capture the quote rate. Volatile assets are unusable without one."""
    admin.require(Permissions.PAYMENT_METHODS_MANAGE)
    method = await PaymentMethodRepository(session).get(method_id)
    if method is None:
        await render(event, "⚠️ Method not found.", build([admin_back_row("providers")]))
        return

    await state.set_state(AdminFlow.method_rate)
    await state.update_data(method_id=str(method_id))
    await render(
        event,
        "\n".join(
            [
                "💱 <b>SET QUOTE RATE</b>",
                "",
                f"Method: <b>{esc(method.display_name)}</b>",
                f"Current: <code>{method.quote_rate}</code>",
                "",
                f"Send how much <b>1 {esc(method.asset)}</b> is worth in the "
                "currency your products are priced in.",
                "",
                f"For a stablecoin priced in itself that is <code>1</code>. For "
                f"{esc(method.asset)} it is the live price, for example "
                "<code>64500.00</code>.",
                "",
                "⚠️ The rate is frozen into each payment when it is created, so "
                "changing it never re-prices an order already awaiting payment.",
            ]
        ),
        build([[button("❌ Cancel", adm("method", "view", method_id.hex))]]),
    )


@router.message(AdminFlow.method_rate, F.text)
async def receive_rate(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    admin.require(Permissions.PAYMENT_METHODS_MANAGE)
    data = await state.get_data()
    method_id = uuid.UUID(data.get("method_id"))
    method = await PaymentMethodRepository(session).get(method_id)
    if method is None:
        await state.clear()
        await message.answer("⚠️ Method not found.", reply_markup=build([admin_back_row()]))
        return

    raw = (message.text or "").strip().replace(",", "")
    try:
        rate = Decimal(raw)
    except (InvalidOperation, ValueError):
        # Stay in the state so a corrected number goes straight through.
        await message.answer(
            "⚠️ That is not a number. Send something like <code>64500.00</code>.",
            reply_markup=build([[button("❌ Cancel", adm("method", "view", method_id.hex))]]),
        )
        return
    if rate <= 0:
        await message.answer(
            "⚠️ The rate must be greater than zero.",
            reply_markup=build([[button("❌ Cancel", adm("method", "view", method_id.hex))]]),
        )
        return

    await state.clear()
    token = await create_confirmation(
        actor_id=admin.user.id,
        action="method_rate",
        payload={"target": str(method_id), "rate": str(rate)},
    )
    # Show what the change actually means in money rather than only the number.
    sample = quote_expected_amount(Decimal("100"), rate, method.asset_decimals)
    await message.answer(
        "\n".join(
            [
                "⚠️ <b>CONFIRM RATE CHANGE</b>",
                "",
                f"Method: <b>{esc(method.display_name)}</b>",
                f"Current: <code>{method.quote_rate}</code>",
                f"New: <code>{rate}</code>",
                "",
                f"A 100.00 order would ask for <b>{sample} {esc(method.asset)}</b>.",
                "",
                "This prices every payment created from now on.",
            ]
        ),
        reply_markup=confirm_keyboard(token, yes="✅ Set rate"),
    )


@register("method_rate")
async def confirmed_rate(
    callback: CallbackQuery, session: AsyncSession, admin: AdminContext, payload: dict
) -> None:
    admin.require(Permissions.PAYMENT_METHODS_MANAGE)
    method_id = target_uuid(payload)
    method = await PaymentMethodRepository(session).get(method_id)
    if method is None:
        await render(callback, "⚠️ Method not found.", build([admin_back_row("providers")]))
        return

    previous = method.quote_rate
    method.quote_rate = Decimal(str(payload.get("rate")))
    method.quote_rate_updated_at = utcnow()
    await session.flush()
    await audit(
        session,
        admin,
        AuditAction.PAYMENT_METHOD_UPDATED,
        target_type="payment_method",
        target_id=method_id,
        details={
            "code": method.code,
            "field": "quote_rate",
            "previous": str(previous),
            "new": str(method.quote_rate),
        },
    )
    log.warning(
        "admin.quote_rate_changed",
        method=method.code,
        previous=str(previous),
        new=str(method.quote_rate),
        actor=str(admin.user.id),
    )
    await _method_detail(callback, session, admin, method_id)


async def _health(event, session: AsyncSession, admin: AdminContext) -> None:
    """Provider + infrastructure health (section 69)."""
    admin.require(Permissions.PROVIDERS_VIEW)
    providers = await PaymentProviderRepository(session).list_all()
    redis_ok, redis_message = await redis_health()

    lines = ["❤️ <b>PAYMENT HEALTH</b>", "", "<b>PROVIDERS</b>", DIVIDER]
    for provider in providers:
        if not provider.is_enabled:
            lines.append(f"⚪ {esc(provider.display_name)} — disabled")
            continue
        icon = {"healthy": "🟢", "unhealthy": "🔴"}.get(provider.health_status, "❔")
        lines.append(
            f"{icon} {esc(provider.display_name)} — {provider.health_latency_ms or 0} ms · "
            f"{provider.consecutive_failures} failures"
        )
        if provider.health_checked_at:
            lines.append(f"   checked {humanize_datetime(provider.health_checked_at)}")

    lines += [
        "",
        "<b>INFRASTRUCTURE</b>",
        DIVIDER,
        f"{'🟢' if redis_ok else '🔴'} Redis — {esc(redis_message)}",
        "🟢 Database — connected",
    ]

    await render(
        event,
        "\n".join(lines),
        build([[button("💠 Providers", adm("providers"))], admin_back_row()]),
    )
