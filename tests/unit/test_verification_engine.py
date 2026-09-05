"""Verification engine tests - the payment test matrix from section 132."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.money import base_units
from app.core.timeutils import utcnow
from app.domain.enums import NetworkCode, ProviderCode, VerificationOutcome
from app.domain.payments.fingerprint import normalize_address
from app.domain.payments.types import ObservedTransaction, PaymentExpectation
from app.domain.payments.verification import (
    quote_expected_amount,
    select_best_candidate,
    verify_transaction,
)

USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
RECEIVER = "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"


def make_expectation(**overrides) -> PaymentExpectation:
    now = utcnow()
    defaults = dict(
        intent_id="pi_1",
        reference="TG-10284",
        provider=ProviderCode.TRON,
        network=NetworkCode.TRC20,
        asset="USDT",
        asset_decimals=6,
        expected_amount=Decimal("10.000000"),
        expected_amount_units=base_units("10.000000", 6),
        destination=RECEIVER,
        destination_normalized=normalize_address(RECEIVER, NetworkCode.TRC20),
        token_contract=USDT_TRC20_CONTRACT,
        memo=None,
        required_confirmations=19,
        created_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(minutes=25),
    )
    defaults.update(overrides)
    return PaymentExpectation(**defaults)


def make_tx(**overrides) -> ObservedTransaction:
    now = utcnow()
    defaults = dict(
        provider=ProviderCode.TRON,
        network=NetworkCode.TRC20,
        external_id="a8f3ec0b" + "0" * 56,
        asset="USDT",
        amount_units=base_units("10.000000", 6),
        decimals=6,
        to_address=RECEIVER,
        to_address_normalized=normalize_address(RECEIVER, NetworkCode.TRC20),
        is_successful=True,
        observed_at=now,
        block_time=now - timedelta(minutes=1),
        token_contract=USDT_TRC20_CONTRACT,
        confirmations=25,
    )
    defaults.update(overrides)
    return ObservedTransaction(**defaults)


# --- success path ---------------------------------------------------------


def test_exact_payment_verifies():
    decision = verify_transaction(make_expectation(), make_tx())
    assert decision.outcome is VerificationOutcome.VERIFIED
    assert decision.observed_amount == Decimal("10")
    assert all(check["passed"] for check in decision.checks.values())


def test_verification_records_every_check():
    decision = verify_transaction(make_expectation(), make_tx())
    for key in (
        "transaction_successful",
        "network",
        "asset",
        "token_contract",
        "receiver",
        "amount",
        "confirmations",
        "payment_window",
    ):
        assert key in decision.checks, f"missing evidence for {key}"


# --- failure matrix -------------------------------------------------------


def test_underpayment_is_never_credited():
    decision = verify_transaction(
        make_expectation(), make_tx(amount_units=base_units("8.500000", 6))
    )
    assert decision.outcome is VerificationOutcome.UNDERPAID
    assert not decision.is_verified
    assert decision.outcome.needs_review


def test_underpayment_by_one_base_unit_is_not_credited():
    decision = verify_transaction(
        make_expectation(), make_tx(amount_units=base_units("10.000000", 6) - 1)
    )
    assert decision.outcome is VerificationOutcome.UNDERPAID


def test_overpayment_requires_review():
    decision = verify_transaction(
        make_expectation(), make_tx(amount_units=base_units("12.000000", 6))
    )
    assert decision.outcome is VerificationOutcome.OVERPAID
    assert decision.observed_amount == Decimal("12")


def test_configured_tolerance_can_accept_a_small_shortfall():
    decision = verify_transaction(
        make_expectation(),
        make_tx(amount_units=base_units("9.999000", 6)),
        underpayment_tolerance=Decimal("0.001"),
    )
    assert decision.outcome is VerificationOutcome.VERIFIED


def test_wrong_network_is_rejected():
    decision = verify_transaction(
        make_expectation(), make_tx(network=NetworkCode.BEP20, provider=ProviderCode.EVM)
    )
    assert decision.outcome is VerificationOutcome.WRONG_NETWORK


def test_wrong_asset_symbol_is_rejected():
    decision = verify_transaction(make_expectation(), make_tx(asset="USDC"))
    assert decision.outcome is VerificationOutcome.WRONG_ASSET


def test_fake_token_with_correct_symbol_is_rejected():
    """A token claiming to be USDT but from another contract must not pass."""
    decision = verify_transaction(
        make_expectation(),
        make_tx(asset="USDT", token_contract="TFakeTokenContractAddress000000000"),
    )
    assert decision.outcome is VerificationOutcome.WRONG_ASSET
    assert decision.checks["token_contract"]["passed"] is False


def test_missing_token_contract_is_rejected_when_one_is_expected():
    decision = verify_transaction(make_expectation(), make_tx(token_contract=None))
    assert decision.outcome is VerificationOutcome.WRONG_ASSET


def test_wrong_receiver_is_rejected():
    other = "TOtherReceiverAddress9999999999999"
    decision = verify_transaction(
        make_expectation(),
        make_tx(to_address=other, to_address_normalized=normalize_address(other, NetworkCode.TRC20)),
    )
    assert decision.outcome is VerificationOutcome.WRONG_RECEIVER


def test_failed_transaction_is_rejected():
    decision = verify_transaction(
        make_expectation(), make_tx(is_successful=False, status_label="REVERTED")
    )
    assert decision.outcome is VerificationOutcome.FAILED_TRANSACTION


def test_insufficient_confirmations_stays_pending():
    decision = verify_transaction(make_expectation(), make_tx(confirmations=8))
    assert decision.outcome is VerificationOutcome.PENDING_CONFIRMATION
    assert decision.missing_confirmations == 11
    assert decision.outcome.is_retryable


def test_transaction_far_outside_window_is_flagged():
    now = utcnow()
    decision = verify_transaction(
        make_expectation(
            created_at=now - timedelta(days=10), expires_at=now - timedelta(days=10) + timedelta(minutes=30)
        ),
        make_tx(block_time=now),
        late_payment_grace=timedelta(hours=1),
    )
    assert decision.outcome is VerificationOutcome.OUTSIDE_WINDOW


def test_late_payment_inside_grace_still_verifies_but_is_marked_late():
    now = utcnow()
    expectation = make_expectation(
        created_at=now - timedelta(hours=3), expires_at=now - timedelta(hours=2)
    )
    decision = verify_transaction(
        expectation, make_tx(block_time=now - timedelta(minutes=30)), late_payment_grace=timedelta(days=1)
    )
    assert decision.outcome is VerificationOutcome.VERIFIED
    assert decision.checks["payment_window"]["within_original_window"] is False


def test_memo_mismatch_is_rejected_when_memo_required():
    decision = verify_transaction(
        make_expectation(memo="TG-10284", network=NetworkCode.TON, provider=ProviderCode.TON),
        make_tx(network=NetworkCode.TON, provider=ProviderCode.TON, memo="TG-99999"),
    )
    assert decision.outcome is VerificationOutcome.MEMO_MISMATCH


def test_memo_match_is_accepted():
    decision = verify_transaction(
        make_expectation(memo="TG-10284", network=NetworkCode.TON, provider=ProviderCode.TON),
        make_tx(network=NetworkCode.TON, provider=ProviderCode.TON, memo="tg-10284"),
    )
    assert decision.outcome is VerificationOutcome.VERIFIED


def test_evm_address_case_difference_still_matches():
    addr = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    expectation = make_expectation(
        network=NetworkCode.ERC20,
        provider=ProviderCode.EVM,
        destination=addr,
        destination_normalized=normalize_address(addr, NetworkCode.ERC20),
        token_contract=addr,
        asset_decimals=6,
    )
    tx = make_tx(
        network=NetworkCode.ERC20,
        provider=ProviderCode.EVM,
        to_address=addr.upper(),
        to_address_normalized=normalize_address(addr.upper(), NetworkCode.ERC20),
        token_contract=addr.lower(),
    )
    assert verify_transaction(expectation, tx).outcome is VerificationOutcome.VERIFIED


# --- candidate selection --------------------------------------------------


def test_no_transactions_yields_not_found():
    tx, decision = select_best_candidate(make_expectation(), [])
    assert tx is None
    assert decision.outcome is VerificationOutcome.NOT_FOUND


def test_best_candidate_prefers_the_verified_transaction():
    expectation = make_expectation()
    bad = make_tx(external_id="bad", amount_units=base_units("1.000000", 6))
    good = make_tx(external_id="good")
    tx, decision = select_best_candidate(expectation, [bad, good])
    assert decision.outcome is VerificationOutcome.VERIFIED
    assert tx.external_id == "good"


def test_unrelated_transactions_do_not_park_payment_in_review():
    """A deposit for someone else must not become this order's problem."""
    other = "TSomeoneElseAddress99999999999999"
    tx, decision = select_best_candidate(
        make_expectation(),
        [make_tx(to_address=other, to_address_normalized=normalize_address(other, NetworkCode.TRC20))],
    )
    assert tx is None
    assert decision.outcome is VerificationOutcome.NOT_FOUND


def test_pending_confirmation_beats_needs_review():
    expectation = make_expectation()
    pending = make_tx(external_id="pending", confirmations=2)
    underpaid = make_tx(external_id="under", amount_units=base_units("9.000000", 6))
    tx, decision = select_best_candidate(expectation, [underpaid, pending])
    assert decision.outcome is VerificationOutcome.PENDING_CONFIRMATION
    assert tx.external_id == "pending"


# --- quoting --------------------------------------------------------------


@pytest.mark.parametrize(
    ("order_amount", "rate", "decimals", "expected"),
    [
        ("10", "1", 6, "10.000000"),
        ("10", "0.9995", 6, "10.005003"),  # rounds up, never short-quotes
        ("100", "65000", 8, "0.00153847"),
    ],
)
def test_quote_expected_amount_rounds_up(order_amount, rate, decimals, expected):
    result = quote_expected_amount(Decimal(order_amount), Decimal(rate), decimals)
    assert result == Decimal(expected)
    assert result * Decimal(rate) >= Decimal(order_amount)
