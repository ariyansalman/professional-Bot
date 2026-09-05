"""Async engine / session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def create_engine() -> AsyncEngine:
    settings = get_settings()
    db = settings.database
    kwargs: dict[str, Any] = {"echo": db.echo, "pool_pre_ping": True, "future": True}
    if db.is_sqlite:
        # SQLite (tests) does not support the pooling options below.
        kwargs.pop("pool_pre_ping")
    else:
        kwargs.update(
            pool_size=db.pool_size,
            max_overflow=db.max_overflow,
            pool_timeout=db.pool_timeout,
            pool_recycle=db.pool_recycle,
            connect_args={
                # Supabase's transaction-mode pooler rejects prepared statements.
                "statement_cache_size": db.statement_cache_size,
                "prepared_statement_cache_size": db.statement_cache_size,
                "timeout": db.connect_timeout,
                "server_settings": {"application_name": settings.app_name},
            },
        )
    return create_async_engine(db.dsn, **kwargs)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
            class_=AsyncSession,
        )
    return _sessionmaker


def configure_sessionmaker(factory: async_sessionmaker[AsyncSession]) -> None:
    """Override the factory (used by the test suite)."""
    global _sessionmaker
    _sessionmaker = factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commits on success, rolls back on any exception."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def read_session() -> AsyncIterator[AsyncSession]:
    """Read-only scope: never commits."""
    factory = get_sessionmaker()
    async with factory() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        log.info("database.engine_disposed")
    _engine = None
    _sessionmaker = None
