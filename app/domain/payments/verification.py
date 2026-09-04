"""The payment verification engine.

This module contains the pure decision logic: given an immutable
:class:`PaymentExpectation` and an :class:`ObservedTransaction`, decide whether
the payment may be credited. It performs no I/O and touches no database, which
makes every rule directly testable.

The checks, in order (a failure short-circuits and is recorded):

1. transaction succeeded on the network / at the provider
2. network matches the expectation
3. asset matches
4. token contract / mint matches - never the symbol alone
5. receiver matches the configured destination
6. memo / tag matches when the method requires one
7. amount matches exactly (Decimal + integer base units)
8. confirmations satisfy the configured finality requirement
9. the transaction falls inside the payment window

Duplicate detection is deliberately *not* here: it is a database-level
guarantee (``payment_consumptions.fingerprint`` UNIQUE) applied by the caller,
because only the database can make it atomic across concurrent workers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.core.money import AmountMatch, compare_amounts, from_base_units
from app.domain.enums import VerificationOutcome
from app.domain.payments.fingerprint import normalize_address
from app.domain.payments.types import ObservedTransaction, PaymentExpectation


@dataclass(slots=True)
class VerificationDecision:
    """Outcome plus the evidence behind it."""

    outcome: VerificationOutcome
    #: Per-predicate results, surfaced on the admin payment-review screen.
    checks: dict[str, Any] = field(default_factory=dict)
    #: Internal technical reason. Never rendered to a customer.
    detail: str = ""
    observed_amount: Decimal | None = None
    observed_confirmations: int | None = None
    #: Confirmations still required before the payment can be credited.
    missing_confirmations: int = 0

    @property
    def is_verified(self) -> bool:
        return self.outcome is VerificationOutcome.VERIFIED

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "checks": self.checks,
            "observed_amount": str(self.observed_amount) if self.observed_amount else None,
            "observed_confirmations": self.observed_confirmations,
        }


def _check(passed: bool, expected: Any = None, actual: Any = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"passed": passed}
    if expected is not None:
        entry["expected"] = str(expected)
    if actual is not None:
        entry["actual"] = str(actual)
    return entry


def verify_transaction(
    expectation: PaymentExpectation,
    transaction: ObservedTransaction,
    *,
    underpayment_tolerance: Decimal = Decimal("0"),
    overpayment_tolerance: Decimal = Decimal("0"),
    late_payment_grace: timedelta = timedelta(days=1),
) -> VerificationDecision:
    """Decide whether ``transaction`` satisfies ``expectation``."""
    checks: dict[str, Any] = {}

    # 1. The transaction must have actually succeeded.
    checks["transaction_successful"] = _check(
        transaction.is_successful, actual=transaction.status_label or transaction.is_successful
    )
    if not transaction.is_successful:
        return VerificationDecision(
            outcome=VerificationOutcome.FAILED_TRANSACTION,
            checks=checks,
            detail=f"transaction {transaction.external_id} did not succeed "
            f"(status={transaction.status_label!r})",
        )

    # 2. Network.
    network_ok = transaction.network == expectation.network
    checks["network"] = _check(network_ok, expectation.network.value, transaction.network.value)
    if not network_ok:
        return VerificationDecision(
            outcome=VerificationOutcome.WRONG_NETWORK,
            checks=checks,
            detail=f"expected network {expectation.network.value}, observed {transaction.network.value}",
        )

    # 3. Asset symbol.
    asset_ok = transaction.asset.upper() == expectation.asset.upper()
    checks["asset"] = _check(asset_ok, expectation.asset, transaction.asset)
    if not asset_ok:
        return VerificationDecision(
            outcome=VerificationOutcome.WRONG_ASSET,
            checks=checks,
            detail=f"expected asset {expectation.asset}, observed {transaction.asset}",
        )

    # 4. Token contract / mint. A matching symbol is NOT sufficient: anyone can
    #    deploy a token called "USDT". When the method declares a contract, the
    #    observed transfer must carry exactly that contract.
    if expectation.token_contract:
        observed_contract = normalize_address(transaction.token_contract, transaction.network)
        expected_contract = normalize_address(expectation.token_contract, expectation.network)
        contract_ok = bool(observed_contract) and observed_contract == expected_contract
        checks["token_contract"] = _check(
            contract_ok, expectation.token_contract, transaction.token_contract or "missing"
        )
        if not contract_ok:
            return VerificationDecision(
                outcome=VerificationOutcome.WRONG_ASSET,
                checks=checks,
                detail=(
                    f"token contract mismatch: expected {expected_contract!r}, "
                    f"observed {observed_contract!r}"
                ),
            )
    else:
        checks["token_contract"] = _check(True, "n/a (native asset)", "n/a")

    # 5. Receiver.
    observed_receiver = transaction.to_address_normalized or normalize_address(
        transaction.to_address, transaction.network
    )
    receiver_ok = observed_receiver == expectation.destination_normalized
    checks["receiver"] = _check(receiver_ok, expectation.destination, transaction.to_address)
    if not receiver_ok:
        return VerificationDecision(
            outcome=VerificationOutcome.WRONG_RECEIVER,
            checks=checks,
            detail=(
                f"receiver mismatch: expected {expectation.destination_normalized!r}, "
                f"observed {observed_receiver!r}"
            ),
        )

    # 6. Memo / tag / comment, when the payment method requires one.
    if expectation.memo:
        observed_memo = (transaction.memo or transaction.reference or "").strip()
        memo_ok = observed_memo.upper() == expectation.memo.strip().upper()
        checks["memo"] = _check(memo_ok, expectation.memo, observed_memo or "missing")
        if not memo_ok:
            return VerificationDecision(
                outcome=VerificationOutcome.MEMO_MISMATCH,
                checks=checks,
                observed_amount=transaction.amount,
                detail=f"memo mismatch: expected {expectation.memo!r}, observed {observed_memo!r}",
            )

    # 7. Amount. Compared first as exact integer base units (the chain's own
    #    representation) and only then, if the asset precision differs between
    #    provider and expectation, as quantised Decimals.
    observed_amount = transaction.amount
    if transaction.decimals == expectation.asset_decimals:
        units_delta = transaction.amount_units - expectation.expected_amount_units
        exact_units = units_delta == 0
    else:
        exact_units = False
        units_delta = None

    if exact_units:
        match = AmountMatch.EXACT
    else:
        match = compare_amounts(
            expectation.expected_amount,
            observed_amount,
            underpayment_tolerance=underpayment_tolerance,
            overpayment_tolerance=overpayment_tolerance,
        )
    checks["amount"] = _check(
        match is AmountMatch.EXACT,
        expectation.expected_amount,
        observed_amount,
    )
    checks["amount"]["expected_units"] = str(expectation.expected_amount_units)
    checks["amount"]["observed_units"] = str(transaction.amount_units)
    if units_delta is not None:
        checks["amount"]["units_delta"] = str(units_delta)

    if match is AmountMatch.UNDERPAID:
        shortfall = expectation.expected_amount - observed_amount
        return VerificationDecision(
            outcome=VerificationOutcome.UNDERPAID,
            checks=checks,
            observed_amount=observed_amount,
            observed_confirmations=transaction.confirmations,
            detail=f"underpaid by {shortfall} {expectation.asset}",
        )
    if match is AmountMatch.OVERPAID:
        excess = observed_amount - expectation.expected_amount
        return VerificationDecision(
            outcome=VerificationOutcome.OVERPAID,
            checks=checks,
            observed_amount=observed_amount,
            observed_confirmations=transaction.confirmations,
            detail=f"overpaid by {excess} {expectation.asset}",
        )

    # 8. Payment window. Checked before confirmations so a transaction sent
    #    long after expiry is routed to reconciliation rather than silently
    #    waiting for confirmations.
    sent_at = transaction.timestamp
    window_ok = True
    if sent_at is not None:
        # A small negative skew is tolerated: providers round timestamps and a
        # transaction broadcast moments before the intent row was committed is
        # still legitimately this customer's payment.
        earliest = expectation.created_at - timedelta(minutes=10)
        latest = expectation.expires_at + late_payment_grace
        window_ok = earliest <= sent_at <= latest
        checks["payment_window"] = _check(
            window_ok,
            f"{earliest.isoformat()} .. {latest.isoformat()}",
            sent_at.isoformat(),
        )
        checks["payment_window"]["within_original_window"] = (
            expectation.created_at <= sent_at <= expectation.expires_at
        )
    if not window_ok:
        return VerificationDecision(
            outcome=VerificationOutcome.OUTSIDE_WINDOW,
            checks=checks,
            observed_amount=observed_amount,
            observed_confirmations=transaction.confirmations,
            detail=f"transaction timestamp {sent_at} is outside the accepted window",
        )

    # 9. Confirmations / finality.
    required = max(expectation.required_confirmations, 0)
    confirmations = transaction.confirmations
    confirmations_ok = confirmations >= required
    checks["confirmations"] = _check(confirmations_ok, required, confirmations)
    if not confirmations_ok:
        return VerificationDecision(
            outcome=VerificationOutcome.PENDING_CONFIRMATION,
            checks=checks,
            observed_amount=observed_amount,
            observed_confirmations=confirmations,
            missing_confirmations=required - confirmations,
            detail=f"{confirmations}/{required} confirmations",
        )

    return VerificationDecision(
        outcome=VerificationOutcome.VERIFIED,
        checks=checks,
        observed_amount=observed_amount,
        observed_confirmations=confirmations,
        detail="all checks passed",
    )


def select_best_candidate(
    expectation: PaymentExpectation,
    transactions: list[ObservedTransaction],
    *,
    underpayment_tolerance: Decimal = Decimal("0"),
    overpayment_tolerance: Decimal = Decimal("0"),
    late_payment_grace: timedelta = timedelta(days=1),
) -> tuple[ObservedTransaction | None, VerificationDecision]:
    """Pick the transaction that best satisfies the expectation.

    Ranking preference:
      1. fully verified
      2. pending confirmation (money is on its way)
      3. attributable but needing review (underpaid / overpaid / wrong asset)
      4. not attributable to this expectation at all

    "Attributable" means the transfer actually landed on our configured
    destination. A transfer to a different address is another customer's
    payment, so it is ignored rather than parking this order in manual review.
    A customer-submitted txid is checked with :func:`verify_transaction`
    directly, which does surface ``WRONG_RECEIVER`` to that customer.

    Returns the transaction together with its decision so the caller can record
    exactly what was evaluated.
    """
    if not transactions:
        return None, VerificationDecision(
            outcome=VerificationOutcome.NOT_FOUND,
            checks={},
            detail="no candidate transactions returned by provider",
        )

    ranked: list[tuple[int, ObservedTransaction, VerificationDecision]] = []
    for tx in transactions:
        decision = verify_transaction(
            expectation,
            tx,
            underpayment_tolerance=underpayment_tolerance,
            overpayment_tolerance=overpayment_tolerance,
            late_payment_grace=late_payment_grace,
        )
        if decision.outcome is VerificationOutcome.VERIFIED:
            rank = 0
        elif decision.outcome is VerificationOutcome.PENDING_CONFIRMATION:
            rank = 1
        elif decision.outcome is VerificationOutcome.WRONG_RECEIVER:
            # Landed somewhere else entirely: not this order's payment.
            rank = 3
        elif decision.outcome.needs_review:
            rank = 2
        else:
            rank = 3
        ranked.append((rank, tx, decision))

    ranked.sort(key=lambda item: (item[0], -item[1].amount_units))
    best_rank, best_tx, best_decision = ranked[0]
    if best_rank == 3:
        # Nothing was even close: report not-found so polling continues rather
        # than parking the payment in review over an unrelated transaction.
        return None, VerificationDecision(
            outcome=VerificationOutcome.NOT_FOUND,
            checks=best_decision.checks,
            detail=f"no matching transaction ({best_decision.detail})",
        )
    return best_tx, best_decision


def quote_expected_amount(order_amount: Decimal, quote_rate: Decimal, decimals: int) -> Decimal:
    """Convert an order total into the payment asset amount.

    ``quote_rate`` is the price of one unit of the payment asset expressed in
    the order currency (1 for a stablecoin priced in the same currency). The
    result is rounded *up* at the asset's precision so the customer is never
    asked for less than the order is worth.
    """
    from decimal import ROUND_UP

    if quote_rate <= 0:
        raise ValueError("quote_rate must be positive")
    raw = order_amount / quote_rate
    quantum = Decimal(1).scaleb(-decimals)
    return raw.quantize(quantum, rounding=ROUND_UP)
