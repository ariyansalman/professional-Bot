# Testing

```bash
make test        # SQLite; PostgreSQL-specific tests are skipped
make test-pg     # everything, including real concurrency tests
make lint
```

## Layout

| Path | Covers |
|---|---|
| `tests/unit/test_verification_engine.py` | The section 132 payment matrix |
| `tests/unit/test_adapters.py` | Provider payload normalisation (HTTP mocked) |
| `tests/unit/test_ux_contract.py` | No dead buttons, state-aware keyboards, every screen state |
| `tests/integration/test_order_payment_flow.py` | Order → payment → delivery |
| `tests/integration/test_concurrency_postgres.py` | Real concurrent transactions |
| `tests/integration/test_bot_flows.py` | Real Telegram updates through the real dispatcher |
| `tests/integration/test_admin_flows.py` | Every admin screen, and its access control |
| `tests/integration/test_reseller_api.py` | Auth, scopes, idempotency, isolation |

## Why the bot-flow tests matter

Importing a handler proves nothing about whether it runs. These tests feed real
`Update` objects through the production middleware chain with Telegram's HTTP
session replaced by a recorder, so the handlers, keyboards and rendered text are
all genuine. They have already caught two bugs that no import check would:
the middleware chain being registered as inner rather than outer (which made the
entire admin panel unreachable), and an admin lookup that triggered an async
lazy load for a brand-new user.

## Why the PostgreSQL tests matter

SQLite cannot express row-level locking or genuinely concurrent transactions, so
the two hardest guarantees can only be proven against PostgreSQL:

- **one transaction, one order** — two workers race to credit the same on-chain
  transaction to two different orders; exactly one wins
- **one item, one buyer** — ten simultaneous buyers compete for the last stock
  item; exactly one order is created

These are skipped without `TEST_DATABASE_URL` and always run in CI.

## Payment matrix coverage

**Verifies:** exact payment, correct contract, sufficient confirmations,
EVM address casing differences, matching memo, late payment inside the grace
window, configured underpayment tolerance.

**Refuses:** underpayment (including by one base unit), overpayment, wrong
network, wrong asset symbol, counterfeit token with the correct symbol, missing
token contract, wrong receiver, failed/reverted transaction, insufficient
confirmations, transaction far outside the window, memo mismatch, duplicate
transaction, unknown transaction, provider error, and pending exchange deposits.

## Writing a payment test

The transport is stubbed; every verification rule still runs for real:

```python
def _stub_adapter(monkeypatch, transactions):
    class StubAdapter:
        provider_code = ProviderCode.TRON
        async def find_transactions(self, expectation, *, reference=None):
            return list(transactions)
        async def aclose(self):
            return None
    monkeypatch.setattr(
        "app.domain.payments.service.build_adapter",
        lambda provider, method=None: StubAdapter(),
    )
```

No test ever fakes a verified payment: it supplies an observation and asserts
what the engine decides about it.

## What is not covered by automated tests

- **Live provider APIs.** Adapters are tested against recorded payload shapes;
  the live endpoints require real credentials. Use **Providers → Test
  connection** in the admin panel against a real account before going live.
- **Real on-chain payments.** Send one small real payment per enabled method
  before opening the shop.
- **Telegram rendering.** Handlers are exercised, but how a screen looks on a
  device is verified by walking the flows manually.
