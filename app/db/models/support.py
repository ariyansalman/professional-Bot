"""Support tickets, broadcasts, audit logs and system settings."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    Base,
    BigIntPrimaryKey,
    GUID,
    TZDateTime,
    TimestampMixin,
    UUIDPrimaryKey,
)
from app.domain.enums import (
    AuditAction,
    BroadcastAudience,
    BroadcastStatus,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)


class SupportTicket(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        UniqueConstraint("reference", name="uq_support_tickets_reference"),
        Index("ix_support_tickets_status_priority", "status", "priority"),
        Index("ix_support_tickets_user", "user_id", "status"),
    )

    reference: Mapped[str] = mapped_column(String(24), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[TicketCategory] = mapped_column(
        Enum(TicketCategory, native_enum=False, length=16), default=TicketCategory.OTHER
    )
    priority: Mapped[TicketPriority] = mapped_column(
        Enum(TicketPriority, native_enum=False, length=16), default=TicketPriority.NORMAL
    )
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, native_enum=False, length=16), default=TicketStatus.OPEN
    )
    subject: Mapped[str] = mapped_column(String(160), default="")
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("orders.id", ondelete="SET NULL"), default=None
    )
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    closed_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    last_message_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None, index=True)
    internal_notes: Mapped[str | None] = mapped_column(Text, default=None)

    messages: Mapped[list[SupportMessage]] = relationship(
        back_populates="ticket",
        lazy="noload",
        order_by="SupportMessage.created_at",
        cascade="all, delete-orphan",
    )


class SupportMessage(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "support_messages"
    __table_args__ = (Index("ix_support_messages_ticket", "ticket_id", "created_at"),)

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Staff-only note, never shown to the customer.
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    attachment_file_id: Mapped[str | None] = mapped_column(String(255), default=None)

    ticket: Mapped[SupportTicket] = relationship(back_populates="messages", lazy="noload")


class Broadcast(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "broadcasts"

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    audience: Mapped[BroadcastAudience] = mapped_column(
        Enum(BroadcastAudience, native_enum=False, length=16), default=BroadcastAudience.ALL
    )
    audience_filter: Mapped[dict[str, Any]] = mapped_column(default=dict)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[BroadcastStatus] = mapped_column(
        Enum(BroadcastStatus, native_enum=False, length=16), default=BroadcastStatus.DRAFT
    )
    total_recipients: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    blocked_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    #: Cursor so a crashed broadcast resumes instead of restarting.
    cursor: Mapped[str | None] = mapped_column(String(64), default=None)


class AuditLog(UUIDPrimaryKey, Base):
    """Append-only. Every high-risk admin action lands here."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_actor_created", "actor_id", "created_at"),
        Index("ix_audit_logs_action_created", "action", "created_at"),
        Index("ix_audit_logs_target", "target_type", "target_id"),
    )

    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False, index=True)
    action: Mapped[AuditAction] = mapped_column(
        Enum(AuditAction, native_enum=False, length=48), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    actor_label: Mapped[str] = mapped_column(String(96), default="")
    target_type: Mapped[str | None] = mapped_column(String(48), default=None)
    target_id: Mapped[str | None] = mapped_column(String(64), default=None)
    reason: Mapped[str | None] = mapped_column(String(512), default=None)
    #: Before/after snapshot with secrets already redacted by the service.
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)
    ip_address: Mapped[str | None] = mapped_column(String(64), default=None)


class SystemSetting(BigIntPrimaryKey, TimestampMixin, Base):
    """Runtime-editable settings (maintenance mode, texts, policy knobs)."""

    __tablename__ = "system_settings"
    __table_args__ = (UniqueConstraint("key", name="uq_system_settings_key"),)

    key: Mapped[str] = mapped_column(String(96), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    description: Mapped[str] = mapped_column(String(255), default="")
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
