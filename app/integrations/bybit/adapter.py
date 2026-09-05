"""Bybit V5 deposit verification.

Two documented record sources are used, and they are kept distinct:

* on-chain deposits  - ``GET /v5/asset/deposit/query-record``
  Docs: https://bybit-exchange.github.io/docs/v5/asset/deposit/deposit-record
  Reports ``coin``, ``chain``, ``amount``, ``txID``, ``status``, ``toAddress``,
  ``tag``, ``confirmations``, ``successAt``.

* internal (off-chain) deposits - ``GET /v5/asset/deposit/query-internal-record``
  Docs: https://bybit-exchange.github.io/docs/v5/asset/deposit/internal-deposit-record
  Bybit-to-Bybit transfers. These have no chain and no confirmations, so a
  payment method relying on them must be configured with 0 required
  confirmations and ``EXCHANGE_INTERNAL`` as its network.

Authentication (V5): ``X-BAPI-SIGN`` = HMAC_SHA256(secret,
``timestamp + api_key + recv_window + queryString``) with headers
``X-BAPI-API-KEY``, ``X-BAPI-TIMESTAMP``, ``X-BAPI-RECV-WINDOW``.
A read-only key is sufficient; withdrawal permission must not be granted.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlencode

from app.core.exceptions import ProviderAuthError, ProviderError
from app.core.logging import get_logger
from app.core.money import base_units, to_decimal
from app.core.security import hmac_sha256_hex
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

BYBIT_API_BASE = "https://api.bybit.com"

#: Bybit deposit status: 0 unknown, 1 toBeConfirmed, 2 processing,
#: 3 success, 4 deposit failed, 10011 pending to be credited,
#: 10012 crediting. Only 3 is final success.
BYBIT_DEPOSIT_SUCCESS = 3

BYBIT_CHAIN_MAP: dict[str, NetworkCode] = {
    "TRX": NetworkCode.TRC20,
    "TRC20": NetworkCode.TRC20,
    "BSC": NetworkCode.BEP20,
    "BEP20": NetworkCode.BEP20,
    "ETH": NetworkCode.ERC20,
    "ERC20": NetworkCode.ERC20,
    "TON": NetworkCode.TON,
    "SOL": NetworkCode.SOL,
    "CAVAX": NetworkCode.AVAXC,
    "AVAXC": NetworkCode.AVAXC,
    "ARBI": NetworkCode.ARBITRUM,
    "ARBITRUM": NetworkCode.ARBITRUM,
    "MATIC": NetworkCode.POLYGON,
    "POLYGON": NetworkCode.POLYGON,
    "BTC": NetworkCode.BTC,
    "LTC": NetworkCode.LTC,
}


def map_bybit_chain(value: str | None) -> NetworkCode | None:
    if not value:
        return None
    return BYBIT_CHAIN_MAP.get(value.strip().upper())


class BybitAdapter(BaseAdapter):
    provider_code = ProviderCode.BYBIT
    capabilities = ProviderCapabilities(
        lookup_by_id=True,
        list_recent=True,
        reports_confirmations=True,
        reports_memo=True,
        reports_sender=False,
        notes=(
            "Uses GET /v5/asset/deposit/query-record (on-chain deposits).",
            "Also reads GET /v5/asset/deposit/query-internal-record for "
            "Bybit-to-Bybit transfers when the method's network is exchange_internal.",
            "Requires a read-only API key (Assets: read). Withdrawal permission "
            "is never required and must not be granted.",
            "Only deposit status 3 (success) is credited.",
        ),
    )

    def __init__(
        self,
        credentials: ProviderCredentials,
        *,
        base_url: str = BYBIT_API_BASE,
        recv_window: int = 5000,
        lookback_minutes: int = 240,
        http: ProviderHTTPClient | None = None,
    ) -> None:
        if not credentials.is_complete:
            raise ProviderAuthError("Bybit API key/secret are not configured", provider="bybit")
        self.credentials = credentials
        self.recv_window = recv_window
        self.lookback_minutes = lookback_minutes
        super().__init__(http or ProviderHTTPClient(base_url, provider="bybit"))

    def _headers(self, query: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        recv = str(self.recv_window)
        payload = f"{timestamp}{self.credentials.api_key}{recv}{query}"
        return {
            "X-BAPI-API-KEY": self.credentials.api_key or "",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv,
            "X-BAPI-SIGN": hmac_sha256_hex(self.credentials.api_secret or "", payload),
        }

    async def _signed_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urlencode(sorted(params.items()))
        url = f"{path}?{query}" if query else path
        payload = await self.http.request("GET", url, headers=self._headers(query))
        if not isinstance(payload, dict):
            raise ProviderError("bybit: unexpected response shape", provider="bybit")
        ret_code = payload.get("retCode")
        if ret_code != 0:
            # 10003/10004/10005 are key/signature/permission problems.
            auth_codes = {10003, 10004, 10005, 10016, 33004}
            message = f"bybit: retCode={ret_code} retMsg={payload.get('retMsg')!r}"
            if ret_code in auth_codes:
                raise ProviderAuthError(message, provider="bybit")
            raise ProviderError(message, provider="bybit")
        return payload.get("result") or {}

    async def find_transactions(
        self, expectation: PaymentExpectation, *, reference: str | None = None
    ) -> list[ObservedTransaction]:
        if expectation.network is NetworkCode.EXCHANGE_INTERNAL:
            return await self._internal_deposits(expectation)
        return await self._onchain_deposits(expectation)

    async def _onchain_deposits(
        self, expectation: PaymentExpectation
    ) -> list[ObservedTransaction]:
        start = to_millis(expectation.created_at) - self.lookback_minutes * 60_000
        result = await self._signed_get(
            "/v5/asset/deposit/query-record",
            {
                "coin": expectation.asset.upper(),
                "startTime": max(start, 0),
                "endTime": to_millis(utcnow()),
                "limit": 50,
            },
        )
        rows = result.get("rows") or []
        transactions: list[ObservedTransaction] = []
        for row in rows:
            tx = self._normalize_onchain(row, expectation)
            if tx is not None:
                transactions.append(tx)
        log.info(
            "bybit.deposits_fetched",
            provider="bybit",
            candidates=len(transactions),
            raw_records=len(rows),
            intent=expectation.intent_id,
        )
        return transactions

    def _normalize_onchain(
        self, row: dict[str, Any], expectation: PaymentExpectation
    ) -> ObservedTransaction | None:
        chain = map_bybit_chain(row.get("chain"))
        if chain is None:
            log.warning("bybit.unmapped_chain", chain=str(row.get("chain")))
            return None
        try:
            amount = to_decimal(str(row.get("amount", "0")))
            units = base_units(amount, expectation.asset_decimals)
            status = int(row.get("status", -1))
        except (ValueError, TypeError) as exc:
            log.warning("bybit.deposit_parse_failed", error=str(exc))
            return None

        txid = str(row.get("txID") or "")
        if not txid:
            # Bybit briefly returns rows without a txID while broadcasting.
            return None
        address = str(row.get("toAddress") or "")
        success_at = row.get("successAt")
        return ObservedTransaction(
            provider=ProviderCode.BYBIT,
            network=chain,
            external_id=txid,
            asset=str(row.get("coin", "")).upper(),
            amount_units=units,
            decimals=expectation.asset_decimals,
            to_address=address,
            to_address_normalized=normalize_address(address, chain),
            is_successful=status == BYBIT_DEPOSIT_SUCCESS,
            status_label=f"status={status}",
            observed_at=utcnow(),
            block_time=from_timestamp(success_at, unit="ms") if success_at else None,
            confirmations=int(row.get("confirmations") or 0),
            memo=str(row.get("tag") or "") or None,
            txid=txid,
            token_contract=expectation.token_contract,
            record_type="deposit",
            raw=row,
        )

    async def _internal_deposits(
        self, expectation: PaymentExpectation
    ) -> list[ObservedTransaction]:
        start = to_millis(expectation.created_at) - self.lookback_minutes * 60_000
        result = await self._signed_get(
            "/v5/asset/deposit/query-internal-record",
            {
                "coin": expectation.asset.upper(),
                "startTime": max(start, 0),
                "endTime": to_millis(utcnow()),
                "limit": 50,
            },
        )
        transactions: list[ObservedTransaction] = []
        for row in result.get("rows") or []:
            try:
                amount = to_decimal(str(row.get("amount", "0")))
                units = base_units(amount, expectation.asset_decimals)
                status = int(row.get("status", -1))
            except (ValueError, TypeError):
                continue
            record_id = str(row.get("id") or row.get("txID") or "")
            if not record_id:
                continue
            created = row.get("createdTime")
            transactions.append(
                ObservedTransaction(
                    provider=ProviderCode.BYBIT,
                    network=NetworkCode.EXCHANGE_INTERNAL,
                    external_id=record_id,
                    asset=str(row.get("coin", "")).upper(),
                    amount_units=units,
                    decimals=expectation.asset_decimals,
                    to_address=expectation.destination,
                    to_address_normalized=expectation.destination_normalized,
                    # Internal transfer status: 1 processing, 2 success, 3 failed.
                    is_successful=status == 2,
                    status_label=f"status={status}",
                    observed_at=utcnow(),
                    block_time=from_timestamp(created, unit="ms") if created else None,
                    confirmations=1 if status == 2 else 0,
                    record_type="internal_deposit",
                    raw=row,
                )
            )
        return transactions

    async def health_check(self) -> ProviderHealth:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            # Signed, read-only, cheap: proves connectivity + key validity.
            await self._signed_get("/v5/account/info", {})
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
