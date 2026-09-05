"""Support ticket, audit log, broadcast and settings repositories."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.core.timeutils import utcnow
from app.db.models.support import (
    AuditLog,
    Broadcast,
    SupportMessage,
    SupportTicket,
    SystemSetting,
)
from app.db.repositories.base import BaseRepository, Page
from app.domain.enums import AuditAction, TicketStatus

OPEN_TICKET_STATES = [
    TicketStatus.OPEN,
    TicketStatus.ASSIGNED,
    TicketStatus.WAITING_USER,
]


class SupportRepository(BaseRepository[SupportTicket]):
    model = SupportTicket

    async def get_with_messages(self, ticket_id: uuid.UUID) -> SupportTicket | None:
        stmt = (
            select(SupportTicket)
            .where(SupportTicket.id == ticket_id)
            .options(selectinload(SupportTicket.messages))
        )
        return await self.session.scalar(stmt)

    async def get_by_reference(self, reference: str) -> SupportTicket | None:
        stmt = select(SupportTicket).where(SupportTicket.reference == reference.strip().upper())
        return await self.session.scalar(stmt)

    async def list_for_user(
        self, user_id: uuid.UUID, *, page: int = 1, per_page: int = 5
    ) -> Page[SupportTicket]:
        stmt = (
            select(SupportTicket)
            .where(SupportTicket.user_id == user_id)
            .order_by(SupportTicket.created_at.desc())
        )
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def list_open(
        self,
        *,
        assigned_to: uuid.UUID | None = None,
        page: int = 1,
        per_page: int = 8,
    ) -> Page[SupportTicket]:
        stmt = (
            select(SupportTicket)
            .where(SupportTicket.status.in_(OPEN_TICKET_STATES))
            .order_by(SupportTicket.priority.desc(), SupportTicket.created_at)
        )
        if assigned_to is not None:
            stmt = stmt.where(SupportTicket.assigned_to_id == assigned_to)
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def open_count(self) -> int:
        stmt = select(func.count(SupportTicket.id)).where(
            SupportTicket.status.in_(OPEN_TICKET_STATES)
        )
        return int((await self.session.scalar(stmt)) or 0)

    async def next_reference_sequence(self) -> int:
        return int((await self.session.scalar(select(func.count(SupportTicket.id)))) or 0) + 1

    async def add_message(
        self,
        ticket: SupportTicket,
        *,
        body: str,
        author_id: uuid.UUID | None,
        is_staff: bool = False,
        is_internal: bool = False,
        attachment_file_id: str | None = None,
    ) -> SupportMessage:
        message = SupportMessage(
            ticket_id=ticket.id,
            author_id=author_id,
            body=body,
            is_staff=is_staff,
            is_internal=is_internal,
            attachment_file_id=attachment_file_id,
        )
        self.session.add(message)
        ticket.last_message_at = utcnow()
        await self.session.flush()
        return message

    async def search(self, query: str, *, limit: int = 20) -> list[SupportTicket]:
        term = query.strip()
        if not term:
            return []
        stmt = (
            select(SupportTicket)
            .where(
                or_(
                    SupportTicket.reference.ilike(f"%{term}%"),
                    SupportTicket.subject.ilike(f"%{term}%"),
                )
            )
            .order_by(SupportTicket.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())


class AuditRepository(BaseRepository[AuditLog]):
    """Append-only. There is no update or delete method by design."""

    model = AuditLog

    async def record(
        self,
        *,
        action: AuditAction,
        actor_id: uuid.UUID | None,
        actor_label: str = "",
        target_type: str | None = None,
        target_id: str | None = None,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        ip_address: str | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            created_at=utcnow(),
            action=action,
            actor_id=actor_id,
            actor_label=actor_label[:96],
            target_type=target_type,
            target_id=str(target_id)[:64] if target_id else None,
            reason=(reason or "")[:512] or None,
            details=details or {},
            correlation_id=correlation_id,
            ip_address=ip_address,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def list_recent(
        self,
        *,
        action: AuditAction | None = None,
        actor_id: uuid.UUID | None = None,
        target_id: str | None = None,
        page: int = 1,
        per_page: int = 10,
    ) -> Page[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if actor_id is not None:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if target_id is not None:
            stmt = stmt.where(AuditLog.target_id == str(target_id))
        return await self.paginate(stmt, page=page, per_page=per_page)


class BroadcastRepository(BaseRepository[Broadcast]):
    model = Broadcast

    async def list_recent(self, *, limit: int = 10) -> list[Broadcast]:
        stmt = select(Broadcast).order_by(Broadcast.created_at.desc()).limit(limit)
        return list((await self.session.scalars(stmt)).all())

    async def pending(self) -> list[Broadcast]:
        from app.domain.enums import BroadcastStatus

        stmt = select(Broadcast).where(
            Broadcast.status.in_([BroadcastStatus.QUEUED, BroadcastStatus.SENDING])
        )
        return list((await self.session.scalars(stmt)).all())


class SettingsRepository(BaseRepository[SystemSetting]):
    model = SystemSetting

    async def get_value(self, key: str, default: Any = None) -> Any:
        stmt = select(SystemSetting).where(SystemSetting.key == key)
        setting = await self.session.scalar(stmt)
        if setting is None:
            return default
        return setting.value.get("value", default) if isinstance(setting.value, dict) else default

    async def set_value(
        self,
        key: str,
        value: Any,
        *,
        description: str = "",
        updated_by_id: uuid.UUID | None = None,
    ) -> SystemSetting:
        stmt = select(SystemSetting).where(SystemSetting.key == key)
        setting = await self.session.scalar(stmt)
        if setting is None:
            setting = SystemSetting(key=key, value={"value": value}, description=description)
            self.session.add(setting)
        else:
            setting.value = {"value": value}
            if description:
                setting.description = description
        setting.updated_by_id = updated_by_id
        await self.session.flush()
        return setting

    async def all_settings(self) -> dict[str, Any]:
        rows = await self.session.scalars(select(SystemSetting))
        return {
            row.key: (row.value.get("value") if isinstance(row.value, dict) else row.value)
            for row in rows
        }
