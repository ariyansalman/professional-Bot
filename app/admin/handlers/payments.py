"""Admin payment management, manual review and reconciliation (58-59, 99)."""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.filters import IsAdmin
from app.admin.keyboards.panels import adm, admin_back_row, confirm_keyboard, payment_review_keyboard
from app.admin.permissions.rbac import Permissions
from app.admin.services.context import AdminContext, audit, consume_confirmation, create_confirmation
from app.bot.callbacks import AdminCB, ConfirmCB, PageCB
from app.bot.keyboards.common import build, button
from app.bot.services.formatting import DIVIDER, esc, money
from app.bot.services.screen import loading, render
from app.bot.states import AdminFlow
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.security import mask_address
from app.core.timeutils import humanize_datetime
from app.db.repositories.payments import (
    PaymentAttemptRepository,
    PaymentIntentRepository,
    ReconciliationRepository,
    VerificationAttemptRepository,
)
from app.domain.enums import AuditAction, PaymentStatus, ReconciliationStatus
from app.domain.payments.service import PaymentService

log = get_logger(__name__)
router = Router(name="admin_payments")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

FILTERS: dict[str, list[PaymentStatus]] = {
    "pending": [PaymentStatus.AWAITING_PAYMENT, PaymentStatus.SUBMITTED],
    "verifying": [
        PaymentStatus.DETECTING,
        PaymentStatus.DETECTED,
        PaymentStatus.VERIFYING,
        PaymentStatus.PENDING_CONFIRMATION,
    ],
    "review": [PaymentStatus.UNDER_REVIEW],
    "verified": [PaymentStatus.VERIFIED],
    "failed": [PaymentStatus.FAILED],
    "expired": [PaymentStatus.EXPIRED],
}

FILTER_LABELS = [
    ("review", "Review"),
    ("pending", "Pending"),
    ("verifying", "Verifying"),
    ("verified", "Verified"),
    ("failed", "Failed"),
    ("expired", "Expired"),
]


@router.callback_query(AdminCB.filter(F.section == "payments"))
async def payments_section(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: AdminContext,
    state: FSMContext,
) -> None:
    admin.require(Permissions.PAYMENTS_VIEW)
    action = callback_data.action

    if action == "view":
        await _detail(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "recheck":
        await _recheck(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "approve":
        await _request_approval(callback, session, admin, uuid.UUID(callback_data.arg), state)
    elif action == "reject":
        await _request_rejection(callback, session, admin, uuid.UUID(callback_data.arg), state)
    elif action == "audit":
        await _audit_trail(callback, session, admin, uuid.UUID(callback_data.arg))
    elif action == "noop":
        await callback.answer()
    else:
        await _list(callback, session, admin, callback_data.arg or "review", callback_data.page)


@router.callback_query(PageCB.filter(F.scope == "adm:payments"))
async def payments_page(
    callback: CallbackQuery, callback_data: PageCB, session: AsyncSession, admin: AdminContext
) -> None:
    admin.require(Permissions.PAYMENTS_VIEW)
    await _list(callback, session, admin, callback_data.arg or "review", callback_data.page)


async def _list(
    event, session: AsyncSession, admin: AdminContext, filter_key: str, page: int
) -> None:
    statuses = FILTERS.get(filter_key, FILTERS["review"])
    result = await PaymentIntentRepository(session).list_by_status(
        statuses, page=page, per_page=6
    )

    lines = ["💳 <b>PAYMENTS</b>", "", f"Filter: <b>{filter_key}</b> · {result.total} total", DIVIDER]
    if result.is_empty:
        lines.append("")
        lines.append("No payments in this filter.")

    rows = []
    chips = [
        button(f"• {label} •" if key == filter_key else label, adm("payments", arg=key))
        for key, label in FILTER_LABELS
    ]
    rows.extend([chips[i : i + 3] for i in range(0, len(chips), 3)])

    for intent in result.items:
        lines += [
            "",
            f"#{esc(intent.reference)} · <b>{intent.status.value}</b>",
            f"{money(intent.expected_amount, intent.asset)} · {intent.network.value}",
        ]
        if intent.review_reason:
            lines.append(f"⚠️ {esc(intent.review_reason[:80])}")
        rows.append(
            [button(f"💳 {intent.reference} · {intent.status.value}", adm("payments", "view", intent.id.hex))]
        )

    if result.pages > 1:
        nav = []
        if result.has_prev:
            nav.append(button("◀", PageCB(scope="adm:payments", page=result.page - 1, arg=filter_key).pack()))
        nav.append(button(result.label, adm("payments", "noop")))
        if result.has_next:
            nav.append(button("▶", PageCB(scope="adm:payments", page=result.page + 1, arg=filter_key).pack()))
        rows.append(nav)
    rows.append(admin_back_row())
    await render(event, "\n".join(lines), build(rows))


async def _detail(
    event, session: AsyncSession, admin: AdminContext, intent_id: uuid.UUID
) -> None:
    """Full payment evidence (section 58)."""
    repo = PaymentIntentRepository(session)
    intent = await repo.get_full(intent_id)
    if intent is None:
        await render(event, "⚠️ Payment not found.", build([admin_back_row("payments")]))
        return

    attempts = await PaymentAttemptRepository(session).list_for_intent(intent.id)
    verifications = await VerificationAttemptRepository(session).list_for_intent(intent.id, limit=5)
    order = intent.order

    lines = [
        "🔍 <b>PAYMENT REVIEW</b>" if intent.status is PaymentStatus.UNDER_REVIEW else "💳 <b>PAYMENT</b>",
        "",
        f"Order: <b>#{esc(intent.reference)}</b>",
        f"User: {esc(order.user.display_name) if order and order.user else 'reseller/api'}",
        "",
        f"Expected: <b>{money(intent.expected_amount, intent.asset)}</b>",
        f"Received: <b>{money(intent.received_amount or 0, intent.asset)}</b>",
        "",
        f"Method: {esc(intent.method.display_name)}",
        f"Provider: {intent.provider_code.value}",
        f"Network: {intent.network.value}",
        f"Destination: <code>{esc(mask_address(intent.destination))}</code>",
    ]
    if intent.token_contract:
        lines.append(f"Contract: <code>{esc(mask_address(intent.token_contract))}</code>")
    if intent.memo:
        lines.append(f"Memo: <code>{esc(intent.memo)}</code>")
    lines += [
        "",
        f"Status: <b>{intent.status.value}</b>",
        f"Confirmations: {intent.confirmations}/{intent.required_confirmations}",
        f"Attempts: {intent.verification_attempts}",
        f"Created: {humanize_datetime(intent.created_at)}",
        f"Expires: {humanize_datetime(intent.expires_at)}",
    ]
    if intent.verified_at:
        lines.append(f"Verified: {humanize_datetime(intent.verified_at)}")
    if intent.review_reason:
        lines += ["", f"⚠️ Reason: {esc(intent.review_reason)}"]
    if intent.failure_reason:
        lines += ["", f"❌ Failure: {esc(intent.failure_reason)}"]

    if attempts:
        lines += ["", DIVIDER, "<b>SUBMISSIONS</b>"]
        for attempt in attempts[-3:]:
            reference = attempt.submitted_txid or "-"
            lines.append(
                f"• {mask_address(reference, 8, 6)} · {attempt.last_outcome.value if attempt.last_outcome else 'pending'}"
            )

    if verifications:
        lines += ["", DIVIDER, "<b>LAST CHECKS</b>"]
        for verification in verifications[:3]:
            lines.append(
                f"• {humanize_datetime(verification.created_at)} — <b>{verification.outcome.value}</b>"
            )
            failed = [
                key
                for key, value in (verification.checks or {}).items()
                if isinstance(value, dict) and value.get("passed") is False
            ]
            if failed:
                lines.append(f"  failed: {', '.join(failed)}")

    await render(event, "\n".join(lines), payment_review_keyboard(admin, intent.id.hex))


async def _recheck(
    event, session: AsyncSession, admin: AdminContext, intent_id: uuid.UUID
) -> None:
    """Run a real verification pass against the provider."""
    admin.require(Permissions.PAYMENTS_RECHECK)
    if isinstance(event, CallbackQuery):
        await loading(event, "⏳ Rechecking payment...")

    repo = PaymentIntentRepository(session)
    intent = await repo.get_full(intent_id)
    if intent is None:
        await render(event, "⚠️ Payment not found.", build([admin_back_row("payments")]))
        return

    payments = PaymentService(session)
    try:
        result = await payments.verify(intent, triggered_by=f"admin:{admin.user.telegram_id}")
        outcome = result.outcome.value
    except AppError as exc:
        outcome = f"error: {exc.code}"
        log.warning("admin.recheck_failed", intent_id=str(intent_id), detail=exc.detail[:200])

    await audit(
        session,
        admin,
        AuditAction.PAYMENT_RECHECKED,
        target_type="payment_intent",
        target_id=intent_id,
        details={"outcome": outcome},
    )
    await _detail(event, session, admin, intent_id)


async def _request_approval(
    event, session: AsyncSession, admin: AdminContext, intent_id: uuid.UUID, state: FSMContext
) -> None:
    """Approval requires a permission, a reason and an explicit confirmation."""
    admin.require(Permissions.PAYMENTS_APPROVE)
    await state.set_state(AdminFlow.action_reason)
    await state.update_data(pending_action="payment_approve", pending_target=str(intent_id))
    await render(
        event,
        "\n".join(
            [
                "✅ <b>APPROVE PAYMENT</b>",
                "",
                "Approving credits this order without an automatic match.",
                "",
                "Send a short reason for the audit log.",
            ]
        ),
        build([[button("◀ Cancel", adm("payments", "view", intent_id.hex))]]),
    )


async def _request_rejection(
    event, session: AsyncSession, admin: AdminContext, intent_id: uuid.UUID, state: FSMContext
) -> None:
    admin.require(Permissions.PAYMENTS_REJECT)
    await state.set_state(AdminFlow.action_reason)
    await state.update_data(pending_action="payment_reject", pending_target=str(intent_id))
    await render(
        event,
        "\n".join(
            [
                "❌ <b>REJECT PAYMENT</b>",
                "",
                "Send a short reason for the audit log.",
            ]
        ),
        build([[button("◀ Cancel", adm("payments", "view", intent_id.hex))]]),
    )


@router.message(AdminFlow.action_reason, F.text)
async def capture_reason(
    message: Message, session: AsyncSession, admin: AdminContext, state: FSMContext
) -> None:
    """Capture the reason, then ask for a final confirmation (section 114)."""
    data = await state.get_data()
    await state.clear()
    action = data.get("pending_action")
    target = data.get("pending_target")
    reason = (message.text or "").strip()[:400]

    if not action or not target:
        await render(message, "⚠️ That action expired.", build([admin_back_row()]))
        return
    if len(reason) < 3:
        await render(message, "⚠️ A reason is required.", build([admin_back_row("payments")]))
        return

    token = await create_confirmation(
        actor_id=admin.user.id, action=action, payload={"target": target, "reason": reason}
    )
    verb = "APPROVE" if action == "payment_approve" else "REJECT"
    await render(
        message,
        "\n".join(
            [
                f"⚠️ <b>CONFIRM {verb}</b>",
                "",
                f"Payment: <code>{esc(target)}</code>",
                f"Reason: {esc(reason)}",
                "",
                "This is a financial action and will be recorded in the audit log.",
            ]
        ),
        confirm_keyboard(token, yes=f"✅ {verb.title()}"),
    )


@router.callback_query(ConfirmCB.filter())
async def handle_confirmation(
    callback: CallbackQuery,
    callback_data: ConfirmCB,
    session: AsyncSession,
    admin: AdminContext,
) -> None:
    """Execute a confirmed high-risk action.

    The token is consumed atomically, so a double-tap cannot run it twice.
    """
    if callback_data.decision != "yes":
        await render(callback, "Cancelled.", build([admin_back_row()]))
        return

    entry = await consume_confirmation(token=callback_data.token, actor_id=admin.user.id)
    if entry is None:
        await render(
            callback,
            "⚠️ That confirmation expired or was already used.",
            build([admin_back_row()]),
        )
        return

    action, payload = entry
    target = payload.get("target", "")
    reason = payload.get("reason", "")

    if action == "payment_approve":
        await _do_approve(callback, session, admin, uuid.UUID(target), reason)
    elif action == "payment_reject":
        await _do_reject(callback, session, admin, uuid.UUID(target), reason)
    else:
        # Other sections register their own confirmable actions.
        from app.admin.handlers.confirmations import dispatch_confirmation

        await dispatch_confirmation(callback, session, admin, action, payload)


async def _do_approve(
    event, session: AsyncSession, admin: AdminContext, intent_id: uuid.UUID, reason: str
) -> None:
    admin.require(Permissions.PAYMENTS_APPROVE)
    repo = PaymentIntentRepository(session)
    intent = await repo.get_full(intent_id)
    if intent is None:
        await render(event, "⚠️ Payment not found.", build([admin_back_row("payments")]))
        return

    payments = PaymentService(session)
    try:
        approved = await payments.approve_manually(
            intent=intent, actor_id=admin.user.id, actor_label=admin.label, reason=reason
        )
    except AppError as exc:
        await render(
            event,
            f"⚠️ {exc.safe_message}",
            build([[button("◀ Back", adm("payments", "view", intent_id.hex))]]),
        )
        return

    await audit(
        session,
        admin,
        AuditAction.PAYMENT_APPROVED,
        target_type="payment_intent",
        target_id=intent_id,
        reason=reason,
        details={"order": intent.reference, "newly_verified": approved},
    )
    await render(
        event,
        f"✅ Payment <b>#{esc(intent.reference)}</b> approved.",
        build([[button("💳 View payment", adm("payments", "view", intent_id.hex))], admin_back_row()]),
    )


async def _do_reject(
    event, session: AsyncSession, admin: AdminContext, intent_id: uuid.UUID, reason: str
) -> None:
    admin.require(Permissions.PAYMENTS_REJECT)
    repo = PaymentIntentRepository(session)
    intent = await repo.get_full(intent_id)
    if intent is None:
        await render(event, "⚠️ Payment not found.", build([admin_back_row("payments")]))
        return
    try:
        await payments_reject(session, intent, admin, reason)
    except AppError as exc:
        await render(
            event,
            f"⚠️ {exc.safe_message}",
            build([[button("◀ Back", adm("payments", "view", intent_id.hex))]]),
        )
        return

    await audit(
        session,
        admin,
        AuditAction.PAYMENT_REJECTED,
        target_type="payment_intent",
        target_id=intent_id,
        reason=reason,
        details={"order": intent.reference},
    )
    await render(
        event,
        f"❌ Payment <b>#{esc(intent.reference)}</b> rejected.",
        build([[button("💳 View payment", adm("payments", "view", intent_id.hex))], admin_back_row()]),
    )


async def payments_reject(session, intent, admin: AdminContext, reason: str) -> None:
    await PaymentService(session).reject_manually(
        intent=intent, actor_id=admin.user.id, actor_label=admin.label, reason=reason
    )


async def _audit_trail(
    event, session: AsyncSession, admin: AdminContext, intent_id: uuid.UUID
) -> None:
    admin.require(Permissions.AUDIT_VIEW)
    verifications = await VerificationAttemptRepository(session).list_for_intent(intent_id, limit=15)
    lines = ["📋 <b>VERIFICATION TRAIL</b>", ""]
    if not verifications:
        lines.append("No verification attempts recorded.")
    for verification in verifications:
        lines += [
            f"<b>{verification.outcome.value}</b> · {humanize_datetime(verification.created_at)}",
            f"provider: {verification.provider_code.value} · {verification.duration_ms or 0} ms",
        ]
        if verification.external_reference:
            lines.append(f"tx: <code>{esc(mask_address(verification.external_reference, 10, 8))}</code>")
        for key, value in (verification.checks or {}).items():
            if isinstance(value, dict):
                mark = "✓" if value.get("passed") else "✗"
                lines.append(f"  {mark} {key}")
        if verification.detail:
            lines.append(f"  <i>{esc(verification.detail[:160])}</i>")
        lines.append("")

    await render(
        event,
        "\n".join(lines),
        build([[button("◀ Back", adm("payments", "view", intent_id.hex))], admin_back_row()]),
    )


@router.callback_query(AdminCB.filter(F.section == "reconciliation"))
async def reconciliation(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, admin: AdminContext
) -> None:
    """Anomaly queue (section 99)."""
    admin.require(Permissions.PAYMENTS_VIEW)
    repo = ReconciliationRepository(session)

    if callback_data.action == "resolve":
        admin.require(Permissions.RECONCILIATION_RESOLVE)
        record = await repo.get(uuid.UUID(callback_data.arg))
        if record is not None:
            await repo.resolve(
                record,
                resolved_by_id=admin.user.id,
                note=f"resolved by {admin.label}",
                status=ReconciliationStatus.RESOLVED,
            )
            log.info("admin.reconciliation_resolved", record_id=str(record.id))

    page = await repo.open_records(page=callback_data.page, per_page=6)
    lines = ["🧮 <b>RECONCILIATION</b>", "", f"{page.total} open item(s)", DIVIDER]
    if page.is_empty:
        lines += ["", "✅ Nothing to reconcile."]

    rows = []
    for record in page.items:
        lines += [
            "",
            f"<b>{record.kind.value}</b>",
            esc(record.summary),
            humanize_datetime(record.created_at),
        ]
        if admin.can(Permissions.RECONCILIATION_RESOLVE):
            rows.append(
                [button(f"✅ Resolve {record.kind.value[:16]}", adm("reconciliation", "resolve", record.id.hex))]
            )
        if record.payment_intent_id:
            rows.append(
                [button("💳 Open payment", adm("payments", "view", record.payment_intent_id.hex))]
            )
    rows.append(admin_back_row())
    await render(callback, "\n".join(lines), build(rows))
