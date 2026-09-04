"""SQLAlchemy declarative base, shared column types and mixins."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, MetaData, Numeric, String, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator

from app.core.money import MONEY_PRECISION, MONEY_SCALE

# Deterministic constraint names keep Alembic autogenerate stable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class JSONB(TypeDecorator):
    """JSONB on PostgreSQL, plain JSON elsewhere (tests run on SQLite)."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.JSONB())
        return dialect.type_descriptor(JSON())


class GUID(TypeDecorator):
    """UUID on PostgreSQL, 36-char string elsewhere."""

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


#: NUMERIC(30, 8): exact money. ``asdecimal`` keeps values as Decimal.
Money = Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True)

#: On-chain base-unit amounts can exceed 64 bits (uint256), so they are stored
#: as an unscaled NUMERIC and handled as Python ints.
BaseUnits = Numeric(78, 0, asdecimal=False)

TZDateTime = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        Decimal: Money,
        datetime: TZDateTime,
        uuid.UUID: GUID,
        int: Integer,
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4, sort_order=-100
    )


class BigIntPrimaryKey:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, sort_order=-100)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False, index=True, sort_order=100
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        sort_order=101,
    )


class SoftDeleteMixin:
    """Soft deletion. Financial and order-linked rows are never hard-deleted."""

    deleted_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None, sort_order=102)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
