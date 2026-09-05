"""TRON (TRC20) verification via full-node HTTP API.

Endpoints (TronGrid / any TRON full node):

* ``POST /wallet/gettransactionbyid``     - transaction envelope + contract data
* ``POST /wallet/gettransactioninfobyid`` - receipt: logs, block number, fee
* ``POST /wallet/getnowblock``            - chain head, for confirmations

TRC20 transfers are read from the transaction info logs using the same
``Transfer(address,address,uint256)`` event signature as EVM chains. The log's
``address`` field is the emitting contract in 21-byte hex form (``41`` prefix),
which is converted to the base58check form for comparison against the
configured token contract.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import base58

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

TRONGRID_BASE = "https://api.trongrid.io"

#: keccak256("Transfer(address,address,uint256)") without the 0x prefix, which
#: is how TRON's gettransactioninfobyid reports log topics.
TRON_TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

#: TRON mainnet address version byte.
TRON_ADDRESS_PREFIX = 0x41


def hex_to_base58_address(hex_address: str) -> str:
    """Convert a 21-byte hex TRON address (``41...``) to base58check.

    TRON addresses are ``base58(payload + sha256(sha256(payload))[:4])`` where
    payload is the 0x41-prefixed 20-byte address.
    """
    cleaned = hex_address.lower().removeprefix("0x")
    if len(cleaned) == 40:  # bare 20-byte address from an event topic
        cleaned = f"{TRON_ADDRESS_PREFIX:02x}{cleaned}"
    if len(cleaned) != 42 or not cleaned.startswith("41"):
        return hex_address
    payload = bytes.fromhex(cleaned)
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return base58.b58encode(payload + checksum).decode()


def base58_to_hex_address(address: str) -> str:
    """Inverse of :func:`hex_to_base58_address`, for comparing contract ids."""
    try:
        decoded = base58.b58decode(address)
    except ValueError:
        return address.lower()
    if len(decoded) != 25:
        return address.lower()
    return decoded[:21].hex()


class TronAdapter(BaseAdapter):
    provider_code = ProviderCode.TRON
    capabilities = ProviderCapabilities(
        lookup_by_id=True,
        list_recent=False,
        reports_confirmations=True,
        reports_memo=False,
        reports_sender=True,
        notes=(
            "Full-node HTTP API: /wallet/gettransactionbyid, "
            "/wallet/gettransactioninfobyid, /wallet/getnowblock.",
            "TRC20 transfers are matched on the emitting contract address.",
            "Requires the customer to submit the transaction hash.",
            "A TronGrid API key (TRON-PRO-API-KEY header) raises the rate limit.",
        ),
    )

    def __init__(
        self,
        base_url: str = TRONGRID_BASE,
        *,
        api_key: str | None = None,
        http: ProviderHTTPClient | None = None,
    ) -> None:
        headers = {"TRON-PRO-API-KEY": api_key} if api_key else {}
        super().__init__(http or ProviderHTTPClient(base_url, provider="tron", headers=headers))

    def supports_network(self, network: NetworkCode) -> bool:
        return network is NetworkCode.TRC20

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self.http.request("POST", path, json=payload)
        if not isinstance(result, dict):
            raise ProviderError(f"tron: unexpected payload from {path}", provider="tron")
        return result

    async def find_transactions(
        self, expectation: PaymentExpectation, *, reference: str | None = None
    ) -> list[ObservedTransaction]:
        txid = normalize_txid(reference)
        if not txid:
            return []

        transaction, info, head = await asyncio.gather(
            self._post("/wallet/gettransactionbyid", {"value": txid}),
            self._post("/wallet/gettransactioninfobyid", {"value": txid}),
            self._post("/wallet/getnowblock", {}),
        )
        if not transaction or not transaction.get("txID"):
            raise TransactionNotFoundError(
                f"tron: transaction {txid} not found", provider="tron"
            )

        # contractRet is SUCCESS when the smart-contract call itself succeeded.
        ret = (transaction.get("ret") or [{}])[0]
        succeeded = str(ret.get("contractRet", "")).upper() == "SUCCESS"
        # A non-empty receipt result other than SUCCESS means the call reverted.
        receipt_result = str((info.get("receipt") or {}).get("result", "")).upper()
        if receipt_result and receipt_result != "SUCCESS":
            succeeded = False

        block_number = int(info.get("blockNumber") or 0)
        head_number = int(
            ((head.get("block_header") or {}).get("raw_data") or {}).get("number") or 0
        )
        confirmations = max(head_number - block_number + 1, 0) if block_number else 0
        block_time = (
            from_timestamp(info["blockTimeStamp"], unit="ms") if info.get("blockTimeStamp") else None
        )

        transfers = self._trc20_transfers(
            info, expectation, confirmations, succeeded, block_time, txid, block_number
        )
        if transfers or expectation.token_contract:
            return transfers
        return self._native_transfer(
            transaction, expectation, confirmations, succeeded, block_time, txid, block_number
        )

    def _trc20_transfers(
        self,
        info: dict[str, Any],
        expectation: PaymentExpectation,
        confirmations: int,
        succeeded: bool,
        block_time: Any,
        txid: str,
        block_number: int,
    ) -> list[ObservedTransaction]:
        transfers: list[ObservedTransaction] = []
        for index, entry in enumerate(info.get("log") or []):
            topics = entry.get("topics") or []
            if len(topics) < 3 or topics[0].lower() != TRON_TRANSFER_TOPIC:
                continue
            contract = hex_to_base58_address(entry.get("address", ""))
            to_address = hex_to_base58_address(topics[2][-40:])
            from_address = hex_to_base58_address(topics[1][-40:])
            try:
                amount_units = int(entry.get("data") or "0", 16)
            except ValueError:
                continue
            transfers.append(
                ObservedTransaction(
                    provider=ProviderCode.TRON,
                    network=NetworkCode.TRC20,
                    external_id=txid,
                    log_index=index,
                    asset=expectation.asset,
                    amount_units=amount_units,
                    decimals=expectation.asset_decimals,
                    to_address=to_address,
                    to_address_normalized=normalize_address(to_address, NetworkCode.TRC20),
                    from_address=from_address,
                    token_contract=contract,
                    is_successful=succeeded,
                    status_label="success" if succeeded else "failed",
                    observed_at=utcnow(),
                    block_number=block_number,
                    block_time=block_time,
                    confirmations=confirmations,
                    txid=txid,
                    record_type="trc20_transfer",
                    raw=entry,
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
        txid: str,
        block_number: int,
    ) -> list[ObservedTransaction]:
        contracts = (transaction.get("raw_data") or {}).get("contract") or []
        if not contracts:
            return []
        value = ((contracts[0].get("parameter") or {}).get("value")) or {}
        if contracts[0].get("type") != "TransferContract":
            return []
        to_address = hex_to_base58_address(value.get("to_address", ""))
        return [
            ObservedTransaction(
                provider=ProviderCode.TRON,
                network=NetworkCode.TRC20,
                external_id=txid,
                asset=expectation.asset,
                amount_units=int(value.get("amount") or 0),
                decimals=expectation.asset_decimals,
                to_address=to_address,
                to_address_normalized=normalize_address(to_address, NetworkCode.TRC20),
                from_address=hex_to_base58_address(value.get("owner_address", "")),
                token_contract=None,
                is_successful=succeeded,
                status_label="success" if succeeded else "failed",
                observed_at=utcnow(),
                block_number=block_number,
                block_time=block_time,
                confirmations=confirmations,
                txid=txid,
                record_type="trx_transfer",
                raw=value,
            )
        ]

    async def health_check(self) -> ProviderHealth:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            head = await self._post("/wallet/getnowblock", {})
        except ProviderError as exc:
            return ProviderHealth(
                healthy=False,
                latency_ms=int((loop.time() - started) * 1000),
                message="RPC unreachable",
                details={"detail": str(exc)[:200]},
            )
        number = ((head.get("block_header") or {}).get("raw_data") or {}).get("number")
        return ProviderHealth(
            healthy=bool(number),
            latency_ms=int((loop.time() - started) * 1000),
            message="OK" if number else "No block returned",
            details={"head_block": number},
        )
