"""Payment repositories, including the single-consumption claim."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core.timeutils import utcnow
from app.db.models.payment import (
    BlockchainTransaction,
    PaymentAttempt,
    PaymentConsumption,
    PaymentIntent,
    PaymentMethod,
    PaymentProvider,
    ProviderTransaction,
    ReconciliationRecord,
    VerificationAttempt,
)
from app.db.repositories.base import BaseRepository, Page
from app.domain.enums import (
    NetworkCode,
    PaymentStatus,
    ProviderCode,
    ReconciliationKind,
    ReconciliationStatus,
    VerificationOutcome,
)


class PaymentProviderRepository(BaseRepository[PaymentProvider]):
    model = PaymentProvider

    async def get_by_code(self, code: ProviderCode) -> PaymentProvider | None:
        stmt = select(PaymentProvider).where(PaymentProvider.code == code)
        return await self.session.scalar(stmt)

    async def list_all(self) -> list[PaymentProvider]:
        stmt = select(PaymentProvider).order_by(PaymentProvider.kind, PaymentProvider.code)
        return list((await self.session.scalars(stmt)).all())

    async def list_enabled(self) -> list[PaymentProvider]:
        stmt = select(PaymentProvider).where(PaymentProvider.is_enabled.is_(True))
        return list((await self.session.scalars(stmt)).all())

    async def record_health(
        self,
        provider: PaymentProvider,
        *,
        healthy: bool,
        latency_ms: int,
        message: str,
    ) -> None:
        provider.health_status = "healthy" if healthy else "unhealthy"
        provider.health_checked_at = utcnow()
        provider.health_latency_ms = latency_ms
        provider.health_message = message[:255]
        if healthy:
            provider.consecutive_failures = 0
            provider.last_success_at = utcnow()
        else:
            provider.consecutive_failures += 1
        await self.session.flush()


class PaymentMethodRepository(BaseRepository[PaymentMethod]):
    model = PaymentMethod

    async def get_by_code(self, code: str) -> PaymentMethod | None:
        stmt = (
            select(PaymentMethod)
            .where(PaymentMethod.code == code)
            .options(selectinload(PaymentMethod.provider))
        )
        return await self.session.scalar(stmt)

    async def list_all(self) -> list[PaymentMethod]:
        stmt = (
            select(PaymentMethod)
            .options(selectinload(PaymentMethod.provider))
            .order_by(PaymentMethod.sort_priority.desc(), PaymentMethod.display_name)
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_available(self) -> list[PaymentMethod]:
        """Only methods that are enabled, configured and whose provider is healthy.

        A method with no receiving destination, or whose provider is disabled or
        failing repeatedly, is hidden rather than offered and then failing.
        """
        stmt = (
            select(PaymentMethod)
            .join(PaymentProvider, PaymentMethod.provider_id == PaymentProvider.id)
            .where(
                PaymentMethod.is_enabled.is_(True),
                PaymentProvider.is_enabled.is_(True),
                PaymentMethod.receiving_address.is_not(None),
                PaymentProvider.consecutive_failures < 5,
            )
            .options(selectinload(PaymentMethod.provider))
            .order_by(PaymentMethod.sort_priority.desc(), PaymentMethod.display_name)
        )
        return list((await self.session.scalars(stmt)).all())


class PaymentIntentRepository(BaseRepository[PaymentIntent]):
    model = PaymentIntent

    async def get_full(self, intent_id: uuid.UUID) -> PaymentIntent | None:
        stmt = (
            select(PaymentIntent)
            .where(PaymentIntent.id == intent_id)
            .options(
                selectinload(PaymentIntent.order),
                selectinload(PaymentIntent.method).selectinload(PaymentMethod.provider),
            )
        )
        return await self.session.scalar(stmt)

    async def get_by_reference(self, reference: str) -> PaymentIntent | None:
        stmt = (
            select(PaymentIntent)
            .where(PaymentIntent.reference == reference.strip().upper())
            .options(
                selectinload(PaymentIntent.order),
                selectinload(PaymentIntent.method).selectinload(PaymentMethod.provider),
            )
        )
        return await self.session.scalar(stmt)

    async def active_for_order(self, order_id: uuid.UUID) -> PaymentIntent | None:
        stmt = (
            select(PaymentIntent)
            .where(
                PaymentIntent.order_id == order_id,
                PaymentIntent.status.in_(
                    [
                        PaymentStatus.CREATED,
                        PaymentStatus.AWAITING_PAYMENT,
                        PaymentStatus.SUBMITTED,
                        PaymentStatus.DETECTING,
                        PaymentStatus.DETECTED,
                        PaymentStatus.VERIFYING,
                        PaymentStatus.PENDING_CONFIRMATION,
                    ]
                ),
            )
            .options(selectinload(PaymentIntent.method).selectinload(PaymentMethod.provider))
            .order_by(PaymentIntent.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def latest_for_order(self, order_id: uuid.UUID) -> PaymentIntent | None:
        stmt = (
            select(PaymentIntent)
            .where(PaymentIntent.order_id == order_id)
            .options(selectinload(PaymentIntent.method).selectinload(PaymentMethod.provider))
            .order_by(PaymentIntent.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def verified_for_order(self, order_id: uuid.UUID) -> PaymentIntent | None:
        stmt = (
            select(PaymentIntent)
            .where(
                PaymentIntent.order_id == order_id,
                PaymentIntent.status == PaymentStatus.VERIFIED,
            )
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def due_for_polling(self, *, limit: int = 50) -> list[PaymentIntent]:
        """Intents the verification worker should check now."""
        now = utcnow()
        stmt = (
            select(PaymentIntent)
            .where(
                PaymentIntent.status.in_(
                    [
                        PaymentStatus.SUBMITTED,
                        PaymentStatus.DETECTING,
                        PaymentStatus.DETECTED,
                        PaymentStatus.VERIFYING,
                        PaymentStatus.PENDING_CONFIRMATION,
                    ]
                ),
                or_(PaymentIntent.next_poll_at.is_(None), PaymentIntent.next_poll_at <= now),
            )
            .options(
                selectinload(PaymentIntent.order),
                selectinload(PaymentIntent.method).selectinload(PaymentMethod.provider),
            )
            .order_by(PaymentIntent.next_poll_at.nulls_first())
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def expired_open(self, *, limit: int = 100) -> list[PaymentIntent]:
        stmt = (
            select(PaymentIntent)
            .where(
                PaymentIntent.status.in_(
                    [
                        PaymentStatus.CREATED,
                        PaymentStatus.AWAITING_PAYMENT,
                        PaymentStatus.SUBMITTED,
                        PaymentStatus.DETECTING,
                    ]
                ),
                PaymentIntent.expires_at <= utcnow(),
            )
            .options(selectinload(PaymentIntent.order))
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_by_status(
        self, statuses: list[PaymentStatus], *, page: int = 1, per_page: int = 8
    ) -> Page[PaymentIntent]:
        stmt = (
            select(PaymentIntent)
            .where(PaymentIntent.status.in_(statuses))
            .options(selectinload(PaymentIntent.order))
            .order_by(PaymentIntent.created_at.desc())
        )
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def counts_by_status(self) -> dict[str, int]:
        stmt = select(PaymentIntent.status, func.count(PaymentIntent.id)).group_by(
            PaymentIntent.status
        )
        rows = await self.session.execute(stmt)
        return {
            (status.value if hasattr(status, "value") else str(status)): count
            for status, count in rows
        }

    async def search(self, query: str) -> list[PaymentIntent]:
        """Find intents by public reference or by a submitted transaction id."""
        term = query.strip()
        if not term:
            return []
        stmt = (
            select(PaymentIntent)
            .outerjoin(PaymentAttempt, PaymentAttempt.payment_intent_id == PaymentIntent.id)
            .where(
                or_(
                    PaymentIntent.reference.ilike(f"%{term}%"),
                    PaymentAttempt.submitted_txid.ilike(f"%{term}%"),
                    PaymentAttempt.submitted_reference.ilike(f"%{term}%"),
                )
            )
            .options(selectinload(PaymentIntent.order))
            .order_by(PaymentIntent.created_at.desc())
            .limit(20)
            .distinct()
        )
        return list((await self.session.scalars(stmt)).all())

    async def schedule_next_poll(self, intent: PaymentIntent, seconds: int) -> None:
        from datetime import timedelta

        intent.next_poll_at = utcnow() + timedelta(seconds=seconds)
        await self.session.flush()


class PaymentAttemptRepository(BaseRepository[PaymentAttempt]):
    model = PaymentAttempt

    async def latest_for_intent(self, intent_id: uuid.UUID) -> PaymentAttempt | None:
        stmt = (
            select(PaymentAttempt)
            .where(PaymentAttempt.payment_intent_id == intent_id)
            .order_by(PaymentAttempt.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def list_for_intent(self, intent_id: uuid.UUID) -> list[PaymentAttempt]:
        stmt = (
            select(PaymentAttempt)
            .where(PaymentAttempt.payment_intent_id == intent_id)
            .order_by(PaymentAttempt.created_at)
        )
        return list((await self.session.scalars(stmt)).all())

    async def find_by_txid(self, txid: str) -> list[PaymentAttempt]:
        stmt = select(PaymentAttempt).where(PaymentAttempt.submitted_txid == txid.strip())
        return list((await self.session.scalars(stmt)).all())


class VerificationAttemptRepository(BaseRepository[VerificationAttempt]):
    model = VerificationAttempt

    async def list_for_intent(
        self, intent_id: uuid.UUID, *, limit: int = 20
    ) -> list[VerificationAttempt]:
        stmt = (
            select(VerificationAttempt)
            .where(VerificationAttempt.payment_intent_id == intent_id)
            .order_by(VerificationAttempt.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def record(
        self,
        *,
        payment_intent_id: uuid.UUID,
        provider_code: ProviderCode,
        outcome: VerificationOutcome,
        checks: dict[str, Any] | None = None,
        payment_attempt_id: uuid.UUID | None = None,
        observed_amount: Decimal | None = None,
        observed_confirmations: int | None = None,
        external_reference: str | None = None,
        detail: str | None = None,
        duration_ms: int | None = None,
        correlation_id: str | None = None,
    ) -> VerificationAttempt:
        attempt = VerificationAttempt(
            payment_intent_id=payment_intent_id,
            payment_attempt_id=payment_attempt_id,
            provider_code=provider_code,
            outcome=outcome,
            checks=checks or {},
            observed_amount=observed_amount,
            observed_confirmations=observed_confirmations,
            external_reference=external_reference,
            detail=(detail or "")[:512] or None,
            duration_ms=duration_ms,
            correlation_id=correlation_id,
        )
        self.session.add(attempt)
        await self.session.flush()
        return attempt

    async def average_latency_ms(self, since: datetime) -> int:
        stmt = select(func.avg(VerificationAttempt.duration_ms)).where(
            VerificationAttempt.created_at >= since,
            VerificationAttempt.duration_ms.is_not(None),
        )
        value = await self.session.scalar(stmt)
        return int(value or 0)


class TransactionRepository(BaseRepository[BlockchainTransaction]):
    """Persists observed transactions for reconciliation and dispute handling."""

    model = BlockchainTransaction

    async def upsert_blockchain(
        self,
        *,
        network: NetworkCode,
        txid: str,
        log_index: int,
        asset: str,
        amount_units: int,
        amount: Decimal,
        decimals: int,
        to_address: str,
        to_address_normalized: str,
        is_successful: bool,
        observed_at: datetime,
        token_contract: str | None = None,
        from_address: str | None = None,
        memo: str | None = None,
        block_number: int | None = None,
        block_time: datetime | None = None,
        confirmations: int = 0,
        raw_payload: dict[str, Any] | None = None,
    ) -> BlockchainTransaction:
        existing = await self.session.scalar(
            select(BlockchainTransaction).where(
                BlockchainTransaction.network == network,
                BlockchainTransaction.txid == txid,
                BlockchainTransaction.log_index == log_index,
            )
        )
        if existing is not None:
            # Refresh the mutable observation fields (confirmations grow).
            existing.confirmations = max(existing.confirmations, confirmations)
            existing.is_successful = is_successful
            existing.block_number = block_number or existing.block_number
            existing.block_time = block_time or existing.block_time
            await self.session.flush()
            return existing
        record = BlockchainTransaction(
            network=network,
            txid=txid,
            log_index=log_index,
            asset=asset,
            token_contract=token_contract,
            from_address=from_address,
            to_address=to_address,
            to_address_normalized=to_address_normalized,
            amount_units=amount_units,
            amount=amount,
            decimals=decimals,
            memo=memo,
            block_number=block_number,
            block_time=block_time,
            confirmations=confirmations,
            is_successful=is_successful,
            observed_at=observed_at,
            raw_payload=raw_payload or {},
        )
        self.session.add(record)
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            return await self.upsert_blockchain(
                network=network,
                txid=txid,
                log_index=log_index,
                asset=asset,
                amount_units=amount_units,
                amount=amount,
                decimals=decimals,
                to_address=to_address,
                to_address_normalized=to_address_normalized,
                is_successful=is_successful,
                observed_at=observed_at,
                token_contract=token_contract,
                confirmations=confirmations,
            )
        return record

    async def upsert_provider(
        self,
        *,
        provider_code: ProviderCode,
        external_id: str,
        asset: str,
        amount: Decimal,
        observed_at: datetime,
        record_type: str = "deposit",
        network: str | None = None,
        status: str = "",
        txid: str | None = None,
        address: str | None = None,
        reference: str | None = None,
        confirmations: int | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> ProviderTransaction:
        existing = await self.session.scalar(
            select(ProviderTransaction).where(
                ProviderTransaction.provider_code == provider_code,
                ProviderTransaction.external_id == external_id,
            )
        )
        if existing is not None:
            existing.status = status or existing.status
            if confirmations is not None:
                existing.confirmations = confirmations
            await self.session.flush()
            return existing
        record = ProviderTransaction(
            provider_code=provider_code,
            external_id=external_id,
            record_type=record_type,
            asset=asset,
            network=network,
            amount=amount,
            status=status,
            txid=txid,
            address=address,
            reference=reference,
            confirmations=confirmations,
            observed_at=observed_at,
            raw_payload=raw_payload or {},
        )
        self.session.add(record)
        try:
            await self.session.flush()
        except IntegrityError:  # pragma: no cover - concurrent observation
            await self.session.rollback()
            return await self.upsert_provider(
                provider_code=provider_code,
                external_id=external_id,
                asset=asset,
                amount=amount,
                observed_at=observed_at,
                status=status,
            )
        return record


class PaymentConsumptionRepository(BaseRepository[PaymentConsumption]):
    """The single-consumption guard.

    :meth:`claim` is the only way a transaction becomes credited to an intent.
    """

    model = PaymentConsumption

    async def get_by_fingerprint(self, fingerprint: str) -> PaymentConsumption | None:
        stmt = select(PaymentConsumption).where(PaymentConsumption.fingerprint == fingerprint)
        return await self.session.scalar(stmt)

    async def claim(
        self,
        *,
        fingerprint: str,
        payment_intent_id: uuid.UUID,
        order_id: uuid.UUID,
        provider_code: ProviderCode,
        network: NetworkCode,
        external_id: str,
        amount: Decimal,
        amount_units: int,
        asset: str,
        correlation_id: str | None = None,
    ) -> tuple[PaymentConsumption | None, bool]:
        """Atomically claim a transaction for a payment intent.

        Returns ``(consumption, claimed)``. ``claimed`` is False when the
        transaction was already consumed - by this same intent (a safe retry)
        or by a different one (a genuine double-spend attempt). The caller must
        treat the latter as ``DUPLICATE`` and never credit the payment.

        Concurrency safety comes from the UNIQUE constraint on ``fingerprint``:
        two workers racing to claim the same transaction will see exactly one
        INSERT succeed and the other raise IntegrityError.
        """
        consumption = PaymentConsumption(
            fingerprint=fingerprint,
            payment_intent_id=payment_intent_id,
            order_id=order_id,
            provider_code=provider_code,
            network=network,
            external_id=external_id,
            amount=amount,
            amount_units=amount_units,
            asset=asset,
            consumed_at=utcnow(),
            correlation_id=correlation_id,
        )
        savepoint = await self.session.begin_nested()
        self.session.add(consumption)
        try:
            await self.session.flush()
        except IntegrityError:
            await savepoint.rollback()
            existing = await self.get_by_fingerprint(fingerprint)
            return existing, False
        await savepoint.commit()
        return consumption, True

    async def for_order(self, order_id: uuid.UUID) -> list[PaymentConsumption]:
        stmt = select(PaymentConsumption).where(PaymentConsumption.order_id == order_id)
        return list((await self.session.scalars(stmt)).all())


class ReconciliationRepository(BaseRepository[ReconciliationRecord]):
    model = ReconciliationRecord

    async def open_records(
        self, *, kind: ReconciliationKind | None = None, page: int = 1, per_page: int = 8
    ) -> Page[ReconciliationRecord]:
        stmt = (
            select(ReconciliationRecord)
            .where(
                ReconciliationRecord.status.in_(
                    [ReconciliationStatus.OPEN, ReconciliationStatus.INVESTIGATING]
                )
            )
            .order_by(ReconciliationRecord.created_at.desc())
        )
        if kind is not None:
            stmt = stmt.where(ReconciliationRecord.kind == kind)
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def open_count(self) -> int:
        stmt = select(func.count(ReconciliationRecord.id)).where(
            ReconciliationRecord.status == ReconciliationStatus.OPEN
        )
        return int((await self.session.scalar(stmt)) or 0)

    async def record(
        self,
        *,
        kind: ReconciliationKind,
        dedupe_key: str,
        summary: str,
        payment_intent_id: uuid.UUID | None = None,
        order_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> ReconciliationRecord | None:
        """Raise an anomaly, at most once per ``dedupe_key``."""
        existing = await self.session.scalar(
            select(ReconciliationRecord).where(ReconciliationRecord.dedupe_key == dedupe_key)
        )
        if existing is not None:
            return None
        record = ReconciliationRecord(
            kind=kind,
            dedupe_key=dedupe_key,
            summary=summary[:255],
            payment_intent_id=payment_intent_id,
            order_id=order_id,
            details=details or {},
        )
        self.session.add(record)
        try:
            await self.session.flush()
        except IntegrityError:  # pragma: no cover
            await self.session.rollback()
            return None
        return record

    async def resolve(
        self,
        record: ReconciliationRecord,
        *,
        resolved_by_id: uuid.UUID | None,
        note: str,
        status: ReconciliationStatus = ReconciliationStatus.RESOLVED,
    ) -> None:
        record.status = status
        record.resolved_by_id = resolved_by_id
        record.resolved_at = utcnow()
        record.resolution_note = note[:512]
        await self.session.flush()
