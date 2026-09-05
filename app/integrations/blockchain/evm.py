"""EVM chain verification via standard JSON-RPC.

Works for every EVM network the platform supports - BSC (BEP20), Ethereum
(ERC20), Avalanche C-Chain, Arbitrum One and Polygon - because they all expose
the same standard methods:

* ``eth_getTransactionByHash``  - transaction envelope
* ``eth_getTransactionReceipt`` - execution status + event logs
* ``eth_blockNumber``           - chain head, for the confirmation count
* ``eth_getBlockByNumber``      - block timestamp

ERC-20 transfers are read from the receipt logs, matching the canonical
``Transfer(address,address,uint256)`` event:

    topic0 = keccak256("Transfer(address,address,uint256)")
           = 0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef
    topic1 = from (indexed, left-padded to 32 bytes)
    topic2 = to   (indexed)
    data   = value (uint256)

Critically, the log's ``address`` field is the **token contract that emitted
the event**. Matching on that is what makes a counterfeit "USDT" token fail
verification, which a symbol comparison alone would not catch.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.exceptions import ProviderError, TransactionNotFoundError
from app.core.logging import get_logger
from app.core.money import from_base_units
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

#: keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

EVM_NETWORKS = frozenset(
    {
        NetworkCode.BEP20,
        NetworkCode.ERC20,
        NetworkCode.AVAXC,
        NetworkCode.ARBITRUM,
        NetworkCode.POLYGON,
    }
)


def topic_to_address(topic: str) -> str:
    """An indexed address topic is the 20-byte address right-aligned in 32 bytes."""
    cleaned = topic[2:] if topic.startswith("0x") else topic
    return "0x" + cleaned[-40:].lower()


def hex_to_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    text = str(value)
    return int(text, 16) if text.startswith(("0x", "0X")) else int(text)


class EVMAdapter(BaseAdapter):
    """Verifies native-coin and ERC-20 transfers on one EVM network."""

    provider_code = ProviderCode.EVM
    capabilities = ProviderCapabilities(
        lookup_by_id=True,
        # Standard JSON-RPC has no "list transfers to this address" method; an
        # indexer would be required. Payments are therefore verified from a
        # customer-submitted txid, which is a lookup hint only - every field is
        # still validated against the chain.
        list_recent=False,
        reports_confirmations=True,
        reports_memo=False,
        reports_sender=True,
        notes=(
            "Standard JSON-RPC: eth_getTransactionByHash, eth_getTransactionReceipt, "
            "eth_blockNumber, eth_getBlockByNumber.",
            "ERC-20 transfers are matched on the emitting contract address, so a "
            "counterfeit token with the USDT symbol cannot pass verification.",
            "Requires the customer to submit the transaction hash: plain JSON-RPC "
            "cannot enumerate incoming transfers by address.",
        ),
    )

    def __init__(
        self,
        rpc_url: str,
        network: NetworkCode,
        *,
        http: ProviderHTTPClient | None = None,
    ) -> None:
        if network not in EVM_NETWORKS:
            raise ValueError(f"{network} is not an EVM network")
        self.network = network
        self.rpc_url = rpc_url
        super().__init__(http or ProviderHTTPClient(rpc_url, provider=f"evm:{network.value}"))
        self._request_id = 0

    def supports_network(self, network: NetworkCode) -> bool:
        return network is self.network

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        payload = await self.http.request(
            "POST",
            "",
            json={"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params},
        )
        if not isinstance(payload, dict):
            raise ProviderError(f"evm: unexpected RPC payload for {method}", provider=self.http.provider)
        if payload.get("error"):
            error = payload["error"]
            raise ProviderError(
                f"evm: RPC error on {method}: {error}",
                provider=self.http.provider,
                retryable=True,
            )
        return payload.get("result")

    async def find_transactions(
        self, expectation: PaymentExpectation, *, reference: str | None = None
    ) -> list[ObservedTransaction]:
        txid = normalize_txid(reference)
        if not txid:
            # No hash to look up: nothing observable. The caller keeps polling
            # until the customer submits one; nothing is ever assumed.
            return []
        tx_hash = f"0x{txid}" if not txid.startswith("0x") else txid

        receipt, transaction, head = await asyncio.gather(
            self._rpc("eth_getTransactionReceipt", [tx_hash]),
            self._rpc("eth_getTransactionByHash", [tx_hash]),
            self._rpc("eth_blockNumber", []),
        )
        if not receipt or not transaction:
            raise TransactionNotFoundError(
                f"evm: transaction {tx_hash} not found on {self.network.value}",
                provider=self.http.provider,
            )

        block_number = hex_to_int(receipt.get("blockNumber"))
        head_number = hex_to_int(head)
        confirmations = max(head_number - block_number + 1, 0) if block_number else 0
        # status is "0x1" on success, "0x0" when the transaction reverted.
        succeeded = hex_to_int(receipt.get("status")) == 1
        block_time = await self._block_time(receipt.get("blockNumber"))

        if expectation.token_contract:
            return self._token_transfers(
                receipt, expectation, confirmations, succeeded, block_time, tx_hash
            )
        return self._native_transfer(
            transaction, expectation, confirmations, succeeded, block_time, tx_hash
        )

    def _token_transfers(
        self,
        receipt: dict[str, Any],
        expectation: PaymentExpectation,
        confirmations: int,
        succeeded: bool,
        block_time: Any,
        tx_hash: str,
    ) -> list[ObservedTransaction]:
        transfers: list[ObservedTransaction] = []
        for log_entry in receipt.get("logs") or []:
            topics = log_entry.get("topics") or []
            if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
                continue
            contract = normalize_address(log_entry.get("address"), self.network)
            to_address = topic_to_address(topics[2])
            amount_units = hex_to_int(log_entry.get("data") or "0x0")
            transfers.append(
                ObservedTransaction(
                    provider=ProviderCode.EVM,
                    network=self.network,
                    external_id=tx_hash,
                    log_index=hex_to_int(log_entry.get("logIndex")),
                    asset=expectation.asset,
                    amount_units=amount_units,
                    decimals=expectation.asset_decimals,
                    to_address=to_address,
                    to_address_normalized=normalize_address(to_address, self.network),
                    from_address=topic_to_address(topics[1]),
                    token_contract=contract,
                    is_successful=succeeded,
                    status_label="success" if succeeded else "reverted",
                    observed_at=utcnow(),
                    block_number=hex_to_int(receipt.get("blockNumber")),
                    block_time=block_time,
                    confirmations=confirmations,
                    txid=tx_hash,
                    record_type="erc20_transfer",
                    raw={"log": log_entry, "status": receipt.get("status")},
                )
            )
        return transfers

    def _native_transfer(
        self,
        transaction: dict[str, Any],
        expectation: PaymentExpectation,
        confirmations: int,
        succeeded: bool,
        block_time: Any,
        tx_hash: str,
    ) -> list[ObservedTransaction]:
        to_address = normalize_address(transaction.get("to"), self.network)
        return [
            ObservedTransaction(
                provider=ProviderCode.EVM,
                network=self.network,
                external_id=tx_hash,
                asset=expectation.asset,
                amount_units=hex_to_int(transaction.get("value")),
                decimals=expectation.asset_decimals,
                to_address=to_address,
                to_address_normalized=to_address,
                from_address=normalize_address(transaction.get("from"), self.network),
                token_contract=None,
                is_successful=succeeded,
                status_label="success" if succeeded else "reverted",
                observed_at=utcnow(),
                block_number=hex_to_int(transaction.get("blockNumber")),
                block_time=block_time,
                confirmations=confirmations,
                txid=tx_hash,
                record_type="native_transfer",
                raw=transaction,
            )
        ]

    async def _block_time(self, block_number: Any) -> Any:
        if not block_number:
            return None
        try:
            block = await self._rpc("eth_getBlockByNumber", [block_number, False])
        except ProviderError:
            return None
        if not block or not block.get("timestamp"):
            return None
        return from_timestamp(hex_to_int(block["timestamp"]))

    async def health_check(self) -> ProviderHealth:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            head = await self._rpc("eth_blockNumber", [])
        except ProviderError as exc:
            return ProviderHealth(
                healthy=False,
                latency_ms=int((loop.time() - started) * 1000),
                message="RPC unreachable",
                details={"detail": str(exc)[:200]},
            )
        return ProviderHealth(
            healthy=True,
            latency_ms=int((loop.time() - started) * 1000),
            message="OK",
            details={"head_block": hex_to_int(head)},
        )


def token_amount(units: int, decimals: int) -> Any:
    return from_base_units(units, decimals)
