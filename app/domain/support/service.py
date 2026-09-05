"""Support ticket lifecycle."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.db.models.support import SupportMessage, SupportTicket
from app.db.models.user import User
from app.db.repositories.support import SupportRepository
from app.db.repositories.users import NotificationRepository
from app.domain.enums import (
    NotificationKind,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)

log = get_logger(__name__)

#: Payment problems are escalated automatically: money is involved.
CATEGORY_PRIORITY = {
    TicketCategory.PAYMENT: TicketPriority.HIGH,
    TicketCategory.ORDER: TicketPriority.NORMAL,
    TicketCategory.PRODUCT: TicketPriority.NORMAL,
    TicketCategory.TECHNICAL: TicketPriority.NORMAL,
    TicketCategory.OTHER: TicketPriority.LOW,
}


class SupportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.tickets = SupportRepository(session)
        self.notifications = NotificationRepository(session)

    async def create_ticket(
        self,
        *,
        user: User,
        category: TicketCategory,
        body: str,
        order_id: uuid.UUID | None = None,
        subject: str | None = None,
    ) -> SupportTicket:
        reference = await self._next_reference()
        ticket = SupportTicket(
            reference=reference,
            user_id=user.id,
            category=category,
            priority=CATEGORY_PRIORITY.get(category, TicketPriority.NORMAL),
            status=TicketStatus.OPEN,
            subject=(subject or body)[:160],
            order_id=order_id,
            last_message_at=utcnow(),
        )
        self.session.add(ticket)
        await self.session.flush()

        await self.tickets.add_message(ticket, body=body, author_id=user.id, is_staff=False)
        log.info(
            "support.ticket_created",
            ticket=reference,
            user_id=str(user.id),
            category=category.value,
        )
        return ticket

    async def _next_reference(self) -> str:
        sequence = await self.tickets.next_reference_sequence()
        for offset in range(50):
            candidate = f"TK-{1000 + sequence + offset}"
            if await self.tickets.get_by_reference(candidate) is None:
                return candidate
        return f"TK-{uuid.uuid4().hex[:8].upper()}"

    async def customer_reply(
        self, *, ticket: SupportTicket, user: User, body: str
    ) -> SupportMessage:
        if ticket.user_id != user.id:
            raise PermissionDeniedError("ticket belongs to another user")
        if ticket.status in (TicketStatus.CLOSED,):
            raise NotFoundError(
                "ticket is closed", safe_message="This ticket is closed. Please open a new one."
            )
        message = await self.tickets.add_message(
            ticket, body=body, author_id=user.id, is_staff=False
        )
        if ticket.status is TicketStatus.WAITING_USER:
            ticket.status = TicketStatus.ASSIGNED
        elif ticket.status is TicketStatus.RESOLVED:
            ticket.status = TicketStatus.OPEN
            ticket.resolved_at = None
        await self.session.flush()
        return message

    async def staff_reply(
        self,
        *,
        ticket: SupportTicket,
        staff: User,
        body: str,
        internal: bool = False,
    ) -> SupportMessage:
        message = await self.tickets.add_message(
            ticket, body=body, author_id=staff.id, is_staff=True, is_internal=internal
        )
        if not internal:
            ticket.status = TicketStatus.WAITING_USER
            await self.notifications.create(
                ticket.user_id,
                kind=NotificationKind.SUPPORT,
                title=f"Reply on ticket {ticket.reference}",
                body=body[:400],
                payload={"ticket_id": str(ticket.id)},
            )
        await self.session.flush()
        log.info(
            "support.staff_replied",
            ticket=ticket.reference,
            staff_id=str(staff.id),
            internal=internal,
        )
        return message

    async def assign(self, *, ticket: SupportTicket, staff: User) -> None:
        ticket.assigned_to_id = staff.id
        ticket.assigned_at = utcnow()
        if ticket.status is TicketStatus.OPEN:
            ticket.status = TicketStatus.ASSIGNED
        await self.session.flush()

    async def resolve(self, *, ticket: SupportTicket, staff: User) -> None:
        ticket.status = TicketStatus.RESOLVED
        ticket.resolved_at = utcnow()
        await self.notifications.create(
            ticket.user_id,
            kind=NotificationKind.SUPPORT,
            title=f"Ticket {ticket.reference} resolved",
            body="Your support ticket has been resolved.",
            payload={"ticket_id": str(ticket.id)},
        )
        await self.session.flush()
        log.info("support.resolved", ticket=ticket.reference, staff_id=str(staff.id))

    async def close(self, *, ticket: SupportTicket) -> None:
        ticket.status = TicketStatus.CLOSED
        ticket.closed_at = utcnow()
        await self.session.flush()

    @staticmethod
    def summary(ticket: SupportTicket) -> dict[str, Any]:
        return {
            "reference": ticket.reference,
            "status": ticket.status.value,
            "priority": ticket.priority.value,
            "category": ticket.category.value,
        }
