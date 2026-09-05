"""TON verification via the Toncenter v3 indexer API.

Endpoints:

* ``GET /api/v3/jettonTransfers`` - jetton (TON's token standard) transfers,
  filterable by ``owner_address``, ``jetton_master`` and ``direction=in``.
* ``GET /api/v3/transactions``    - native TON transfers by account/hash.
* ``GET /api/v3/masterchainInfo`` - chain head, used for the health probe.

Docs: https://toncenter.com/api/v3/

TON differs from account-model chains in one way that matters commercially: a
single receiving wallet is normally shared by all customers and payments are
distinguished by the **comment (memo) attached to the transfer**. The payment
method must therefore be configured with ``requires_memo`` so the verification
engine enforces the reference match; without it, two customers paying the same
amount could not be told apart.

Unlike the EVM/TRON adapters this one *can* enumerate incoming transfers, so a
customer-submitted hash is optional.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.core.timeutils import from_timestamp, utcnow
from app.domain.enums import NetworkCode, ProviderCode
from app.domain.payments.fingerprint import normalize_address
from app.domain.payments.types import (
    ObservedTransaction,
    PaymentExpectation,
    ProviderCapabilities,
    ProviderHealth,
)
from app.integrations.base import BaseAdapter, ProviderHTTPClient

log = get_logger(__name__)

TONCENTER_V3_BASE = "https://toncenter.com"
TON_DECIMALS = 9


class TONAdapter(BaseAdapter):
    provider_code = ProviderCode.TON
    capabilities = ProviderCapabilities(
        lookup_by_id=True,
        list_recent=True,
        reports_confirmations=False,
        reports_memo=True,
        reports_sender=True,
        notes=(
            "Toncenter v3: /api/v3/jettonTransfers, /api/v3/transactions.",
            "Jetton transfers are matched on the jetton master address.",
            "TON payments to a shared wallet MUST require a memo/comment: it is "
            "the only thing that binds a transfer to a specific order.",
            "TON has no rolling confirmation count; an included transaction in a "
            "committed masterchain block is final, so methods use 1 confirmation.",
            "An API key raises the public rate limit (X-API-Key header).",
        ),
    )

    def __init__(
        self,
        base_url: str = TONCENTER_V3_BASE,
        *,
        api_key: str | None = None,
        lookback_minutes: int = 240,
        http: ProviderHTTPClient | None = None,
    ) -> None:
        headers = {"X-API-Key": api_key} if api_key else {}
        self.lookback_minutes = lookback_minutes
        super().__init__(http or ProviderHTTPClient(base_url, provider="ton", headers=headers))

    def supports_network(self, network: NetworkCode) -> bool:
        return network is NetworkCode.TON

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = await self.http.request("GET", path, params=params)
        if not isinstance(payload, dict):
            raise ProviderError(f"ton: unexpected payload from {path}", provider="ton")
        return payload

    async def find_transactions(
        self, expectation: PaymentExpectation, *, reference: str | None = None
    ) -> list[ObservedTransaction]:
        if expectation.token_contract:
            return await self._jetton_transfers(expectation)
        return await self._native_transfers(expectation)

    async def _jetton_transfers(
        self, expectation: PaymentExpectation
    ) -> list[ObservedTransaction]:
        start = int(expectation.created_at.timestamp()) - self.lookback_minutes * 60
        payload = await self._get(
            "/api/v3/jettonTransfers",
            {
                "owner_address": expectation.destination,
                "jetton_master": expectation.token_contract,
                "direction": "in",
                "start_utime": max(start, 0),
                "limit": 100,
                "sort": "desc",
            },
        )
        transfers: list[ObservedTransaction] = []
        for row in payload.get("jetton_transfers") or []:
            tx = self._normalize_jetton(row, expectation)
            if tx is not None:
                transfers.append(tx)
        log.info(
            "ton.jetton_transfers_fetched",
            provider="ton",
            candidates=len(transfers),
            intent=expectation.intent_id,
        )
        return transfers

    def _normalize_jetton(
        self, row: dict[str, Any], expectation: PaymentExpectation
    ) -> ObservedTransaction | None:
        try:
            amount_units = int(str(row.get("amount") or 0))
        except (TypeError, ValueError):
            return None
        destination = str(row.get("destination") or "")
        external_id = str(row.get("transaction_hash") or row.get("trace_id") or "")
        if not external_id:
            return None
        # The comment carrying the order reference lives in forward_payload's
        # decoded text; toncenter surfaces it as `comment` when decodable.
        comment = row.get("comment")
        if comment is None:
            payload_field = row.get("forward_payload")
            if isinstance(payload_field, dict):
                comment = payload_field.get("comment") or payload_field.get("text")
        utime = row.get("transaction_now") or row.get("utime")
        # A transfer only appears in this index once its transaction is
        # committed, and toncenter reports aborted transactions explicitly.
        aborted = bool(row.get("transaction_aborted"))
        return ObservedTransaction(
            provider=ProviderCode.TON,
            network=NetworkCode.TON,
            external_id=external_id,
            asset=expectation.asset,
            amount_units=amount_units,
            decimals=expectation.asset_decimals,
            to_address=destination,
            to_address_normalized=normalize_address(destination, NetworkCode.TON),
            from_address=str(row.get("source") or "") or None,
            token_contract=str(row.get("jetton_master") or expectation.token_contract or ""),
            memo=str(comment) if comment else None,
            is_successful=not aborted,
            status_label="aborted" if aborted else "committed",
            observed_at=utcnow(),
            block_time=from_timestamp(utime) if utime else None,
            # An indexed, committed TON transaction is final.
            confirmations=0 if aborted else 1,
            txid=external_id,
            record_type="jetton_transfer",
            raw=row,
        )

    async def _native_transfers(
        self, expectation: PaymentExpectation
    ) -> list[ObservedTransaction]:
        start = int(expectation.created_at.timestamp()) - self.lookback_minutes * 60
        payload = await self._get(
            "/api/v3/transactions",
            {
                "account": expectation.destination,
                "start_utime": max(start, 0),
                "limit": 100,
                "sort": "desc",
            },
        )
        transfers: list[ObservedTransaction] = []
        for row in payload.get("transactions") or []:
            in_msg = row.get("in_msg") or {}
            value = in_msg.get("value")
            if not value:
                continue
            try:
                amount_units = int(str(value))
            except (TypeError, ValueError):
                continue
            destination = str(in_msg.get("destination") or expectation.destination)
            external_id = str(row.get("hash") or "")
            if not external_id:
                continue
            decoded = in_msg.get("message_content") or {}
            comment = decoded.get("decoded", {}).get("comment") if isinstance(decoded, dict) else None
            utime = row.get("now")
            aborted = bool((row.get("description") or {}).get("aborted"))
            transfers.append(
                ObservedTransaction(
                    provider=ProviderCode.TON,
                    network=NetworkCode.TON,
                    external_id=external_id,
                    asset=expectation.asset,
                    amount_units=amount_units,
                    decimals=TON_DECIMALS,
                    to_address=destination,
                    to_address_normalized=normalize_address(destination, NetworkCode.TON),
                    from_address=str(in_msg.get("source") or "") or None,
                    memo=str(comment) if comment else None,
                    is_successful=not aborted,
                    status_label="aborted" if aborted else "committed",
                    observed_at=utcnow(),
                    block_time=from_timestamp(utime) if utime else None,
                    confirmations=0 if aborted else 1,
                    txid=external_id,
                    record_type="ton_transfer",
                    raw=row,
                )
            )
        return transfers

    async def health_check(self) -> ProviderHealth:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            info = await self._get("/api/v3/masterchainInfo", {})
        except ProviderError as exc:
            return ProviderHealth(
                healthy=False,
                latency_ms=int((loop.time() - started) * 1000),
                message="Indexer unreachable",
                details={"detail": str(exc)[:200]},
            )
        seqno = (info.get("last") or {}).get("seqno")
        return ProviderHealth(
            healthy=bool(seqno),
            latency_ms=int((loop.time() - started) * 1000),
            message="OK" if seqno else "No masterchain info",
            details={"seqno": seqno},
        )
