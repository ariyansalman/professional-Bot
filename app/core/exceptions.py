"""Application exception hierarchy.

Rule: internal detail never reaches the customer. Every exception carries a
machine ``code`` (used by the API and for metrics) and a ``safe_message``
suitable for display. The original technical cause stays in ``detail`` and is
only ever written to the structured log.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every expected application error."""

    code = "internal_error"
    http_status = 500
    safe_message = "Something went wrong. Please try again."

    def __init__(
        self,
        detail: str | None = None,
        *,
        safe_message: str | None = None,
        code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.detail = detail or self.__class__.safe_message
        if safe_message:
            self.safe_message = safe_message
        if code:
            self.code = code
        self.context = context or {}
        super().__init__(self.detail)

    def as_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.safe_message}}


class ConfigurationError(AppError):
    code = "configuration_error"
    safe_message = "This feature is not configured yet."


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404
    safe_message = "The requested item could not be found."


class ValidationError(AppError):
    code = "validation_error"
    http_status = 422
    safe_message = "The submitted data is not valid."


class ConflictError(AppError):
    code = "conflict"
    http_status = 409
    safe_message = "This action conflicts with the current state."


class AuthenticationError(AppError):
    code = "unauthenticated"
    http_status = 401
    safe_message = "Authentication is required."


class PermissionDeniedError(AppError):
    code = "permission_denied"
    http_status = 403
    safe_message = "You do not have permission to perform this action."


class RateLimitedError(AppError):
    code = "rate_limited"
    http_status = 429
    safe_message = "Too many requests. Please slow down."

    def __init__(self, *args: Any, retry_after: int = 1, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class MaintenanceError(AppError):
    code = "maintenance"
    http_status = 503
    safe_message = "The service is temporarily under maintenance."


# --- Domain-specific ------------------------------------------------------


class InvalidStateTransition(ConflictError):
    code = "invalid_state_transition"
    safe_message = "This action is not available for the current status."


class OutOfStockError(ConflictError):
    code = "out_of_stock"
    safe_message = "This product is currently unavailable."


class InsufficientStockError(OutOfStockError):
    code = "insufficient_stock"
    safe_message = "There is not enough stock for the requested quantity."


class CouponError(ValidationError):
    code = "coupon_invalid"
    safe_message = "The coupon is expired, invalid, or unavailable for this order."


class PaymentError(AppError):
    code = "payment_error"
    safe_message = "The payment could not be processed."


class PaymentExpiredError(PaymentError):
    code = "payment_expired"
    safe_message = "The payment window has expired."


class DuplicateTransactionError(PaymentError):
    code = "duplicate_transaction"
    http_status = 409
    safe_message = "This transaction has already been used for another order."


class ProviderError(AppError):
    """A payment provider / RPC endpoint failed.

    Never surfaced verbatim: the provider payload is kept in ``detail``.
    """

    code = "provider_error"
    http_status = 502
    safe_message = "We could not reach the payment provider. Please try again."

    def __init__(
        self,
        detail: str | None = None,
        *,
        provider: str | None = None,
        retryable: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(detail, **kwargs)
        self.provider = provider
        self.retryable = retryable


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"


class ProviderAuthError(ProviderError):
    code = "provider_auth_error"

    def __init__(self, detail: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("retryable", False)
        super().__init__(detail, **kwargs)


class ProviderRateLimitedError(ProviderError):
    code = "provider_rate_limited"


class TransactionNotFoundError(ProviderError):
    code = "transaction_not_found"
    http_status = 404
    safe_message = "We could not find that transaction yet."

    def __init__(self, detail: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("retryable", True)
        super().__init__(detail, **kwargs)


class UnsupportedCapabilityError(AppError):
    """Raised when a provider genuinely cannot support a requested capability.

    We never emulate an unsupported capability; we fail loudly and route the
    payment to manual review instead.
    """

    code = "unsupported_capability"
    http_status = 501
    safe_message = "This payment method requires manual verification."


class IdempotencyConflictError(ConflictError):
    code = "idempotency_conflict"
    safe_message = "A different request was already submitted with this idempotency key."
