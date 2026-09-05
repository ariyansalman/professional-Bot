"""Correlation ids, access logging and uniform error handling."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.correlation import correlation_scope, get_correlation_id
from app.core.exceptions import AppError
from app.core.logging import get_logger

log = get_logger(__name__)


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Bind a correlation id to the request and echo it back."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id")
        with correlation_scope(incoming, prefix="api") as correlation_id:
            request.state.correlation_id = correlation_id
            started = time.perf_counter()
            response = await call_next(request)
            duration_ms = int((time.perf_counter() - started) * 1000)
            response.headers["X-Request-ID"] = correlation_id
            log.info(
                "api.request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
                principal=getattr(request.state, "principal_id", None),
            )
            return response


def register_exception_handlers(app) -> None:
    """Map every error to a safe JSON envelope.

    An unexpected exception is logged with its traceback and returned as a
    generic 500: internal detail, SQL text and provider payloads never leave
    the process.
    """
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        log.warning(
            "api.app_error",
            code=exc.code,
            detail=exc.detail[:400],
            path=request.url.path,
            context=exc.context,
        )
        headers = {}
        retry_after = getattr(exc, "retry_after", None)
        if retry_after:
            headers["Retry-After"] = str(retry_after)
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.safe_message,
                    "request_id": get_correlation_id(),
                }
            },
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's error list is safe: it describes the caller's own payload.
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "The request body failed validation.",
                    "request_id": get_correlation_id(),
                    "fields": [
                        {"field": ".".join(str(p) for p in err["loc"][1:]), "issue": err["msg"]}
                        for err in exc.errors()[:10]
                    ],
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": f"http_{exc.status_code}",
                    "message": str(exc.detail),
                    "request_id": get_correlation_id(),
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("api.unhandled_error", path=request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected error occurred.",
                    "request_id": get_correlation_id(),
                }
            },
        )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Conservative security headers for the API and its docs."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
        return response
