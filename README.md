# Telegram Digital Commerce Platform

A production-grade Telegram storefront for digital products, with independent
cryptocurrency payment verification, automatic delivery, a full Telegram admin
panel, and a REST API for resellers.

**100% Python.** aiogram 3 · FastAPI · SQLAlchemy 2 async · Alembic · PostgreSQL
· Redis.

---

## What it does

**Customers** browse a catalog in Telegram, pay with USDT on eight networks or
through an exchange, and receive their product automatically the moment the
payment is independently verified.

**Administrators** run the entire business from Telegram: orders, payments,
manual review, products, inventory, users, resellers, coupons, support,
providers, analytics, broadcasts and an audit log.

**Resellers** sell the same catalog through their own bot, website or app via a
scoped, rate-limited, idempotent REST API with signed webhooks.

## The core guarantee

> A payment is credited only when an independent observation from the provider
> or the blockchain satisfies every configured check, and any given transaction
> can be credited exactly once — ever.

Nothing a customer sends can mark a payment as received. A submitted transaction
id is a lookup hint; the system still validates the network, the asset, the
**token contract**, the receiver, the memo, the exact amount in integer base
units, the payment window and the confirmation depth against the chain itself.

Spending a transaction is a database `INSERT` against a `UNIQUE` constraint, so
two concurrent workers, two processes, or a retry after a crash all converge on
exactly one winner. This is [proven under real concurrent PostgreSQL
transactions](tests/integration/test_concurrency_postgres.py), not asserted.

## Payment methods

| Exchange | Blockchain |
|---|---|
| Binance (deposits + Pay) | USDT on TRC20, BEP20, ERC20, Polygon, Arbitrum, AVAX-C, TON, Solana |
| Bybit (on-chain + internal) | Bitcoin, Litecoin |
| OKX | |

Each integration declares its real capabilities. Where a provider genuinely
cannot support something, the limitation is documented and the payment is routed
to manual review — never emulated. See [docs/payments.md](docs/payments.md).

## Quick start

```bash
git clone <repository> && cd professional-Bot
make install
cp .env.example .env      # then fill it in

# generate the two required secrets
python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
python -c "import secrets;print(secrets.token_urlsafe(32))"

make migrate
make seed-demo            # roles, providers, methods + sample catalog
make run-bot              # then send /start, and /admin
```

Or bring the whole stack up with Docker:

```bash
docker compose up -d --build
```

## First-run checklist

1. `/admin` → **Providers** → configure read-only credentials → **Test connection**
2. **Providers → Methods** → set the receiving address, verify the token
   contract, then enable the method
3. **Products** → add a product → add stock → activate it
4. Place a small real test order end to end

Payment methods ship **disabled with no receiving address**, so nothing can
accept money until you have explicitly said where it should go.

## Architecture

```
Telegram ──▶ bot ──┐
                   ├──▶ PostgreSQL   source of truth for all financial state
Resellers ─▶ api ──┤
                   ├──▶ Redis        locks, cache, rate limits, FSM
Providers ◀─ workers ┘
```

Three process roles, one codebase, one database. See
[docs/architecture.md](docs/architecture.md).

## Documentation

| Document | Contents |
|---|---|
| [architecture.md](docs/architecture.md) | Layering, payment pipeline, concurrency, failure behaviour |
| [payments.md](docs/payments.md) | Every provider, its documented endpoints and its real limits |
| [deployment.md](docs/deployment.md) | Supabase, Railway, Docker, scaling, health checks |
| [operations.md](docs/operations.md) | Daily runbook, payment review, refunds, backup and recovery |
| [reseller-api.md](docs/reseller-api.md) | API reference, idempotency, webhook verification |
| [security.md](docs/security.md) | Secret handling, RBAC, attack/defence matrix |
| [testing.md](docs/testing.md) | Test layout and the payment matrix |

## Tests

```bash
make test      # SQLite; PostgreSQL-specific tests skipped
make test-pg   # everything, including real concurrency tests
```

81 tests: the payment failure matrix, adapter normalisation, the full order
lifecycle, real concurrent PostgreSQL transactions, and the reseller API.

## Project layout

```
app/
  bot/          customer Telegram UI
  admin/        Telegram admin panel and RBAC
  api/          FastAPI reseller API
  core/         config, logging, security, money, redis
  db/           models and repositories
  domain/       business logic and the verification engine
  integrations/ provider adapters
  workers/      background loops
  i18n/         English and Bengali UI strings
```

## Design commitments

- Money is `Decimal` end to end; passing a `float` raises a `TypeError`
- Order and payment statuses move only through validated transitions
- `VERIFIED` has no outgoing transitions — corrections go through refunds
- Delivery checks the payment record, not the order's status column
- A delivery failure never reverses a payment
- Financial history is append-only; products and orders are archived, not deleted
- Provider limitations are documented, never emulated
