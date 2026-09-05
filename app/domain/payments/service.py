"""Payment intent lifecycle and the verification orchestrator.

This is the module that turns an observed transaction into money in the
business. Every rule that protects the platform financially lives here or is
enforced by a constraint this module relies on:

* an intent's ``expected_amount`` is written once at creation and never changed
* verification runs against the intent's own frozen configuration snapshot
* a transaction is claimed through ``payment_consumptions`` (UNIQUE fingerprint)
  before the payment is marked verified, so it can never be spent twice
* the intent is only marked VERIFIED after the claim succeeds
* every attempt - success or failure - is recorded with its evidence
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.correlation import get_correlation_id
from app.core.exceptions import (
    ConfigurationError,
    ConflictError,
    NotFoundError,
    ProviderError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.money import base_units
from app.core.timeutils import ensure_utc, is_expired, utcnow
from app.db.models.order import Order
from app.db.models.payment import PaymentAttempt, PaymentIntent, PaymentMethod
from app.db.repositories.orders import LedgerRepository, OrderRepository
from app.db.repositories.payments import (
    PaymentAttemptRepository,
    PaymentConsumptionRepository,
    PaymentIntentRepository,
    PaymentMethodRepository,
    PaymentProviderRepository,
    ReconciliationRepository,
    TransactionRepository,
    VerificationAttemptRepository,
)
from app.domain.enums import (
    LedgerEntryType,
    NetworkCode,
    OrderStatus,
    PaymentStatus,
    ProviderCode,
    ReconciliationKind,
    VerificationOutcome,
)
from app.domain.payments.fingerprint import normalize_address, transaction_fingerprint
from app.domain.payments.registry import build_adapter, requires_customer_reference
from app.domain.payments.types import ObservedTransaction, PaymentExpectation
from app.domain.payments.verification import (
    VerificationDecision,
    quote_expected_amount,
    select_best_candidate,
    verify_transaction,
)
from app.domain.state_machines import assert_payment_transition, payment_path

log = get_logger(__name__)


@dataclass(slots=True)
class VerificationResult:
    """What the caller (worker or bot handler) needs to react to."""

    outcome: VerificationOutcome
    intent: PaymentIntent
    decision: VerificationDecision
    transaction: ObservedTransaction | None = None
    #: True when this call is what flipped the intent to VERIFIED.
    newly_verified: bool = False
    #: True when the payment now needs a human.
    needs_review: bool = False
    retry_in_seconds: int | None = None


class PaymentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.intents = PaymentIntentRepository(session)
        self.attempts = PaymentAttemptRepository(session)
        self.methods = PaymentMethodRepository(session)
        self.providers = PaymentProviderRepository(session)
        self.verifications = VerificationAttemptRepository(session)
        self.consumptions = PaymentConsumptionRepository(session)
        self.transactions = TransactionRepository(session)
        self.reconciliation = ReconciliationRepository(session)
        self.orders = OrderRepository(session)
        self.ledger = LedgerRepository(session)

    # -- intent creation ---------------------------------------------------

    async def create_intent(
        self, *, order: Order, method: PaymentMethod, window_seconds: int | None = None
    ) -> PaymentIntent:
        """Create the payment intent for an order.

        The expectation snapshot (amount, destination, contract, confirmations)
        is frozen here. A later change to the payment method's configuration
        cannot retroactively validate or invalidate this payment.
        """
        if order.status.is_paid:
            raise ConflictError(
                f"order {order.reference} is already paid",
                safe_message="This order has already been paid.",
            )
        if not method.is_enabled or not method.provider.is_enabled:
            raise ConfigurationError(
                f"payment method {method.code} is disabled",
                safe_message="This payment method is currently unavailable.",
            )
        if not method.receiving_address:
            raise ConfigurationError(
                f"payment method {method.code} has no receiving destination configured",
                safe_message="This payment method is currently unavailable.",
            )

        expected_amount = quote_expected_amount(
            order.total, method.quote_rate or Decimal("1"), method.asset_decimals
        )
        if method.min_amount is not None and expected_amount < method.min_amount:
            raise ValidationError(
                f"amount {expected_amount} below method minimum {method.min_amount}",
                safe_message=f"The minimum for this method is {method.min_amount} {method.asset}.",
            )
        if method.max_amount is not None and expected_amount > method.max_amount:
            raise ValidationError(
                f"amount {expected_amount} above method maximum {method.max_amount}",
                safe_message=f"The maximum for this method is {method.max_amount} {method.asset}.",
            )

        # Cancel any other open intent for this order so only one is payable.
        existing = await self.intents.active_for_order(order.id)
        if existing is not None:
            await self._transition(existing, PaymentStatus.CANCELLED)

        window = window_seconds or method.payment_window_seconds or (
            get_settings().payments.default_window_seconds
        )
        now = utcnow()
        reference = order.reference
        memo = None
        if method.requires_memo:
            memo = (method.memo_template or "{reference}").format(reference=reference)

        intent = PaymentIntent(
            reference=reference,
            order_id=order.id,
            payment_method_id=method.id,
            provider_code=method.provider.code,
            network=method.network,
            status=PaymentStatus.CREATED,
            order_amount=order.total,
            order_currency=order.currency,
            expected_amount=expected_amount,
            expected_amount_units=base_units(expected_amount, method.asset_decimals),
            asset=method.asset,
            asset_decimals=method.asset_decimals,
            destination=method.receiving_address,
            destination_normalized=normalize_address(method.receiving_address, method.network),
            token_contract=method.token_contract,
            memo=memo,
            required_confirmations=method.required_confirmations,
            quote_rate=method.quote_rate or Decimal("1"),
            expires_at=now + timedelta(seconds=window),
            correlation_id=get_correlation_id() or order.correlation_id,
            verification_config={
                "method_code": method.code,
                "network_label": method.network_label,
                "requires_memo": method.requires_memo,
                "required_confirmations": method.required_confirmations,
                "window_seconds": window,
                "quote_rate": str(method.quote_rate or 1),
                "frozen_at": now.isoformat(),
            },
        )
        # Bind the loaded parents so later access never triggers a lazy load.
        intent.order = order
        intent.method = method
        await self.intents.add(intent)
        await self._transition(intent, PaymentStatus.AWAITING_PAYMENT)

        log.info(
            "payment.intent_created",
            intent_id=str(intent.id),
            order=order.reference,
            method=method.code,
            expected_amount=str(expected_amount),
            asset=method.asset,
            network=method.network.value,
            expires_at=intent.expires_at.isoformat(),
        )
        return intent

    def expectation(self, intent: PaymentIntent) -> PaymentExpectation:
        """Build the immutable expectation the engine checks against."""
        return PaymentExpectation(
            intent_id=str(intent.id),
            reference=intent.reference,
            provider=intent.provider_code,
            network=intent.network,
            asset=intent.asset,
            asset_decimals=intent.asset_decimals,
            expected_amount=intent.expected_amount,
            expected_amount_units=int(intent.expected_amount_units),
            destination=intent.destination,
            destination_normalized=intent.destination_normalized,
            token_contract=intent.token_contract,
            memo=intent.memo,
            required_confirmations=intent.required_confirmations,
            created_at=ensure_utc(intent.created_at) or utcnow(),
            expires_at=ensure_utc(intent.expires_at) or utcnow(),
            config=intent.verification_config or {},
        )

    # -- customer submissions ----------------------------------------------

    async def submit_payment(
        self,
        *,
        intent: PaymentIntent,
        reference: str | None = None,
        source: str = "telegram",
    ) -> PaymentAttempt:
        """Record an "I've paid" claim.

        The submitted reference is a *lookup hint only*. Nothing here marks the
        payment as received; it merely tells the verification worker where to
        look.
        """
        if intent.status is PaymentStatus.VERIFIED:
            raise ConflictError(
                f"intent {intent.id} is already verified",
                safe_message="This payment has already been confirmed.",
            )
        if not intent.status.is_open:
            raise ConflictError(
                f"intent {intent.id} is {intent.status} and no longer accepts submissions",
                safe_message="This payment can no longer be updated.",
            )

        submitted = (reference or "").strip() or None
        if submitted and len(submitted) > 160:
            raise ValidationError(
                "submitted reference is too long",
                safe_message="That transaction reference looks invalid.",
            )
        if requires_customer_reference(intent.provider_code) and not submitted:
            raise ValidationError(
                f"{intent.provider_code.value} requires a transaction reference",
                safe_message="Please submit your transaction ID so we can verify the payment.",
            )

        attempt = PaymentAttempt(
            payment_intent_id=intent.id,
            provider_code=intent.provider_code,
            network=intent.network,
            submitted_reference=submitted,
            submitted_txid=submitted,
            submitted_at=utcnow(),
            source=source,
        )
        await self.attempts.add(attempt)

        intent.submitted_at = intent.submitted_at or utcnow()
        await self._transition(intent, PaymentStatus.SUBMITTED)
        intent.next_poll_at = utcnow()
        await self.session.flush()

        log.info(
            "payment.submitted",
            intent_id=str(intent.id),
            order=intent.reference,
            has_reference=bool(submitted),
            source=source,
        )
        return attempt

    # -- verification ------------------------------------------------------

    async def verify(
        self, intent: PaymentIntent, *, triggered_by: str = "worker"
    ) -> VerificationResult:
        """Run one verification pass for an intent.

        This is the only path by which a payment becomes VERIFIED, and it is
        safe to call concurrently: the consumption claim is what decides.
        """
        settings = get_settings()

        if intent.status is PaymentStatus.VERIFIED:
            return VerificationResult(
                outcome=VerificationOutcome.VERIFIED,
                intent=intent,
                decision=VerificationDecision(outcome=VerificationOutcome.VERIFIED),
                newly_verified=False,
            )

        expectation = self.expectation(intent)
        attempt = await self.attempts.latest_for_intent(intent.id)
        reference = attempt.submitted_txid if attempt else None

        if intent.status in (PaymentStatus.SUBMITTED, PaymentStatus.AWAITING_PAYMENT):
            await self._transition(intent, PaymentStatus.DETECTING)

        method = intent.method or await self.methods.get(intent.payment_method_id)
        if method is None:
            raise NotFoundError(f"payment method {intent.payment_method_id} missing")
        provider = method.provider

        import asyncio

        loop = asyncio.get_running_loop()
        started = loop.time()
        adapter = None
        try:
            adapter = build_adapter(provider, method)
            transactions = await adapter.find_transactions(expectation, reference=reference)
        except ConfigurationError as exc:
            # Misconfiguration must never look like a customer failure.
            return await self._record_outcome(
                intent=intent,
                attempt=attempt,
                outcome=VerificationOutcome.UNSUPPORTED,
                decision=VerificationDecision(
                    outcome=VerificationOutcome.UNSUPPORTED, detail=str(exc)
                ),
                duration_ms=int((loop.time() - started) * 1000),
                transaction=None,
            )
        except ProviderError as exc:
            log.warning(
                "payment.provider_error",
                intent_id=str(intent.id),
                provider=provider.code.value,
                error=str(exc)[:300],
                retryable=exc.retryable,
            )
            await self.providers.record_health(
                provider, healthy=False, latency_ms=0, message=str(exc)[:200]
            )
            return await self._record_outcome(
                intent=intent,
                attempt=attempt,
                outcome=VerificationOutcome.PROVIDER_ERROR,
                decision=VerificationDecision(
                    outcome=VerificationOutcome.PROVIDER_ERROR, detail=str(exc)[:400]
                ),
                duration_ms=int((loop.time() - started) * 1000),
                transaction=None,
            )
        finally:
            if adapter is not None:
                await adapter.aclose()

        duration_ms = int((loop.time() - started) * 1000)
        await self.providers.record_health(
            provider, healthy=True, latency_ms=duration_ms, message="OK"
        )

        # A submitted txid identifies one specific transaction, so it is judged
        # directly - the customer deserves to hear "wrong network" rather than
        # "not found". A polled list is ranked instead.
        if reference and len(transactions) == 1:
            transaction = transactions[0]
            decision = verify_transaction(
                expectation,
                transaction,
                underpayment_tolerance=settings.payments.underpayment_tolerance,
                overpayment_tolerance=settings.payments.overpayment_tolerance,
                late_payment_grace=timedelta(
                    seconds=settings.payments.late_payment_grace_seconds
                ),
            )
        else:
            transaction, decision = select_best_candidate(
                expectation,
                transactions,
                underpayment_tolerance=settings.payments.underpayment_tolerance,
                overpayment_tolerance=settings.payments.overpayment_tolerance,
                late_payment_grace=timedelta(
                    seconds=settings.payments.late_payment_grace_seconds
                ),
            )

        if transaction is not None:
            await self._persist_transaction(transaction)

        # A verified decision still has to survive the duplicate check.
        if decision.outcome is VerificationOutcome.VERIFIED and transaction is not None:
            claimed, duplicate_of = await self._claim(intent, transaction)
            if not claimed:
                decision = VerificationDecision(
                    outcome=VerificationOutcome.DUPLICATE,
                    checks={
                        **decision.checks,
                        "duplicate": {
                            "passed": False,
                            "already_used_by": str(duplicate_of) if duplicate_of else "unknown",
                        },
                    },
                    observed_amount=decision.observed_amount,
                    observed_confirmations=decision.observed_confirmations,
                    detail=(
                        f"transaction {transaction.external_id} is already consumed by "
                        f"intent {duplicate_of}"
                    ),
                )

        return await self._record_outcome(
            intent=intent,
            attempt=attempt,
            outcome=decision.outcome,
            decision=decision,
            duration_ms=duration_ms,
            transaction=transaction,
            triggered_by=triggered_by,
        )

    async def _claim(
        self, intent: PaymentIntent, transaction: ObservedTransaction
    ) -> tuple[bool, uuid.UUID | None]:
        """Atomically claim a transaction for this intent.

        Returns ``(claimed, existing_intent_id)``. A claim already held by the
        *same* intent counts as claimed - that is a safe retry, not a duplicate.
        """
        fingerprint = transaction_fingerprint(
            transaction.provider,
            transaction.network,
            transaction.external_id,
            transaction.log_index,
        )
        consumption, claimed = await self.consumptions.claim(
            fingerprint=fingerprint,
            payment_intent_id=intent.id,
            order_id=intent.order_id,
            provider_code=transaction.provider,
            network=transaction.network,
            external_id=transaction.external_id,
            amount=transaction.amount,
            amount_units=transaction.amount_units,
            asset=transaction.asset,
            correlation_id=intent.correlation_id,
        )
        if claimed:
            return True, None
        if consumption is not None and consumption.payment_intent_id == intent.id:
            return True, None
        log.warning(
            "payment.duplicate_transaction_blocked",
            intent_id=str(intent.id),
            external_id=transaction.external_id,
            already_used_by=str(consumption.payment_intent_id) if consumption else None,
        )
        return False, consumption.payment_intent_id if consumption else None

    async def _persist_transaction(self, transaction: ObservedTransaction) -> None:
        """Store the observation for reconciliation and dispute handling."""
        if transaction.network is NetworkCode.EXCHANGE_INTERNAL or transaction.provider in (
            ProviderCode.BINANCE,
            ProviderCode.BINANCE_PAY,
            ProviderCode.BYBIT,
            ProviderCode.OKX,
        ):
            await self.transactions.upsert_provider(
                provider_code=transaction.provider,
                external_id=transaction.external_id,
                asset=transaction.asset,
                amount=transaction.amount,
                observed_at=transaction.observed_at,
                record_type=transaction.record_type,
                network=transaction.network.value,
                status=transaction.status_label,
                txid=transaction.txid,
                address=transaction.to_address,
                reference=transaction.reference,
                confirmations=transaction.confirmations,
                raw_payload=transaction.raw,
            )
            return
        await self.transactions.upsert_blockchain(
            network=transaction.network,
            txid=transaction.txid or transaction.external_id,
            log_index=transaction.log_index,
            asset=transaction.asset,
            amount_units=transaction.amount_units,
            amount=transaction.amount,
            decimals=transaction.decimals,
            to_address=transaction.to_address,
            to_address_normalized=transaction.to_address_normalized,
            is_successful=transaction.is_successful,
            observed_at=transaction.observed_at,
            token_contract=transaction.token_contract,
            from_address=transaction.from_address,
            memo=transaction.memo,
            block_number=transaction.block_number,
            block_time=transaction.block_time,
            confirmations=transaction.confirmations,
            raw_payload=transaction.raw,
        )

    async def _record_outcome(
        self,
        *,
        intent: PaymentIntent,
        attempt: PaymentAttempt | None,
        outcome: VerificationOutcome,
        decision: VerificationDecision,
        duration_ms: int,
        transaction: ObservedTransaction | None,
        triggered_by: str = "worker",
    ) -> VerificationResult:
        """Persist the attempt evidence and move the intent's state."""
        settings = get_settings()
        intent.verification_attempts += 1
        intent.last_outcome = outcome

        await self.verifications.record(
            payment_intent_id=intent.id,
            payment_attempt_id=attempt.id if attempt else None,
            provider_code=intent.provider_code,
            outcome=outcome,
            checks=decision.checks,
            observed_amount=decision.observed_amount,
            observed_confirmations=decision.observed_confirmations,
            external_reference=transaction.external_id if transaction else None,
            detail=decision.detail,
            duration_ms=duration_ms,
            correlation_id=intent.correlation_id,
        )
        if attempt is not None:
            attempt.verification_count += 1
            attempt.last_outcome = outcome
            if outcome is not VerificationOutcome.NOT_FOUND:
                attempt.failure_reason = decision.detail[:255] if not decision.is_verified else None

        if transaction is not None:
            intent.received_amount = transaction.amount
            intent.received_amount_units = transaction.amount_units
            intent.confirmations = transaction.confirmations

        newly_verified = False
        needs_review = False
        retry_in: int | None = None

        if outcome is VerificationOutcome.VERIFIED:
            newly_verified = await self._mark_verified(intent, transaction)
        elif outcome is VerificationOutcome.PENDING_CONFIRMATION:
            intent.detected_at = intent.detected_at or utcnow()
            await self._advance(intent, PaymentStatus.PENDING_CONFIRMATION)
            retry_in = self._confirmation_backoff(intent)
        elif outcome is VerificationOutcome.NOT_FOUND:
            if is_expired(intent.expires_at):
                await self._expire(intent)
            elif intent.verification_attempts >= settings.payments.max_verification_attempts:
                needs_review = True
                await self._to_review(intent, "verification attempt limit reached")
            else:
                await self._transition(intent, PaymentStatus.DETECTING)
                retry_in = settings.payments.verification_poll_interval
        elif outcome is VerificationOutcome.PROVIDER_ERROR:
            # Provider trouble is never the customer's fault: keep polling.
            retry_in = min(settings.payments.verification_poll_interval * 3, 180)
            if intent.verification_attempts >= settings.payments.max_verification_attempts:
                needs_review = True
                await self._to_review(intent, "provider unavailable")
        elif outcome.needs_review:
            needs_review = True
            await self._to_review(intent, decision.detail or outcome.value)
            await self._raise_reconciliation(intent, outcome, decision, transaction)
        else:
            # FAILED_TRANSACTION and anything else terminal for this attempt.
            intent.failure_reason = (decision.detail or outcome.value)[:255]
            intent.failed_at = utcnow()
            await self._advance(intent, PaymentStatus.FAILED)

        if retry_in is not None:
            await self.intents.schedule_next_poll(intent, retry_in)
        await self.session.flush()

        log.info(
            "payment.verification_attempt",
            intent_id=str(intent.id),
            order=intent.reference,
            outcome=outcome.value,
            attempts=intent.verification_attempts,
            duration_ms=duration_ms,
            triggered_by=triggered_by,
            newly_verified=newly_verified,
            needs_review=needs_review,
        )
        return VerificationResult(
            outcome=outcome,
            intent=intent,
            decision=decision,
            transaction=transaction,
            newly_verified=newly_verified,
            needs_review=needs_review,
            retry_in_seconds=retry_in,
        )

    def _confirmation_backoff(self, intent: PaymentIntent) -> int:
        """Poll faster when confirmations are close, slower when far away."""
        missing = max(intent.required_confirmations - intent.confirmations, 1)
        base = get_settings().payments.verification_poll_interval
        return min(base * max(missing // 4, 1), 120)

    async def _mark_verified(
        self, intent: PaymentIntent, transaction: ObservedTransaction | None
    ) -> bool:
        """Flip the intent to VERIFIED and journal the money. Idempotent."""
        if intent.status is PaymentStatus.VERIFIED:
            return False
        await self._advance(intent, PaymentStatus.VERIFIED)
        intent.verified_at = utcnow()
        intent.failure_reason = None
        intent.review_reason = None

        order = intent.order or await self.orders.get_with_items(intent.order_id)
        if order is not None and order.status is not OrderStatus.PAYMENT_VERIFIED:
            from app.domain.orders.service import OrderService

            await OrderService(self.session).transition(order, OrderStatus.PAYMENT_VERIFIED)

        await self.ledger.record(
            entry_type=LedgerEntryType.PAYMENT_VERIFIED,
            amount=intent.received_amount or intent.expected_amount,
            currency=intent.asset,
            dedupe_key=f"payment_verified:{intent.id}",
            order_id=intent.order_id,
            payment_intent_id=intent.id,
            user_id=order.user_id if order else None,
            description=f"Payment verified for order {intent.reference}",
            details={
                "provider": intent.provider_code.value,
                "network": intent.network.value,
                "external_id": transaction.external_id if transaction else None,
                "expected": str(intent.expected_amount),
                "received": str(intent.received_amount or intent.expected_amount),
                "confirmations": intent.confirmations,
            },
            correlation_id=intent.correlation_id,
        )
        await self.session.flush()
        log.info(
            "payment.verified",
            intent_id=str(intent.id),
            order=intent.reference,
            amount=str(intent.received_amount or intent.expected_amount),
            asset=intent.asset,
            provider=intent.provider_code.value,
        )
        return True

    async def _to_review(self, intent: PaymentIntent, reason: str) -> None:
        intent.review_reason = reason[:255]
        await self._advance(intent, PaymentStatus.UNDER_REVIEW)
        order = intent.order or await self.orders.get_with_items(intent.order_id)
        if order is not None and not order.status.is_paid and not order.status.is_terminal:
            from app.domain.orders.service import OrderService

            try:
                await OrderService(self.session).transition(
                    order, OrderStatus.MANUAL_REVIEW, reason=reason
                )
            except Exception as exc:  # pragma: no cover - transition guard
                log.warning(
                    "payment.review_order_transition_failed",
                    order_id=str(order.id),
                    error=str(exc),
                )

    async def _expire(self, intent: PaymentIntent) -> None:
        await self._advance(intent, PaymentStatus.EXPIRED)
        intent.failure_reason = "payment window expired"
        log.info("payment.expired", intent_id=str(intent.id), order=intent.reference)

    async def _raise_reconciliation(
        self,
        intent: PaymentIntent,
        outcome: VerificationOutcome,
        decision: VerificationDecision,
        transaction: ObservedTransaction | None,
    ) -> None:
        kind_map = {
            VerificationOutcome.UNDERPAID: ReconciliationKind.AMOUNT_MISMATCH,
            VerificationOutcome.OVERPAID: ReconciliationKind.AMOUNT_MISMATCH,
            VerificationOutcome.WRONG_NETWORK: ReconciliationKind.WRONG_NETWORK,
            VerificationOutcome.WRONG_ASSET: ReconciliationKind.PROVIDER_INCONSISTENCY,
            VerificationOutcome.DUPLICATE: ReconciliationKind.DUPLICATE_TRANSACTION,
            VerificationOutcome.OUTSIDE_WINDOW: ReconciliationKind.LATE_PAYMENT,
            VerificationOutcome.MEMO_MISMATCH: ReconciliationKind.UNMATCHED_TRANSACTION,
            VerificationOutcome.UNSUPPORTED: ReconciliationKind.PROVIDER_INCONSISTENCY,
        }
        kind = kind_map.get(outcome)
        if kind is None:
            return
        external = transaction.external_id if transaction else "none"
        await self.reconciliation.record(
            kind=kind,
            dedupe_key=f"{kind.value}:{intent.id}:{external}",
            summary=f"{outcome.value} on {intent.reference}",
            payment_intent_id=intent.id,
            order_id=intent.order_id,
            details={
                "outcome": outcome.value,
                "detail": decision.detail,
                "checks": decision.checks,
                "expected_amount": str(intent.expected_amount),
                "observed_amount": str(decision.observed_amount)
                if decision.observed_amount
                else None,
                "external_id": external,
            },
        )

    async def _transition(self, intent: PaymentIntent, target: PaymentStatus) -> None:
        """Move one step, guarded by the transition table."""
        assert_payment_transition(intent.status, target)
        intent.status = target
        await self.session.flush()

    async def _advance(self, intent: PaymentIntent, target: PaymentStatus) -> None:
        """Walk the canonical path to ``target``.

        A single verification pass often establishes in one call what the state
        machine models as several steps (detecting -> detected -> verifying ->
        verified). Walking the path keeps every intermediate state real and
        keeps the transition table authoritative.
        """
        for step in payment_path(intent.status, target):
            await self._transition(intent, step)

    # -- admin actions -----------------------------------------------------

    async def approve_manually(
        self,
        *,
        intent: PaymentIntent,
        actor_id: uuid.UUID,
        actor_label: str,
        reason: str,
    ) -> bool:
        """Credit a payment after human review.

        Still refuses to double-consume: if the observed transaction is already
        claimed by another intent, approval is rejected. Manual review resolves
        ambiguity, it does not bypass the integrity rules.
        """
        if intent.status is PaymentStatus.VERIFIED:
            return False
        if intent.status not in (
            PaymentStatus.UNDER_REVIEW,
            PaymentStatus.FAILED,
            PaymentStatus.EXPIRED,
            PaymentStatus.PENDING_CONFIRMATION,
            PaymentStatus.DETECTED,
        ):
            raise ConflictError(
                f"intent {intent.id} in status {intent.status} cannot be manually approved",
                safe_message="This payment cannot be approved in its current state.",
            )

        existing = await self.consumptions.for_order(intent.order_id)
        conflicting = [c for c in existing if c.payment_intent_id != intent.id]
        if conflicting:
            raise ConflictError(
                f"order {intent.reference} already has a consumed transaction",
                safe_message="This order already has a confirmed payment.",
            )

        newly = await self._mark_verified(intent, None)

        await self.ledger.record(
            entry_type=LedgerEntryType.MANUAL_ADJUSTMENT,
            amount=Decimal("0"),
            currency=intent.asset,
            dedupe_key=f"manual_approval:{intent.id}",
            order_id=intent.order_id,
            payment_intent_id=intent.id,
            actor_id=actor_id,
            description=f"Manual approval of {intent.reference} by {actor_label}",
            details={"reason": reason},
            correlation_id=intent.correlation_id,
        )
        log.warning(
            "payment.manually_approved",
            intent_id=str(intent.id),
            order=intent.reference,
            actor_id=str(actor_id),
            reason=reason,
        )
        return newly

    async def reject_manually(
        self, *, intent: PaymentIntent, actor_id: uuid.UUID, actor_label: str, reason: str
    ) -> None:
        if intent.status is PaymentStatus.VERIFIED:
            raise ConflictError(
                f"intent {intent.id} is verified and cannot be rejected; issue a refund",
                safe_message="This payment is already verified. Use a refund instead.",
            )
        intent.failure_reason = reason[:255]
        intent.failed_at = utcnow()
        await self._advance(intent, PaymentStatus.FAILED)
        log.warning(
            "payment.manually_rejected",
            intent_id=str(intent.id),
            actor_id=str(actor_id),
            reason=reason,
        )

    async def expire_due(self, *, limit: int = 100) -> int:
        """Expire intents whose window lapsed without payment."""
        expired = 0
        for intent in await self.intents.expired_open(limit=limit):
            await self._expire(intent)
            expired += 1
        return expired

    def payment_progress(self, intent: PaymentIntent) -> dict[str, Any]:
        """Data for the customer's verification screen."""
        return {
            "status": intent.status.value,
            "confirmations": intent.confirmations,
            "required_confirmations": intent.required_confirmations,
            "expected_amount": intent.expected_amount,
            "received_amount": intent.received_amount,
            "asset": intent.asset,
            "expires_at": intent.expires_at,
            "attempts": intent.verification_attempts,
        }
