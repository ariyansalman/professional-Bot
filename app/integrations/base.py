"""Shared HTTP plumbing and the payment adapter protocol.

Every provider integration is built on :class:`ProviderHTTPClient`, which
centralises timeouts, retries with exponential backoff, rate-limit handling and
- most importantly - error normalisation. A provider's raw error body never
escapes this layer: it is wrapped in a :class:`ProviderError` whose customer
facing message is generic while the technical detail goes to the log.
"""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from app.core.exceptions import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
)
from app.core.logging import get_logger
from app.domain.enums import NetworkCode, ProviderCode
from app.domain.payments.types import (
    ObservedTransaction,
    PaymentExpectation,
    ProviderCapabilities,
    ProviderHealth,
)

log = get_logger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class ProviderCredentials:
    """Decrypted credentials, held only for the lifetime of a request."""

    api_key: str | None = None
    api_secret: str | None = None
    passphrase: str | None = None
    account_identifier: str | None = None

    @property
    def is_complete(self) -> bool:
        return bool(self.api_key and self.api_secret)


class ProviderHTTPClient:
    """Thin async HTTP wrapper with retry/backoff and error normalisation."""

    def __init__(
        self,
        base_url: str,
        *,
        provider: str,
        timeout: httpx.Timeout | None = None,
        max_retries: int = 3,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.provider = provider
        self.max_retries = max_retries
        self._external_client = client is not None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout or DEFAULT_TIMEOUT,
            headers={"User-Agent": "telegram-commerce/1.0", **(headers or {})},
            follow_redirects=False,
        )

    async def __aenter__(self) -> ProviderHTTPClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if not self._external_client:
            await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        expect_json: bool = True,
    ) -> Any:
        """Perform a request, retrying transient failures with jittered backoff."""
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    content=content,
                    headers=headers,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    await self._backoff(attempt)
                    continue
                raise ProviderUnavailableError(
                    f"{self.provider}: transport failure calling {method} {path}: {exc!r}",
                    provider=self.provider,
                ) from exc
            except httpx.HTTPError as exc:  # pragma: no cover - defensive
                raise ProviderError(
                    f"{self.provider}: HTTP error calling {method} {path}: {exc!r}",
                    provider=self.provider,
                ) from exc

            if response.status_code in _RETRYABLE_STATUS and attempt < self.max_retries:
                await self._backoff(attempt, response)
                continue

            self._raise_for_status(response, method, path)

            if not expect_json:
                return response.content
            try:
                return response.json()
            except ValueError as exc:
                raise ProviderError(
                    f"{self.provider}: non-JSON response from {method} {path}: "
                    f"{response.text[:200]!r}",
                    provider=self.provider,
                ) from exc

        raise ProviderUnavailableError(  # pragma: no cover - loop always returns/raises
            f"{self.provider}: exhausted retries calling {method} {path}: {last_error!r}",
            provider=self.provider,
        )

    def _raise_for_status(self, response: httpx.Response, method: str, path: str) -> None:
        status = response.status_code
        if status < 400:
            return
        # Body is truncated: it may echo request parameters.
        body = response.text[:400]
        if status in (401, 403):
            raise ProviderAuthError(
                f"{self.provider}: authentication rejected ({status}) on {method} {path}: {body}",
                provider=self.provider,
            )
        if status == 429:
            retry_after = response.headers.get("retry-after")
            raise ProviderRateLimitedError(
                f"{self.provider}: rate limited on {method} {path} (retry-after={retry_after})",
                provider=self.provider,
            )
        if status >= 500:
            raise ProviderUnavailableError(
                f"{self.provider}: upstream error {status} on {method} {path}: {body}",
                provider=self.provider,
            )
        raise ProviderError(
            f"{self.provider}: request rejected ({status}) on {method} {path}: {body}",
            provider=self.provider,
            retryable=False,
        )

    async def _backoff(self, attempt: int, response: httpx.Response | None = None) -> None:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    await asyncio.sleep(min(float(retry_after), 30.0))
                    return
                except ValueError:
                    pass
        delay = min(2**attempt * 0.5, 8.0)
        await asyncio.sleep(delay + random.uniform(0, delay / 2))  # noqa: S311 - jitter only


@runtime_checkable
class PaymentAdapter(Protocol):
    """What every payment integration must provide.

    Adapters *observe*; they never decide. They return normalised
    :class:`ObservedTransaction` objects and the verification engine decides
    whether any of them satisfies the expectation.
    """

    provider_code: ProviderCode
    capabilities: ProviderCapabilities

    async def find_transactions(
        self, expectation: PaymentExpectation, *, reference: str | None = None
    ) -> list[ObservedTransaction]:
        """Return candidate incoming transfers for this expectation."""
        ...

    async def health_check(self) -> ProviderHealth:
        """Probe connectivity and authentication."""
        ...

    async def aclose(self) -> None:
        ...


class BaseAdapter(ABC):
    """Common adapter behaviour: timing, logging and lifecycle."""

    provider_code: ProviderCode
    capabilities: ProviderCapabilities

    def __init__(self, http: ProviderHTTPClient) -> None:
        self.http = http

    @abstractmethod
    async def find_transactions(
        self, expectation: PaymentExpectation, *, reference: str | None = None
    ) -> list[ObservedTransaction]:
        ...

    @abstractmethod
    async def health_check(self) -> ProviderHealth:
        ...

    async def aclose(self) -> None:
        await self.http.aclose()

    def supports_network(self, network: NetworkCode) -> bool:
        return True

    async def _timed(self, coro: Any) -> tuple[Any, int]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await coro
        return result, int((loop.time() - started) * 1000)
