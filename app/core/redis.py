"""Redis connectivity, distributed locks, rate limiting and dedupe helpers.

Redis is used for coordination and ephemeral state only. PostgreSQL remains the
source of truth for anything financial: if Redis is wiped, the platform loses
caches and lock hints but no payment, order or delivery state.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio.client import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.exceptions import RateLimitedError
from app.core.logging import get_logger

log = get_logger(__name__)

_redis: Redis | None = None

#: Atomic compare-and-delete so a lock is only released by its owner.
_RELEASE_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

#: Fixed-window counter that sets the TTL on first increment.
_RATE_LIMIT = """
local current = redis.call('incr', KEYS[1])
if current == 1 then
    redis.call('expire', KEYS[1], ARGV[1])
end
return {current, redis.call('ttl', KEYS[1])}
"""


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = aioredis.from_url(
            settings.redis.dsn,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=settings.redis.socket_timeout,
            socket_connect_timeout=settings.redis.socket_timeout,
            max_connections=settings.redis.max_connections,
            health_check_interval=30,
        )
    return _redis


def configure_redis(client: Redis) -> None:
    """Inject a client (used by tests and by fakeredis-based fixtures)."""
    global _redis
    _redis = client


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


def namespaced(*parts: Any) -> str:
    prefix = get_settings().redis.namespace
    return ":".join([prefix, *(str(part) for part in parts)])


class LockAcquisitionError(Exception):
    """The lock is held by another worker."""


class DistributedLock:
    """A Redis lock with an owner token and a safe release.

    Used to serialise verification of a single payment intent across workers.
    Correctness never *depends* on the lock: the database constraints are the
    real guarantee. The lock only avoids wasted duplicate provider calls.
    """

    def __init__(
        self,
        key: str,
        *,
        ttl: int = 60,
        client: Redis | None = None,
    ) -> None:
        self.key = namespaced("lock", key)
        self.ttl = ttl
        self.token = secrets.token_hex(16)
        self._client = client

    @property
    def client(self) -> Redis:
        return self._client or get_redis()

    async def acquire(self, *, blocking: bool = False, wait_seconds: float = 5.0) -> bool:
        """Try to take the lock, optionally waiting up to ``wait_seconds``."""
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            try:
                acquired = await self.client.set(self.key, self.token, nx=True, ex=self.ttl)
            except RedisError as exc:
                # Redis is down: proceed without the advisory lock rather than
                # halting payments. Database constraints still protect us.
                log.warning("redis.lock_unavailable", key=self.key, error=str(exc))
                return True
            if acquired:
                return True
            if not blocking or asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def release(self) -> None:
        try:
            await self.client.eval(_RELEASE_LOCK, 1, self.key, self.token)
        except RedisError as exc:  # pragma: no cover - best effort
            log.warning("redis.lock_release_failed", key=self.key, error=str(exc))

    async def extend(self, ttl: int | None = None) -> None:
        try:
            await self.client.expire(self.key, ttl or self.ttl)
        except RedisError:  # pragma: no cover
            pass


@asynccontextmanager
async def distributed_lock(
    key: str, *, ttl: int = 60, blocking: bool = False, wait_seconds: float = 5.0
) -> AsyncIterator[bool]:
    """Yield True when the lock was acquired, False when another worker holds it."""
    lock = DistributedLock(key, ttl=ttl)
    acquired = await lock.acquire(blocking=blocking, wait_seconds=wait_seconds)
    try:
        yield acquired
    finally:
        if acquired:
            await lock.release()


async def rate_limit(
    key: str,
    limit: int,
    window_seconds: int,
    *,
    client: Redis | None = None,
    raise_on_limit: bool = True,
) -> tuple[bool, int]:
    """Fixed-window rate limit.

    Returns ``(allowed, retry_after_seconds)``. When Redis is unavailable the
    request is allowed: rate limiting is a protection, not a correctness
    requirement, and failing closed would take the whole platform down with it.
    """
    redis_client = client or get_redis()
    full_key = namespaced("ratelimit", key)
    try:
        current, ttl = await redis_client.eval(_RATE_LIMIT, 1, full_key, window_seconds)
    except RedisError as exc:
        log.warning("redis.rate_limit_unavailable", key=key, error=str(exc))
        return True, 0
    retry_after = int(ttl) if int(ttl) > 0 else window_seconds
    if int(current) > limit:
        if raise_on_limit:
            raise RateLimitedError(
                f"rate limit exceeded for {key}: {current}/{limit}", retry_after=retry_after
            )
        return False, retry_after
    return True, 0


async def dedupe(key: str, ttl: int = 3600, *, client: Redis | None = None) -> bool:
    """Return True the first time a key is seen within ``ttl``.

    Used to suppress duplicate Telegram callback processing. It is a UX
    optimisation only; financial idempotency lives in the database.
    """
    redis_client = client or get_redis()
    try:
        return bool(await redis_client.set(namespaced("dedupe", key), "1", nx=True, ex=ttl))
    except RedisError:
        return True


async def cache_get(key: str, *, client: Redis | None = None) -> str | None:
    try:
        # The client is created with decode_responses=True, so values are str.
        value = await (client or get_redis()).get(namespaced("cache", key))
    except RedisError:
        return None
    return value if value is None else str(value)


async def cache_set(
    key: str, value: str, ttl: int = 60, *, client: Redis | None = None
) -> None:
    try:
        await (client or get_redis()).set(namespaced("cache", key), value, ex=ttl)
    except RedisError:  # pragma: no cover - cache is best effort
        pass


async def cache_delete(pattern: str, *, client: Redis | None = None) -> None:
    """Invalidate cache keys matching a pattern (SCAN-based, never KEYS)."""
    redis_client = client or get_redis()
    try:
        async for key in redis_client.scan_iter(match=namespaced("cache", pattern), count=200):
            await redis_client.delete(key)
    except RedisError:  # pragma: no cover
        pass


async def redis_health() -> tuple[bool, str]:
    try:
        pong = await get_redis().ping()
        return bool(pong), "OK"
    except RedisError as exc:
        return False, str(exc)[:120]
