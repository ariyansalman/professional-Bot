"""Shared test fixtures.

Tests run against SQLite (aiosqlite) so the suite needs no external services.
Anything that genuinely requires PostgreSQL semantics is marked and skipped
when no ``TEST_DATABASE_URL`` is provided.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

# Configure the environment before any application module is imported.
os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("SECURITY_SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("SECURITY_API_KEY_PEPPER", "test-pepper")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test-token-for-unit-tests-only")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.db.models import Base  # noqa: E402
from app.db.session import configure_sessionmaker  # noqa: E402


@pytest_asyncio.fixture
async def engine() -> AsyncIterator:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def sessionmaker_(engine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    configure_sessionmaker(factory)
    yield factory


@pytest_asyncio.fixture
async def session(sessionmaker_) -> AsyncIterator[AsyncSession]:
    async with sessionmaker_() as session:
        yield session


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
