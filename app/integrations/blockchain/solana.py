"""Solana verification via JSON-RPC.

Endpoints: ``getTransaction`` (with ``jsonParsed`` encoding), ``getSlot``.
Docs: https://solana.com/docs/rpc/http/gettransaction

SPL-token transfers are verified from the transaction metadata's
``preTokenBalances`` / ``postTokenBalances`` arrays rather than by parsing
instructions. That is deliberate: the balance delta is the *settled* effect of
the whole transaction, so it is immune to instruction-level tricks such as a
transfer followed by a claw-back in the same transaction.

Each token-balance entry carries the ``mint``, the ``owner`` and a
``uiTokenAmount`` with the exact integer ``amount`` and ``decimals``. Matching
on ``mint`` is what rejects a counterfeit token using the USDT symbol.

Confirmations: Solana does not expose a per-transaction confirmation count in
``getTransaction``; it reports the slot. Depth is computed as
``current_slot - transaction_slot``, which is the standard proxy. A transaction
included in a finalized block is final by consensus.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.exceptions import ProviderError, TransactionNotFoundError
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

SOLANA_MAINNET_RPC = "https://api.mainnet-beta.solana.com"
LAMPORTS_DECIMALS = 9


class SolanaAdapter(BaseAdapter):
    provider_code = ProviderCode.SOLANA
    capabilities = ProviderCapabilities(
        lookup_by_id=True,
        list_recent=False,
        reports_confirmations=True,
        reports_memo=False,
        reports_sender=True,
        notes=(
            "JSON-RPC getTransaction (jsonParsed) + getSlot.",
            "SPL transfers are read from settled pre/post token balances and "
            "matched on the token mint, not the symbol.",
            "Depth is derived from the slot difference; a finalized "
            "transaction is final by consensus.",
            "Requires the customer to submit the transaction signature.",
        ),
    )

    def __init__(
        self,
        rpc_url: str = SOLANA_MAINNET_RPC,
        *,
        http: ProviderHTTPClient | None = None,
    ) -> None:
        super().__init__(http or ProviderHTTPClient(rpc_url, provider="solana"))
        self._request_id = 0

    def supports_network(self, network: NetworkCode) -> bool:
        return network is NetworkCode.SOL

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        payload = await self.http.request(
            "POST",
            "",
            json={"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params},
        )
        if not isinstance(payload, dict):
            raise ProviderError(f"solana: unexpected payload for {method}", provider="solana")
        if payload.get("error"):
            raise ProviderError(
                f"solana: RPC error on {method}: {payload['error']}", provider="solana"
            )
        return payload.get("result")

    async def find_transactions(
        self, expectation: PaymentExpectation, *, reference: str | None = None
    ) -> list[ObservedTransaction]:
        signature = (reference or "").strip()
        if not signature:
            return []

        transaction, current_slot = await asyncio.gather(
            self._rpc(
                "getTransaction",
                [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            ),
            self._rpc("getSlot", [{"commitment": "confirmed"}]),
        )
        if not transaction:
            raise TransactionNotFoundError(
                f"solana: signature {signature} not found", provider="solana"
            )

        meta = transaction.get("meta") or {}
        succeeded = meta.get("err") is None
        slot = int(transaction.get("slot") or 0)
        depth = max(int(current_slot or 0) - slot, 0)
        block_time = (
            from_timestamp(transaction["blockTime"]) if transaction.get("blockTime") else None
        )

        if expectation.token_contract:
            return self._spl_transfers(
                meta, expectation, depth, succeeded, block_time, signature, slot
            )
        return self._native_transfers(
            transaction, meta, expectation, depth, succeeded, block_time, signature, slot
        )

    def _spl_transfers(
        self,
        meta: dict[str, Any],
        expectation: PaymentExpectation,
        depth: int,
        succeeded: bool,
        block_time: Any,
        signature: str,
        slot: int,
    ) -> list[ObservedTransaction]:
        """Credit only the *increase* in the receiver's token balance."""
        mint = expectation.token_contract or ""
        pre = {
            index_key(entry): entry for entry in (meta.get("preTokenBalances") or [])
        }
        transfers: list[ObservedTransaction] = []
        for position, post in enumerate(meta.get("postTokenBalances") or []):
            if str(post.get("mint", "")) != mint:
                continue
            owner = str(post.get("owner", ""))
            post_amount = int((post.get("uiTokenAmount") or {}).get("amount") or 0)
            decimals = int((post.get("uiTokenAmount") or {}).get("decimals") or expectation.asset_decimals)
            previous = pre.get(index_key(post))
            pre_amount = (
                int((previous.get("uiTokenAmount") or {}).get("amount") or 0) if previous else 0
            )
            delta = post_amount - pre_amount
            if delta <= 0:
                continue
            transfers.append(
                ObservedTransaction(
                    provider=ProviderCode.SOLANA,
                    network=NetworkCode.SOL,
                    external_id=signature,
                    log_index=position,
                    asset=expectation.asset,
                    amount_units=delta,
                    decimals=decimals,
                    to_address=owner,
                    to_address_normalized=normalize_address(owner, NetworkCode.SOL),
                    token_contract=mint,
                    is_successful=succeeded,
                    status_label="success" if succeeded else "failed",
                    observed_at=utcnow(),
                    block_number=slot,
                    block_time=block_time,
                    confirmations=depth,
                    txid=signature,
                    record_type="spl_transfer",
                    raw={"post": post, "pre": previous},
                )
            )
        return transfers

    def _native_transfers(
        self,
        transaction: dict[str, Any],
        meta: dict[str, Any],
        expectation: PaymentExpectation,
        depth: int,
        succeeded: bool,
        block_time: Any,
        signature: str,
        slot: int,
    ) -> list[ObservedTransaction]:
        """SOL transfers, derived from the lamport balance deltas."""
        account_keys = ((transaction.get("transaction") or {}).get("message") or {}).get(
            "accountKeys"
        ) or []
        pre_balances = meta.get("preBalances") or []
        post_balances = meta.get("postBalances") or []
        transfers: list[ObservedTransaction] = []
        for index, key in enumerate(account_keys):
            address = key.get("pubkey") if isinstance(key, dict) else str(key)
            if not address:
                continue
            if index >= len(pre_balances) or index >= len(post_balances):
                continue
            delta = int(post_balances[index]) - int(pre_balances[index])
            if delta <= 0:
                continue
            transfers.append(
                ObservedTransaction(
                    provider=ProviderCode.SOLANA,
                    network=NetworkCode.SOL,
                    external_id=signature,
                    log_index=index,
                    asset=expectation.asset,
                    amount_units=delta,
                    decimals=LAMPORTS_DECIMALS,
                    to_address=str(address),
                    to_address_normalized=normalize_address(str(address), NetworkCode.SOL),
                    token_contract=None,
                    is_successful=succeeded,
                    status_label="success" if succeeded else "failed",
                    observed_at=utcnow(),
                    block_number=slot,
                    block_time=block_time,
                    confirmations=depth,
                    txid=signature,
                    record_type="sol_transfer",
                    raw={"index": index, "delta": delta},
                )
            )
        return transfers

    async def health_check(self) -> ProviderHealth:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            slot = await self._rpc("getSlot", [{"commitment": "confirmed"}])
        except ProviderError as exc:
            return ProviderHealth(
                healthy=False,
                latency_ms=int((loop.time() - started) * 1000),
                message="RPC unreachable",
                details={"detail": str(exc)[:200]},
            )
        return ProviderHealth(
            healthy=bool(slot),
            latency_ms=int((loop.time() - started) * 1000),
            message="OK",
            details={"slot": slot},
        )


def index_key(entry: dict[str, Any]) -> str:
    """Token balance entries are keyed by account index + mint."""
    return f"{entry.get('accountIndex')}:{entry.get('mint')}"
