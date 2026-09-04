"""Adapter normalisation tests.

These exercise the parsing/normalisation of realistic provider payloads. HTTP
is mocked with respx: no network access and no live credentials are used. The
payload shapes mirror the documented response schemas referenced in each
adapter's module docstring.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from app.core.money import base_units
from app.core.timeutils import utcnow
from app.domain.enums import NetworkCode, ProviderCode, VerificationOutcome
from app.domain.payments.fingerprint import normalize_address
from app.domain.payments.types import PaymentExpectation
from app.domain.payments.verification import select_best_candidate, verify_transaction
from app.integrations.base import ProviderCredentials
from app.integrations.binance.adapter import BinanceDepositAdapter
from app.integrations.blockchain.evm import EVMAdapter
from app.integrations.blockchain.solana import SolanaAdapter
from app.integrations.blockchain.tron import TronAdapter, base58_to_hex_address
from app.integrations.blockchain.utxo import UTXOAdapter
from app.integrations.bybit.adapter import BybitAdapter
from app.integrations.okx.adapter import OKXAdapter

USDT_ERC20 = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
EVM_RECEIVER = "0x1234567890AbcdEF1234567890aBcdef12345678"
TRON_RECEIVER = "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"


def expectation(**overrides) -> PaymentExpectation:
    now = utcnow()
    defaults = dict(
        intent_id="pi_test",
        reference="TG-10284",
        provider=ProviderCode.EVM,
        network=NetworkCode.ERC20,
        asset="USDT",
        asset_decimals=6,
        expected_amount=Decimal("10.000000"),
        expected_amount_units=base_units("10.000000", 6),
        destination=EVM_RECEIVER,
        destination_normalized=normalize_address(EVM_RECEIVER, NetworkCode.ERC20),
        token_contract=USDT_ERC20,
        memo=None,
        required_confirmations=12,
        created_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(minutes=25),
    )
    defaults.update(overrides)
    return PaymentExpectation(**defaults)


def rpc_result(result):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


# --- EVM ------------------------------------------------------------------


@respx.mock
async def test_evm_parses_erc20_transfer_and_verifies():
    exp = expectation()
    tx_hash = "0x" + "ab" * 32
    receipt = {
        "blockNumber": "0x10",
        "status": "0x1",
        "logs": [
            {
                "address": USDT_ERC20.lower(),
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x000000000000000000000000aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "0x000000000000000000000000" + EVM_RECEIVER[2:].lower(),
                ],
                "data": "0x989680",  # 10_000_000 = 10.000000 USDT
                "logIndex": "0x3",
            }
        ],
    }
    responses = [
        rpc_result(receipt),
        rpc_result({"blockNumber": "0x10", "to": EVM_RECEIVER, "from": "0xaaa", "value": "0x0"}),
        rpc_result("0x1b"),  # head block 27 -> 12 confirmations
        rpc_result({"timestamp": hex(int(utcnow().timestamp()))}),
    ]
    respx.post("https://rpc.example/").mock(side_effect=responses)

    adapter = EVMAdapter("https://rpc.example/", NetworkCode.ERC20)
    transactions = await adapter.find_transactions(exp, reference=tx_hash)
    await adapter.aclose()

    assert len(transactions) == 1
    tx = transactions[0]
    assert tx.amount == Decimal("10")
    assert tx.log_index == 3
    assert tx.confirmations == 12
    assert verify_transaction(exp, tx).outcome is VerificationOutcome.VERIFIED


@respx.mock
async def test_evm_counterfeit_token_contract_fails_verification():
    """A transfer of a fake token that calls itself USDT must not verify."""
    exp = expectation()
    fake_contract = "0xfeedfacefeedfacefeedfacefeedfacefeedface"
    receipt = {
        "blockNumber": "0x10",
        "status": "0x1",
        "logs": [
            {
                "address": fake_contract,
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + "0" * 64,
                    "0x000000000000000000000000" + EVM_RECEIVER[2:].lower(),
                ],
                "data": "0x989680",
                "logIndex": "0x0",
            }
        ],
    }
    respx.post("https://rpc.example/").mock(
        side_effect=[
            rpc_result(receipt),
            rpc_result({"blockNumber": "0x10", "to": EVM_RECEIVER, "value": "0x0"}),
            rpc_result("0x1b"),
            rpc_result({"timestamp": hex(int(utcnow().timestamp()))}),
        ]
    )
    adapter = EVMAdapter("https://rpc.example/", NetworkCode.ERC20)
    transactions = await adapter.find_transactions(exp, reference="0x" + "cd" * 32)
    await adapter.aclose()

    _, decision = select_best_candidate(exp, transactions)
    assert decision.outcome is not VerificationOutcome.VERIFIED


@respx.mock
async def test_evm_reverted_transaction_is_not_credited():
    receipt = {
        "blockNumber": "0x10",
        "status": "0x0",  # reverted
        "logs": [],
    }
    respx.post("https://rpc.example/").mock(
        side_effect=[
            rpc_result(receipt),
            rpc_result({"blockNumber": "0x10", "to": EVM_RECEIVER, "value": "0x989680"}),
            rpc_result("0x1b"),
            rpc_result({"timestamp": hex(int(utcnow().timestamp()))}),
        ]
    )
    adapter = EVMAdapter("https://rpc.example/", NetworkCode.ERC20)
    transactions = await adapter.find_transactions(
        expectation(token_contract=None), reference="0x" + "ef" * 32
    )
    await adapter.aclose()
    assert transactions
    assert not transactions[0].is_successful


@respx.mock
async def test_evm_without_reference_observes_nothing():
    """No submitted hash means nothing is observable - never an assumption."""
    adapter = EVMAdapter("https://rpc.example/", NetworkCode.ERC20)
    assert await adapter.find_transactions(expectation()) == []
    await adapter.aclose()


# --- TRON -----------------------------------------------------------------


@respx.mock
async def test_tron_parses_trc20_transfer():
    exp = expectation(
        provider=ProviderCode.TRON,
        network=NetworkCode.TRC20,
        destination=TRON_RECEIVER,
        destination_normalized=normalize_address(TRON_RECEIVER, NetworkCode.TRC20),
        token_contract=USDT_TRC20,
        required_confirmations=19,
    )
    txid = "a8f3" + "0" * 60
    contract_hex = base58_to_hex_address(USDT_TRC20)
    receiver_hex = base58_to_hex_address(TRON_RECEIVER)

    respx.post("https://tron.example/wallet/gettransactionbyid").mock(
        return_value=httpx.Response(200, json={"txID": txid, "ret": [{"contractRet": "SUCCESS"}]})
    )
    respx.post("https://tron.example/wallet/gettransactioninfobyid").mock(
        return_value=httpx.Response(
            200,
            json={
                "blockNumber": 1000,
                "blockTimeStamp": int(utcnow().timestamp() * 1000),
                "receipt": {"result": "SUCCESS"},
                "log": [
                    {
                        "address": contract_hex[2:],
                        "topics": [
                            "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                            "0" * 24 + "a" * 40,
                            "0" * 24 + receiver_hex[2:],
                        ],
                        "data": "0000000000000000000000000000000000000000000000000000000000989680",
                    }
                ],
            },
        )
    )
    respx.post("https://tron.example/wallet/getnowblock").mock(
        return_value=httpx.Response(200, json={"block_header": {"raw_data": {"number": 1030}}})
    )

    adapter = TronAdapter("https://tron.example")
    transactions = await adapter.find_transactions(exp, reference=txid)
    await adapter.aclose()

    assert len(transactions) == 1
    tx = transactions[0]
    assert tx.token_contract == USDT_TRC20
    assert tx.to_address == TRON_RECEIVER
    assert tx.amount == Decimal("10")
    assert tx.confirmations == 31
    assert verify_transaction(exp, tx).outcome is VerificationOutcome.VERIFIED


# --- Solana ---------------------------------------------------------------


@respx.mock
async def test_solana_credits_only_the_balance_increase():
    mint = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
    owner = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
    exp = expectation(
        provider=ProviderCode.SOLANA,
        network=NetworkCode.SOL,
        destination=owner,
        destination_normalized=normalize_address(owner, NetworkCode.SOL),
        token_contract=mint,
        required_confirmations=1,
    )
    respx.post("https://solana.example/").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "slot": 100,
                        "blockTime": int(utcnow().timestamp()),
                        "meta": {
                            "err": None,
                            "preTokenBalances": [
                                {
                                    "accountIndex": 1,
                                    "mint": mint,
                                    "owner": owner,
                                    "uiTokenAmount": {"amount": "5000000", "decimals": 6},
                                }
                            ],
                            "postTokenBalances": [
                                {
                                    "accountIndex": 1,
                                    "mint": mint,
                                    "owner": owner,
                                    "uiTokenAmount": {"amount": "15000000", "decimals": 6},
                                }
                            ],
                        },
                    },
                },
            ),
            httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": 110}),
        ]
    )
    adapter = SolanaAdapter("https://solana.example/")
    transactions = await adapter.find_transactions(exp, reference="5xSig")
    await adapter.aclose()

    assert len(transactions) == 1
    # Balance went 5 -> 15, so exactly 10 was received.
    assert transactions[0].amount == Decimal("10")
    assert verify_transaction(exp, transactions[0]).outcome is VerificationOutcome.VERIFIED


# --- UTXO -----------------------------------------------------------------


@respx.mock
async def test_utxo_emits_one_payment_per_matching_output():
    address = "bc1qexampleaddress00000000000000000000000"
    exp = expectation(
        provider=ProviderCode.UTXO,
        network=NetworkCode.BTC,
        asset="BTC",
        asset_decimals=8,
        expected_amount=Decimal("0.00100000"),
        expected_amount_units=100_000,
        destination=address,
        destination_normalized=normalize_address(address, NetworkCode.BTC),
        token_contract=None,
        required_confirmations=2,
    )
    respx.get("https://esplora.example/api/tx/deadbeef").mock(
        return_value=httpx.Response(
            200,
            json={
                "txid": "deadbeef",
                "status": {
                    "confirmed": True,
                    "block_height": 800_000,
                    "block_time": int(utcnow().timestamp()),
                },
                "vin": [{"prevout": {"scriptpubkey_address": "bc1qsender"}}],
                "vout": [
                    {"scriptpubkey_address": "bc1qsomeoneelse", "value": 500},
                    {"scriptpubkey_address": address, "value": 100_000},
                ],
            },
        )
    )
    respx.get("https://esplora.example/api/blocks/tip/height").mock(
        return_value=httpx.Response(200, text="800002")
    )
    adapter = UTXOAdapter("https://esplora.example", NetworkCode.BTC)
    transactions = await adapter.find_transactions(exp, reference="deadbeef")
    await adapter.aclose()

    assert len(transactions) == 1  # only the output paying us
    assert transactions[0].log_index == 1
    assert transactions[0].confirmations == 3
    assert verify_transaction(exp, transactions[0]).outcome is VerificationOutcome.VERIFIED


@respx.mock
async def test_utxo_mempool_transaction_is_not_credited():
    address = "bc1qexampleaddress00000000000000000000000"
    exp = expectation(
        provider=ProviderCode.UTXO,
        network=NetworkCode.BTC,
        asset="BTC",
        asset_decimals=8,
        expected_amount=Decimal("0.00100000"),
        expected_amount_units=100_000,
        destination=address,
        destination_normalized=normalize_address(address, NetworkCode.BTC),
        token_contract=None,
        required_confirmations=2,
    )
    respx.get("https://esplora.example/api/tx/aa").mock(
        return_value=httpx.Response(
            200,
            json={
                "txid": "aa",
                "status": {"confirmed": False},
                "vin": [],
                "vout": [{"scriptpubkey_address": address, "value": 100_000}],
            },
        )
    )
    respx.get("https://esplora.example/api/blocks/tip/height").mock(
        return_value=httpx.Response(200, text="800002")
    )
    adapter = UTXOAdapter("https://esplora.example", NetworkCode.BTC)
    transactions = await adapter.find_transactions(exp, reference="aa")
    await adapter.aclose()
    assert transactions[0].confirmations == 0
    assert verify_transaction(exp, transactions[0]).outcome is VerificationOutcome.PENDING_CONFIRMATION


# --- Exchanges ------------------------------------------------------------


@respx.mock
async def test_binance_deposit_history_normalisation():
    exp = expectation(
        provider=ProviderCode.BINANCE,
        network=NetworkCode.TRC20,
        destination=TRON_RECEIVER,
        destination_normalized=normalize_address(TRON_RECEIVER, NetworkCode.TRC20),
        token_contract=None,
        required_confirmations=1,
    )
    respx.get(url__regex=r"https://binance\.example/sapi/v1/capital/deposit/hisrec.*").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "769800519366885376",
                    "amount": "10.00000000",
                    "coin": "USDT",
                    "network": "TRX",
                    "status": 1,
                    "address": TRON_RECEIVER,
                    "txId": "abc123",
                    "insertTime": int(utcnow().timestamp() * 1000),
                    "confirmTimes": "12/12",
                }
            ],
        )
    )
    adapter = BinanceDepositAdapter(
        ProviderCredentials(api_key="k", api_secret="s"), base_url="https://binance.example"
    )
    transactions = await adapter.find_transactions(exp)
    await adapter.aclose()

    assert len(transactions) == 1
    assert transactions[0].confirmations == 12
    assert transactions[0].network is NetworkCode.TRC20
    assert verify_transaction(exp, transactions[0]).outcome is VerificationOutcome.VERIFIED


@respx.mock
async def test_binance_pending_deposit_is_not_credited():
    exp = expectation(
        provider=ProviderCode.BINANCE,
        network=NetworkCode.TRC20,
        destination=TRON_RECEIVER,
        destination_normalized=normalize_address(TRON_RECEIVER, NetworkCode.TRC20),
        token_contract=None,
        required_confirmations=1,
    )
    respx.get(url__regex=r"https://binance\.example/sapi/v1/capital/deposit/hisrec.*").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "1",
                    "amount": "10.00000000",
                    "coin": "USDT",
                    "network": "TRX",
                    "status": 0,  # pending
                    "address": TRON_RECEIVER,
                    "txId": "abc",
                    "insertTime": int(utcnow().timestamp() * 1000),
                    "confirmTimes": "1/12",
                }
            ],
        )
    )
    adapter = BinanceDepositAdapter(
        ProviderCredentials(api_key="k", api_secret="s"), base_url="https://binance.example"
    )
    transactions = await adapter.find_transactions(exp)
    await adapter.aclose()
    assert verify_transaction(exp, transactions[0]).outcome is VerificationOutcome.FAILED_TRANSACTION


@respx.mock
async def test_bybit_deposit_normalisation():
    exp = expectation(
        provider=ProviderCode.BYBIT,
        network=NetworkCode.TRC20,
        destination=TRON_RECEIVER,
        destination_normalized=normalize_address(TRON_RECEIVER, NetworkCode.TRC20),
        token_contract=None,
        required_confirmations=1,
    )
    respx.get(url__regex=r"https://bybit\.example/v5/asset/deposit/query-record.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "retCode": 0,
                "retMsg": "success",
                "result": {
                    "rows": [
                        {
                            "coin": "USDT",
                            "chain": "TRX",
                            "amount": "10",
                            "txID": "0xbybit",
                            "status": 3,
                            "toAddress": TRON_RECEIVER,
                            "tag": "",
                            "confirmations": "20",
                            "successAt": str(int(utcnow().timestamp() * 1000)),
                        }
                    ]
                },
            },
        )
    )
    adapter = BybitAdapter(
        ProviderCredentials(api_key="k", api_secret="s"), base_url="https://bybit.example"
    )
    transactions = await adapter.find_transactions(exp)
    await adapter.aclose()
    assert len(transactions) == 1
    assert verify_transaction(exp, transactions[0]).outcome is VerificationOutcome.VERIFIED


@respx.mock
async def test_okx_only_configured_states_are_credited():
    exp = expectation(
        provider=ProviderCode.OKX,
        network=NetworkCode.TRC20,
        destination=TRON_RECEIVER,
        destination_normalized=normalize_address(TRON_RECEIVER, NetworkCode.TRC20),
        token_contract=None,
        required_confirmations=1,
    )
    respx.get(url__regex=r"https://okx\.example/api/v5/asset/deposit-history.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "0",
                "msg": "",
                "data": [
                    {
                        "depId": "88165462",
                        "ccy": "USDT",
                        "chain": "USDT-TRC20",
                        "amt": "10",
                        "to": TRON_RECEIVER,
                        "txId": "0xokx",
                        "ts": str(int(utcnow().timestamp() * 1000)),
                        "state": "1",  # credited but not final
                        "actualDepBlkConfirm": "20",
                    }
                ],
            },
        )
    )
    adapter = OKXAdapter(
        ProviderCredentials(api_key="k", api_secret="s", passphrase="p"),
        base_url="https://okx.example",
    )
    transactions = await adapter.find_transactions(exp)
    await adapter.aclose()
    # Default credited state is "2" only, so state "1" must not be credited.
    assert transactions[0].is_successful is False
    assert verify_transaction(exp, transactions[0]).outcome is VerificationOutcome.FAILED_TRANSACTION


@respx.mock
async def test_provider_error_is_wrapped_and_never_leaks_raw_body():
    from app.core.exceptions import ProviderError

    respx.get(url__regex=r"https://okx\.example/api/v5/asset/deposit-history.*").mock(
        return_value=httpx.Response(200, json={"code": "51000", "msg": "Parameter error"})
    )
    adapter = OKXAdapter(
        ProviderCredentials(api_key="k", api_secret="s", passphrase="p"),
        base_url="https://okx.example",
    )
    with pytest.raises(ProviderError) as exc_info:
        await adapter.find_transactions(expectation(provider=ProviderCode.OKX))
    await adapter.aclose()
    # Technical detail is preserved internally, generic message is customer-safe.
    assert "51000" in str(exc_info.value)
    assert exc_info.value.safe_message == "We could not reach the payment provider. Please try again."
