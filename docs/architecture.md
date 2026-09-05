# Architecture

## Overview

Three process roles share one codebase and one database. Each can be scaled
independently; all of them are stateless apart from PostgreSQL and Redis.

```
                    ┌──────────────┐
   Telegram ───────▶│     bot      │──┐
                    └──────────────┘  │
                                      │   ┌──────────────┐
   Resellers ──────▶┌──────────────┐  ├──▶│  PostgreSQL  │  source of truth
   (HTTPS)          │     api      │──┤   └──────────────┘
                    └──────────────┘  │
                                      │   ┌──────────────┐
                    ┌──────────────┐  ├──▶│    Redis     │  locks, cache,
   Exchanges  ◀────▶│   workers    │──┘   └──────────────┘  rate limits, FSM
   Blockchains      └──────────────┘
```

| Role | Entrypoint | Responsibility |
|---|---|---|
| `bot` | `python -m app.main bot` | Customer UI and admin panel (aiogram 3) |
| `api` | `python -m app.main api` | Reseller REST API (FastAPI) |
| `worker` | `python -m app.main worker` | Verification, delivery, webhooks, reconciliation |

## Layering

```
app/
  bot/          Telegram customer UI: handlers, keyboards, middlewares, states
  admin/        Telegram admin panel: handlers, RBAC, audit, analytics
  api/          FastAPI reseller API: routes, schemas, dependencies, middleware
  core/         config, logging, security, money, time, redis, exceptions
  db/           SQLAlchemy models and repositories
  domain/       business logic: orders, payments, inventory, coupons, ...
  integrations/ provider adapters: binance, bybit, okx, blockchain
  workers/      background loops
  i18n/         UI string catalogue (English + Bengali)
```

The dependency direction is strictly inward: `bot`, `admin`, `api` and
`workers` depend on `domain`; `domain` depends on `db` and `core`; `core`
depends on nothing internal. A handler never talks to a provider directly and
never writes a status column by hand.

## The payment pipeline

This is the part that matters most, so it is worth stating precisely.

```
customer chooses a method
        │
        ▼
PaymentService.create_intent()
        │  freezes the expectation: amount, asset, destination, contract,
        │  memo, required confirmations. Written once, never updated.
        ▼
customer pays, taps "I've paid" (optionally submits a txid)
        │  the submitted reference is a LOOKUP HINT, never proof
        ▼
PaymentVerificationWorker (or a customer-triggered refresh)
        │
        ├─▶ adapter.find_transactions()   observes, decides nothing
        │       normalises the provider payload into ObservedTransaction
        │
        ├─▶ verify_transaction()          pure function, no I/O
        │       success → network → asset → contract → receiver → memo →
        │       amount → payment window → confirmations
        │
        ├─▶ payment_consumptions.claim()  UNIQUE(fingerprint) INSERT
        │       this is the moment a transaction becomes spent
        │
        └─▶ intent → VERIFIED, order → PAYMENT_VERIFIED, ledger entry
                │
                ▼
        DeliveryWorker: allocate stock → decrypt payload → send → complete
```

Three properties fall out of this shape:

1. **Verification is independent.** Nothing the customer sends can mark a
   payment as received. The only path to `VERIFIED` runs through a real
   provider observation that satisfies every check.
2. **A transaction is spendable once.** The claim is a database INSERT against
   a UNIQUE constraint. Two concurrent workers, two processes, or a retry after
   a crash all converge on exactly one winner.
3. **Delivery cannot precede payment.** `DeliveryService.assert_paid()` checks
   the `payment_intents` table, not the order's status column, so a corrupted
   or hand-edited order status still cannot cause an unpaid delivery.

## State machines

Order and payment statuses are never assigned directly. Every change goes
through `app/domain/state_machines.py`, which validates the transition against
an explicit table and raises `InvalidStateTransition` otherwise.

A single verification pass often establishes in one step what the payment state
machine models as several (`detecting → detected → verifying → verified`). The
service walks the canonical path rather than adding shortcut edges, so the
transition table stays the single source of truth.

Notably, `VERIFIED` has **no outgoing transitions**. Money that arrived is never
un-received; corrections happen through the refund flow, which keeps its own
records.

## Concurrency

| Risk | Mechanism |
|---|---|
| Same transaction credited twice | `UNIQUE(payment_consumptions.fingerprint)` |
| Two buyers, one stock item | `SELECT … FOR UPDATE SKIP LOCKED` + `UNIQUE(inventory_item_id, status)` |
| Duplicate delivery | `UNIQUE(deliveries.order_item_id)` |
| Duplicate coupon redemption | `UNIQUE(coupon_usage.order_id)` |
| Duplicate reseller order | `UNIQUE(orders.reseller_id, idempotency_key)` |
| Duplicate webhook delivery | `UNIQUE(webhook_deliveries.endpoint_id, event_id)` |
| Double-journalled money | `UNIQUE(ledger_entries.dedupe_key)` |
| Wasted duplicate provider calls | Redis advisory lock (an optimisation only) |

The Redis lock is deliberately *not* load-bearing. If Redis is unavailable the
locks degrade open and the database constraints still hold, so payments keep
being processed correctly rather than halting.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| Provider/RPC outage | `PROVIDER_ERROR` outcome, backoff, keep polling; never a customer-visible failure |
| Redis down | Locks and rate limits degrade open; FSM falls back to memory; financial state unaffected |
| Worker crash | All state is in PostgreSQL; the next iteration resumes from the database |
| Delivery failure | Payment stays credited, delivery retries with backoff, then escalates to manual review |
| Telegram outage | Deliveries still complete; notifications retry from the notifications table |
| Database transient error | Transaction rolls back whole; no partial order can exist |

## Money

Every amount is a `Decimal` end to end, stored as `NUMERIC(30, 8)`. On-chain
amounts are compared as integers in the asset's base units. `app/core/money.py`
raises a `TypeError` if a `float` is ever passed in, so the mistake cannot be
made silently.

## Observability

A correlation id is created at the edge (Telegram update or HTTP request) and
propagated through a `ContextVar` into every log line, and is persisted on
orders, payment intents, verification attempts and deliveries. One id traces a
customer action all the way to its delivery.

Logs are structured JSON with defensive secret scrubbing: even if a credential
were passed to a log call by mistake, it is redacted before rendering.
