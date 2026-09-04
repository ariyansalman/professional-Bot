"""OKX V5 deposit verification.

Endpoint: ``GET /api/v5/asset/deposit-history``
Docs: https://www.okx.com/docs-v5/en/  (Funding Account -> Get deposit history)

Documented response fields used here: ``depId``, ``txId``, ``ccy``, ``chain``,
``amt``, ``to``, ``ts``, ``state``, ``actualDepBlkConfirm``, ``from``.

Authentication requires four headers:
``OK-ACCESS-KEY``, ``OK-ACCESS-SIGN`` = base64(HMAC_SHA256(secret,
``timestamp + method + requestPath + body``)), ``OK-ACCESS-TIMESTAMP``
(ISO-8601 with milliseconds, e.g. ``2020-12-08T09:08:57.715Z``) and
``OK-ACCESS-PASSPHRASE``. A read-only key is sufficient.

Deposit ``state`` semantics differ between currencies and have changed across
API revisions, so the states treated as *credited* are configurable per
deployment (``config.credited_states``) with a conservative documented default.
The engine additionally enforces the configured confirmation requirement, so a
state mapping alone can never credit an under-confirmed deposit.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from app.core.exceptions import ProviderAuthError, ProviderError
from app.core.logging import get_logger
from app.core.money import base_units, to_decimal
from app.core.security import hmac_sha256_b64
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

OKX_API_BASE = "https://www.okx.com"

#: Documented deposit states: "0" waiting for confirmation,
#: "1" deposit credited, "2" deposit successful, "8" pending due to temporary
#: deposit suspension, "11" match the address blacklist, "12" account frozen,
#: "13" sub-account deposit interception, "14" KYC limit.
#: Default: only "2" (deposit successful) is treated as final.
DEFAULT_CREDITED_STATES = ("2",)

#: OKX reports chains as "USDT-TRC20", "USDT-ERC20", "USDT-Arbitrum One", ...
OKX_CHAIN_SUFFIX_MAP: dict[str, NetworkCode] = {
    "TRC20": NetworkCode.TRC20,
    "TRX": NetworkCode.TRC20,
    "BSC": NetworkCode.BEP20,
    "BEP20": NetworkCode.BEP20,
    "ERC20": NetworkCode.ERC20,
    "ETH": NetworkCode.ERC20,
    "TON": NetworkCode.TON,
    "SOL": NetworkCode.SOL,
    "SOLANA": NetworkCode.SOL,
    "AVAX C-CHAIN": NetworkCode.AVAXC,
    "AVAXC": NetworkCode.AVAXC,
    "ARBITRUM ONE": NetworkCode.ARBITRUM,
    "ARBITRUM": NetworkCode.ARBITRUM,
    "POLYGON": NetworkCode.POLYGON,
    "MATIC": NetworkCode.POLYGON,
    "BTC": NetworkCode.BTC,
    "LTC": NetworkCode.LTC,
}


def map_okx_chain(value: str | None) -> NetworkCode | None:
    """``"USDT-Arbitrum One"`` -> ``NetworkCode.ARBITRUM``."""
    if not value:
        return None
    suffix = value.split("-", 1)[1] if "-" in value else value
    return OKX_CHAIN_SUFFIX_MAP.get(suffix.strip().upper())


def okx_timestamp() -> str:
    """ISO-8601 UTC with milliseconds, as the signature requires."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(UTC).microsecond // 1000:03d}Z"


class OKXAdapter(BaseAdapter):
    provider_code = ProviderCode.OKX
    capabilities = ProviderCapabilities(
        lookup_by_id=True,
        list_recent=True,
        reports_confirmations=True,
        reports_memo=False,
        reports_sender=True,
        notes=(
            "Uses GET /api/v5/asset/deposit-history.",
            "Requires a read-only API key with a passphrase (Read permission "
            "on the funding account). Withdraw permission must not be granted.",
            "Deposit states counted as credited are configurable; the default "
            "is state '2' (deposit successful) only.",
            "The endpoint does not report a deposit memo/tag, so OKX methods "
            "must not rely on memo matching.",
        ),
    )

    def __init__(
        self,
        credentials: ProviderCredentials,
        *,
        base_url: str = OKX_API_BASE,
        lookback_minutes: int = 240,
        credited_states: tuple[str, ...] = DEFAULT_CREDITED_STATES,
        http: ProviderHTTPClient | None = None,
    ) -> None:
        if not credentials.is_complete or not credentials.passphrase:
            raise ProviderAuthError(
                "OKX requires api key, secret and passphrase", provider="okx"
            )
        self.credentials = credentials
        self.lookback_minutes = lookback_minutes
        self.credited_states = tuple(credited_states) or DEFAULT_CREDITED_STATES
        super().__init__(http or ProviderHTTPClient(base_url, provider="okx"))

    def _headers(self, method: str, request_path: str, body: str = "") -> dict[str, str]:
        timestamp = okx_timestamp()
        signature = hmac_sha256_b64(
            self.credentials.api_secret or "", f"{timestamp}{method.upper()}{request_path}{body}"
        )
        return {
            "OK-ACCESS-KEY": self.credentials.api_key or "",
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.credentials.passphrase or "",
            "Content-Type": "application/json",
        }

    async def _signed_get(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        query = urlencode(params or {})
        request_path = f"{path}?{query}" if query else path
        payload = await self.http.request(
            "GET", request_path, headers=self._headers("GET", request_path)
        )
        if not isinstance(payload, dict):
            raise ProviderError("okx: unexpected response shape", provider="okx")
        code = str(payload.get("code", ""))
        if code != "0":
            message = f"okx: code={code} msg={payload.get('msg')!r}"
            # 50100-50115 are authentication/signature/permission failures.
            if code.startswith("501"):
                raise ProviderAuthError(message, provider="okx")
            raise ProviderError(message, provider="okx")
        data = payload.get("data")
        return data if isinstance(data, list) else []

    async def find_transactions(
        self, expectation: PaymentExpectation, *, reference: str | None = None
    ) -> list[ObservedTransaction]:
        after = to_millis(expectation.created_at) - self.lookback_minutes * 60_000
        rows = await self._signed_get(
            "/api/v5/asset/deposit-history",
            {
                "ccy": expectation.asset.upper(),
                "after": max(after, 0),
                "before": to_millis(utcnow()),
                "limit": "100",
            },
        )
        transactions: list[ObservedTransaction] = []
        for row in rows:
            tx = self._normalize(row, expectation)
            if tx is not None:
                transactions.append(tx)
        log.info(
            "okx.deposits_fetched",
            provider="okx",
            candidates=len(transactions),
            raw_records=len(rows),
            intent=expectation.intent_id,
        )
        return transactions

    def _normalize(
        self, row: dict[str, Any], expectation: PaymentExpectation
    ) -> ObservedTransaction | None:
        chain = map_okx_chain(row.get("chain"))
        if chain is None:
            log.warning("okx.unmapped_chain", chain=str(row.get("chain")))
            return None
        try:
            amount = to_decimal(str(row.get("amt", "0")))
            units = base_units(amount, expectation.asset_decimals)
        except (ValueError, TypeError) as exc:
            log.warning("okx.deposit_parse_failed", error=str(exc))
            return None

        external_id = str(row.get("depId") or row.get("txId") or "")
        if not external_id:
            return None
        state = str(row.get("state", ""))
        address = str(row.get("to") or "")
        ts = row.get("ts")
        return ObservedTransaction(
            provider=ProviderCode.OKX,
            network=chain,
            external_id=external_id,
            asset=str(row.get("ccy", "")).upper(),
            amount_units=units,
            decimals=expectation.asset_decimals,
            to_address=address,
            to_address_normalized=normalize_address(address, chain),
            from_address=str(row.get("from") or "") or None,
            is_successful=state in self.credited_states,
            status_label=f"state={state}",
            observed_at=utcnow(),
            block_time=from_timestamp(ts, unit="ms") if ts else None,
            confirmations=_parse_int(row.get("actualDepBlkConfirm")),
            txid=str(row.get("txId") or "") or None,
            token_contract=expectation.token_contract,
            record_type="deposit",
            raw=row,
        )

    async def health_check(self) -> ProviderHealth:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await self._signed_get("/api/v5/account/balance", {"ccy": "USDT"})
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


def _parse_int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0
