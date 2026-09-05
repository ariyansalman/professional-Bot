"""FastAPI application factory for the reseller API (section 122)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware.observability import (
    CorrelationMiddleware,
    SecurityHeadersMiddleware,
    register_exception_handlers,
)
from app.api.routes import health, orders, products, webhooks
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import close_redis
from app.db.session import dispose_engine

log = get_logger(__name__)

API_DESCRIPTION = """
Reseller API for the Telegram digital-commerce platform.

**Authentication** — every request needs an API key issued from the Reseller
Center in the bot:

```
Authorization: Bearer rt_live_xxxxxxxx_...
```

**Scopes** — a key only carries the scopes it was created with:
`products.read`, `orders.create`, `orders.read`, `payments.read`,
`deliveries.read`, `webhooks.manage`.

**Idempotency** — send `Idempotency-Key` on `POST /orders`. Replaying the same
key with the same body returns the original order; reusing it with a different
body is rejected with `409`.

**Rate limits** — applied per API key. Exceeding them returns `429` with a
`Retry-After` header.

**Errors** — every error is `{"error": {"code", "message", "request_id"}}`.
Quote the `request_id` when contacting support.

**Webhooks** — register an HTTPS endpoint via `POST /webhooks`. Deliveries are
signed with `X-Signature: v1=hex(hmac_sha256(secret, "<timestamp>.<event_id>.<body>"))`
and retried with exponential backoff. Verify the signature and treat
`X-Event-Id` as an idempotency key.

Direct database access is never provided.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.observability.level, settings.observability.json_output)
    log.info("api.starting", environment=settings.environment, service="api")
    yield
    await dispose_engine()
    await close_redis()
    log.info("api.stopped")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Telegram Commerce Reseller API",
        version="1.0.0",
        description=API_DESCRIPTION,
        lifespan=lifespan,
        root_path=settings.api.root_path,
        docs_url="/api/v1/docs" if settings.api.docs_enabled else None,
        redoc_url="/api/v1/redoc" if settings.api.docs_enabled else None,
        openapi_url="/api/v1/openapi.json" if settings.api.docs_enabled else None,
    )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationMiddleware)
    if settings.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        )

    register_exception_handlers(app)

    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(products.router)
    v1.include_router(orders.router)
    v1.include_router(webhooks.router)
    app.include_router(v1)
    app.include_router(health.router)

    return app


app = create_app()
