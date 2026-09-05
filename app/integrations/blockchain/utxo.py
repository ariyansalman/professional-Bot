"""BTC / LTC verification via an Esplora-compatible REST API.

Endpoints (Blockstream Esplora and compatible instances):

* ``GET /api/tx/{txid}``          - transaction with vout scriptpubkey addresses
* ``GET /api/blocks/tip/height``  - chain tip, for the confirmation count

Docs: https://github.com/Blockstream/esplora/blob/master/API.md

UTXO chains have no account model: a payment is an output whose
``scriptpubkey_address`` equals our receiving address. Several outputs may pay
us in one transaction, so each matching vout is emitted as a separate observed
transfer keyed by its output index - which also means each is independently
consumable exactly once.

Amounts are integer satoshis, so no scaling error is possible.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.exceptions import ProviderError, TransactionNotFoundError
from app.core.logging import get_logger
from app.core.timeutils import from_timestamp, utcnow
from app.domain.enums import NetworkCode, ProviderCode
from app.domain.payments.fingerprint import normalize_address, normalize_txid
from app.domain.payments.types import (
    ObservedTransaction,
    PaymentExpectation,
    ProviderCapabilities,
    ProviderHealth,
)
from app.integrations.base import BaseAdapter, ProviderHTTPClient

log = get_logger(__name__)

SATOSHI_DECIMALS = 8
UTXO_NETWORKS = frozenset({NetworkCode.BTC, NetworkCode.LTC})


class UTXOAdapter(BaseAdapter):
    provider_code = ProviderCode.UTXO
    capabilities = ProviderCapabilities(
        lookup_by_id=True,
        list_recent=True,
        reports_confirmations=True,
        reports_memo=False,
        reports_sender=True,
        notes=(
            "Esplora REST: /api/tx/{txid}, /api/address/{addr}/txs, "
            "/api/blocks/tip/height.",
            "Each output paying the receiving address is a separate, "
            "independently consumable payment.",
            "Amounts are integer satoshis (8 decimals).",
            "A dedicated receiving address per payment is strongly recommended; "
            "with a shared address, two equal-value payments cannot be "
            "distinguished except by txid.",
        ),
    )

    def __init__(
        self,
        base_url: str,
        network: NetworkCode,
        *,
        http: ProviderHTTPClient | None = None,
    ) -> None:
        if network not in UTXO_NETWORKS:
            raise ValueError(f"{network} is not a UTXO network")
        self.network = network
        super().__init__(http or ProviderHTTPClient(base_url, provider=f"utxo:{network.value}"))

    def supports_network(self, network: NetworkCode) -> bool:
        return network is self.network

    async def _get(self, path: str, *, expect_json: bool = True) -> Any:
        return await self.http.request("GET", path, expect_json=expect_json)

    async def _tip_height(self) -> int:
        raw = await self._get("/api/blocks/tip/height", expect_json=False)
        try:
            return int(raw.decode().strip())
        except (AttributeError, ValueError) as exc:
            raise ProviderError(
                f"utxo: unparsable tip height {raw!r}", provider=self.http.provider
            ) from exc

    async def find_transactions(
        self, expectation: PaymentExpectation, *, reference: str | None = None
    ) -> list[ObservedTransaction]:
        txid = normalize_txid(reference)
        if txid:
            return await self._by_txid(txid, expectation)
        return await self._by_address(expectation)

    async def _by_txid(
        self, txid: str, expectation: PaymentExpectation
    ) -> list[ObservedTransaction]:
        transaction, tip = await asyncio.gather(self._get(f"/api/tx/{txid}"), self._tip_height())
        if not isinstance(transaction, dict) or not transaction.get("txid"):
            raise TransactionNotFoundError(
                f"utxo: transaction {txid} not found", provider=self.http.provider
            )
        return self._outputs_to_us(transaction, expectation, tip)

    async def _by_address(self, expectation: PaymentExpectation) -> list[ObservedTransaction]:
        transactions, tip = await asyncio.gather(
            self._get(f"/api/address/{expectation.destination}/txs"), self._tip_height()
        )
        if not isinstance(transactions, list):
            return []
        observed: list[ObservedTransaction] = []
        for transaction in transactions[:50]:
            observed.extend(self._outputs_to_us(transaction, expectation, tip))
        return observed

    def _outputs_to_us(
        self, transaction: dict[str, Any], expectation: PaymentExpectation, tip: int
    ) -> list[ObservedTransaction]:
        status = transaction.get("status") or {}
        confirmed = bool(status.get("confirmed"))
        block_height = status.get("block_height")
        confirmations = max(tip - int(block_height) + 1, 0) if confirmed and block_height else 0
        block_time = from_timestamp(status["block_time"]) if status.get("block_time") else None
        txid = str(transaction.get("txid") or "")
        sender = None
        vins = transaction.get("vin") or []
        if vins:
            prevout = (vins[0] or {}).get("prevout") or {}
            sender = prevout.get("scriptpubkey_address")

        results: list[ObservedTransaction] = []
        for index, vout in enumerate(transaction.get("vout") or []):
            address = str(vout.get("scriptpubkey_address") or "")
            if not address:
                continue
            if normalize_address(address, self.network) != expectation.destination_normalized:
                continue
            results.append(
                ObservedTransaction(
                    provider=ProviderCode.UTXO,
                    network=self.network,
                    external_id=txid,
                    log_index=index,
                    asset=expectation.asset,
                    amount_units=int(vout.get("value") or 0),
                    decimals=SATOSHI_DECIMALS,
                    to_address=address,
                    to_address_normalized=normalize_address(address, self.network),
                    from_address=sender,
                    token_contract=None,
                    # An unconfirmed (mempool) transaction is reported so the
                    # customer sees "detected", but it carries 0 confirmations
                    # and therefore cannot be credited.
                    is_successful=True,
                    status_label="confirmed" if confirmed else "mempool",
                    observed_at=utcnow(),
                    block_number=int(block_height) if block_height else None,
                    block_time=block_time,
                    confirmations=confirmations,
                    txid=txid,
                    record_type="utxo_output",
                    raw={"vout_index": index, "status": status},
                )
            )
        return results

    async def health_check(self) -> ProviderHealth:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            tip = await self._tip_height()
        except ProviderError as exc:
            return ProviderHealth(
                healthy=False,
                latency_ms=int((loop.time() - started) * 1000),
                message="Indexer unreachable",
                details={"detail": str(exc)[:200]},
            )
        return ProviderHealth(
            healthy=tip > 0,
            latency_ms=int((loop.time() - started) * 1000),
            message="OK",
            details={"tip_height": tip},
        )
