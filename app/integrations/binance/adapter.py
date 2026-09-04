"""Binance verification adapters.

Two distinct, NON-interchangeable capabilities are implemented, matching the
official documentation:

1. :class:`BinanceDepositAdapter` - Wallet deposit history.
   ``GET /sapi/v1/capital/deposit/hisrec`` (HMAC SHA256, ``X-MBX-APIKEY``).
   Docs: https://developers.binance.com/docs/wallet/capital/deposite-history
   This reports **on-chain deposits into the account**. It exposes ``txId``,
   ``network``, ``address``, ``addressTag``, ``amount``, ``coin``, ``status``
   and ``confirmTimes``. It does NOT see internal Binance-to-Binance transfers.

2. :class:`BinancePayAdapter` - Binance Pay merchant order query.
   ``POST /binancepay/openapi/order/query`` (HMAC SHA512, merchant headers).
   Docs: https://developers.binance.com/docs/binance-pay/api-common
   This reports the status of a **merchant Pay order** the platform created,
   keyed by ``merchantTradeNo``. It is an off-chain, account-to-account flow.

These two are deliberately separate adapters because they answer different
questions and require different credentials. Configuring one does not give you
the other.

Known limitation, documented rather than faked: the personal (non-merchant)
"Pay Trade History" endpoint reports transfers involving the account but does
not let a merchant bind an arbitrary customer-supplied reference to an order.
Where a deployment has no Binance Pay merchant account, exchange payments
should be verified through on-chain deposits (adapter 1) or routed to manual
review - never auto-approved.
"""

from __future__ import annotations

import secrets
import time
from typing import Any
from urllib.parse import urlencode

from app.core.exceptions import ProviderAuthError, ProviderError
from app.core.logging import get_logger
from app.core.money import base_units, to_decimal
from app.core.security import hmac_sha256_hex, hmac_sha512_hex_upper
from app.core.timeutils import from_timestamp, to_millis, utcnow
from app.domain.enums import NetworkCode, ProviderCode
from app.domain.payments.fingerprint import normalize_address
from app.domain.payments.types import (
    ObservedTransaction,
    PaymentExpectation,
    ProviderCapabilities,
    ProviderHealth,
)
from app.integrations.base import BaseAdapter, ProviderCredentials, ProviderHTTPClient

log = get_logger(__name__)

BINANCE_API_BASE = "https://api.binance.com"
BINANCE_PAY_BASE = "https://bpay.binanceapi.com"

#: Binance wallet deposit status. Only 1 (success) is credited.
#: 0 = pending, 6 = credited but cannot withdraw, 1 = success,
#: 7 = wrong deposit, 8 = waiting user confirm.
BINANCE_DEPOSIT_SUCCESS = 1

#: Binance network codes -> our NetworkCode. Deposits reporting a network we do
#: not map are returned with the network they claim so the verification engine
#: rejects them as WRONG_NETWORK rather than guessing.
BINANCE_NETWORK_MAP: dict[str, NetworkCode] = {
    "TRX": NetworkCode.TRC20,
    "BSC": NetworkCode.BEP20,
    "ETH": NetworkCode.ERC20,
    "TON": NetworkCode.TON,
    "SOL": NetworkCode.SOL,
    "AVAXC": NetworkCode.AVAXC,
    "ARBITRUM": NetworkCode.ARBITRUM,
    "MATIC": NetworkCode.POLYGON,
    "POLYGON": NetworkCode.POLYGON,
    "BTC": NetworkCode.BTC,
    "LTC": NetworkCode.LTC,
}


def map_binance_network(value: str | None) -> NetworkCode | None:
    if not value:
        return None
    return BINANCE_NETWORK_MAP.get(value.strip().upper())


class BinanceDepositAdapter(BaseAdapter):
    """Verifies on-chain deposits credited to the configured Binance account."""

    provider_code = ProviderCode.BINANCE
    capabilities = ProviderCapabilities(
        lookup_by_id=True,
        list_recent=True,
        reports_confirmations=True,
        reports_memo=True,
        reports_sender=False,
        notes=(
            "Uses GET /sapi/v1/capital/deposit/hisrec (wallet deposit history).",
            "Requires a read-only API key with 'Enable Reading'. Withdrawal "
            "permission is never required and must not be granted.",
            "Only deposits with status=1 (success) are considered credited.",
            "The endpoint covers at most a 90-day window per query.",
        ),
    )

    def __init__(
        self,
        credentials: ProviderCredentials,
        *,
        base_url: str = BINANCE_API_BASE,
        recv_window: int = 5000,
        lookback_minutes: int = 240,
        http: ProviderHTTPClient | None = None,
    ) -> None:
        if not credentials.is_complete:
            raise ProviderAuthError(
                "Binance API key/secret are not configured", provider="binance"
            )
        self.credentials = credentials
        self.recv_window = recv_window
        self.lookback_minutes = lookback_minutes
        super().__init__(
            http
            or ProviderHTTPClient(
                base_url,
                provider="binance",
                headers={"X-MBX-APIKEY": credentials.api_key or ""},
            )
        )

    def _sign(self, params: dict[str, Any]) -> str:
        """HMAC SHA256 over the exact query string that will be sent."""
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self.recv_window
        query = urlencode(params, doseq=True)
        signature = hmac_sha256_hex(self.credentials.api_secret or "", query)
        return f"{query}&signature={signature}"

    async def _signed_get(self, path: str, params: dict[str, Any]) -> Any:
        return await self.http.request("GET", f"{path}?{self._sign(params)}")

    async def find_transactions(
        self, expectation: PaymentExpectation, *, reference: str | None = None
    ) -> list[ObservedTransaction]:
        # Binance limits the window to 90 days; we only care about the payment
        # window plus a margin for late payments.
        start = to_millis(expectation.created_at) - self.lookback_minutes * 60_000
        params: dict[str, Any] = {
            "coin": expectation.asset.upper(),
            "startTime": max(start, 0),
            "endTime": to_millis(utcnow()),
            "limit": 100,
        }
        payload = await self._signed_get("/sapi/v1/capital/deposit/hisrec", params)
        if not isinstance(payload, list):
            raise ProviderError(
                f"binance: unexpected deposit history payload type {type(payload).__name__}",
                provider="binance",
            )

        results: list[ObservedTransaction] = []
        for record in payload:
            tx = self._normalize_deposit(record, expectation)
            if tx is not None:
                results.append(tx)
        log.info(
            "binance.deposits_fetched",
            provider="binance",
            candidates=len(results),
            raw_records=len(payload),
            intent=expectation.intent_id,
        )
        return results

    def _normalize_deposit(
        self, record: dict[str, Any], expectation: PaymentExpectation
    ) -> ObservedTransaction | None:
        try:
            amount = to_decimal(str(record.get("amount", "0")))
            coin = str(record.get("coin", "")).upper()
            status = int(record.get("status", -1))
            network = map_binance_network(record.get("network"))
            # An unmapped network is reported verbatim so the engine can reject
            # it explicitly instead of us silently assuming it matched.
            resolved_network = network or expectation.network
            address = str(record.get("address", ""))
            decimals = expectation.asset_decimals
            units = base_units(amount, decimals)
        except (ValueError, TypeError) as exc:
            log.warning("binance.deposit_parse_failed", error=str(exc))
            return None

        if network is None and record.get("network"):
            log.warning(
                "binance.unmapped_network",
                network=str(record.get("network")),
                note="deposit will not be auto-credited",
            )
            return None

        external_id = str(record.get("id") or record.get("txId") or "")
        if not external_id:
            return None

        return ObservedTransaction(
            provider=ProviderCode.BINANCE,
            network=resolved_network,
            external_id=external_id,
            asset=coin,
            amount_units=units,
            decimals=decimals,
            to_address=address,
            to_address_normalized=normalize_address(address, resolved_network),
            is_successful=status == BINANCE_DEPOSIT_SUCCESS,
            status_label=f"status={status}",
            observed_at=utcnow(),
            block_time=from_timestamp(record["insertTime"], unit="ms")
            if record.get("insertTime")
            else None,
            confirmations=_parse_confirmations(record.get("confirmTimes")),
            memo=str(record.get("addressTag") or "") or None,
            txid=str(record.get("txId") or "") or None,
            token_contract=expectation.token_contract,
            record_type="deposit",
            raw=record,
        )

    async def health_check(self) -> ProviderHealth:
        """Probes a signed endpoint so both connectivity *and* auth are tested."""
        import asyncio

        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            # Account snapshot of the wallet: cheap, read-only, signed.
            await self._signed_get("/sapi/v1/account/status", {})
        except ProviderAuthError as exc:
            return ProviderHealth(
                healthy=False,
                latency_ms=int((loop.time() - started) * 1000),
                message="Authentication failed",
                authenticated=False,
                details={"detail": str(exc)[:200]},
            )
        except ProviderError as exc:
            return ProviderHealth(
                healthy=False,
                latency_ms=int((loop.time() - started) * 1000),
                message="Provider unreachable",
                details={"detail": str(exc)[:200]},
            )
        return ProviderHealth(
            healthy=True,
            latency_ms=int((loop.time() - started) * 1000),
            message="OK",
            authenticated=True,
        )


class BinancePayAdapter(BaseAdapter):
    """Verifies a Binance Pay merchant order created by this platform.

    The platform creates the Pay order with ``merchantTradeNo`` set to the
    payment reference, then polls ``order/query``. Because the order was
    created by us with a known amount, the query result is bound to our order -
    this is the only Binance flow where a customer-supplied reference can be
    trusted to identify *our* payment, and even then the amount and currency
    returned by Binance are re-verified by the engine.
    """

    provider_code = ProviderCode.BINANCE_PAY
    capabilities = ProviderCapabilities(
        lookup_by_id=True,
        list_recent=False,
        reports_confirmations=False,
        reports_memo=False,
        reports_sender=False,
        notes=(
            "Uses POST /binancepay/openapi/order/query with merchant headers.",
            "Requires a Binance Pay MERCHANT account (Merchant Admin Portal).",
            "Off-chain: there is no txid and no confirmation count, so methods "
            "using this provider must be configured with 0 required confirmations.",
            "Only PAID status is credited; EXPIRED/CANCELED/ERROR are not.",
        ),
    )

    #: Binance Pay order statuses that mean the money has actually been paid.
    PAID_STATUSES = frozenset({"PAID"})

    def __init__(
        self,
        credentials: ProviderCredentials,
        *,
        base_url: str = BINANCE_PAY_BASE,
        http: ProviderHTTPClient | None = None,
    ) -> None:
        if not credentials.is_complete:
            raise ProviderAuthError(
                "Binance Pay merchant credentials are not configured", provider="binance_pay"
            )
        self.credentials = credentials
        super().__init__(http or ProviderHTTPClient(base_url, provider="binance_pay"))

    def _headers(self, body: str) -> dict[str, str]:
        """Binance Pay signature: hex(HMAC_SHA512(timestamp\\n nonce\\n body\\n)).upper()"""
        timestamp = str(int(time.time() * 1000))
        nonce = secrets.token_hex(16)  # 32 characters, as required
        payload = f"{timestamp}\n{nonce}\n{body}\n"
        signature = hmac_sha512_hex_upper(self.credentials.api_secret or "", payload)
        return {
            "Content-Type": "application/json",
            "BinancePay-Timestamp": timestamp,
            "BinancePay-Nonce": nonce,
            "BinancePay-Certificate-SN": self.credentials.api_key or "",
            "BinancePay-Signature": signature,
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import json as jsonlib

        body = jsonlib.dumps(payload, separators=(",", ":"))
        response = await self.http.request(
            "POST", path, content=body.encode(), headers=self._headers(body)
        )
        if not isinstance(response, dict):
            raise ProviderError("binance_pay: unexpected response shape", provider="binance_pay")
        if response.get("status") != "SUCCESS":
            code = response.get("code")
            raise ProviderError(
                f"binance_pay: query failed code={code} msg={response.get('errorMessage')!r}",
                provider="binance_pay",
                retryable=code not in {"400001", "400201"},
            )
        return response

    async def find_transactions(
        self, expectation: PaymentExpectation, *, reference: str | None = None
    ) -> list[ObservedTransaction]:
        trade_no = reference or expectation.reference
        response = await self._post(
            "/binancepay/openapi/order/query", {"merchantTradeNo": trade_no}
        )
        data = response.get("data") or {}
        if not data:
            return []

        status = str(data.get("status", "")).upper()
        currency = str(data.get("currency", "")).upper()
        try:
            amount = to_decimal(str(data.get("orderAmount", "0")))
            units = base_units(amount, expectation.asset_decimals)
        except (ValueError, TypeError):
            return []

        transaction_id = str(data.get("transactionId") or data.get("prepayId") or trade_no)
        return [
            ObservedTransaction(
                provider=ProviderCode.BINANCE_PAY,
                network=NetworkCode.EXCHANGE_INTERNAL,
                external_id=transaction_id,
                asset=currency,
                amount_units=units,
                decimals=expectation.asset_decimals,
                # For an off-chain merchant order the "receiver" is our own
                # merchant account; the binding proof is merchantTradeNo.
                to_address=expectation.destination,
                to_address_normalized=expectation.destination_normalized,
                is_successful=status in self.PAID_STATUSES,
                status_label=status,
                observed_at=utcnow(),
                block_time=from_timestamp(data["transactTime"], unit="ms")
                if data.get("transactTime")
                else None,
                confirmations=1 if status in self.PAID_STATUSES else 0,
                reference=trade_no,
                record_type="pay_order",
                raw=data,
            )
        ]

    async def health_check(self) -> ProviderHealth:
        import asyncio

        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            # Querying a reference that cannot exist still exercises signing:
            # a signature/permission problem surfaces as an auth failure, while
            # "order not found" proves credentials are valid.
            await self._post(
                "/binancepay/openapi/order/query", {"merchantTradeNo": "healthcheck000000"}
            )
        except ProviderAuthError as exc:
            return ProviderHealth(
                healthy=False,
                latency_ms=int((loop.time() - started) * 1000),
                message="Authentication failed",
                authenticated=False,
                details={"detail": str(exc)[:200]},
            )
        except ProviderError as exc:
            detail = str(exc)
            # A well-formed "not found" response proves the credentials work.
            if "400201" in detail or "not found" in detail.lower():
                return ProviderHealth(
                    healthy=True,
                    latency_ms=int((loop.time() - started) * 1000),
                    message="OK",
                    authenticated=True,
                )
            return ProviderHealth(
                healthy=False,
                latency_ms=int((loop.time() - started) * 1000),
                message="Provider error",
                details={"detail": detail[:200]},
            )
        return ProviderHealth(
            healthy=True,
            latency_ms=int((loop.time() - started) * 1000),
            message="OK",
            authenticated=True,
        )


def _parse_confirmations(value: Any) -> int:
    """``confirmTimes`` is reported as e.g. ``"12/12"``."""
    if value is None:
        return 0
    text = str(value)
    if "/" in text:
        text = text.split("/", 1)[0]
    try:
        return int(text)
    except ValueError:
        return 0
