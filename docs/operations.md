# Operations

## Daily

Open `/admin`. The dashboard leads with what needs a decision:

| Item | Meaning | Action |
|---|---|---|
| ⚠️ Manual review | A payment arrived but could not be auto-credited | Payments → Review |
| 🧮 Reconciliation | An anomaly the workers refused to resolve alone | Reconciliation |
| ❌ Failed delivery | Payment is fine, fulfilment is not | Orders → Fulfilling |
| 📦 Low stock | A product is about to sell out | Products → Add stock |
| 💔 Provider health | An integration is failing | Providers → Test connection |

## Reviewing a payment

**Payments → Review → \<payment\>** shows the real evidence: what was expected,
what was observed, and which specific check failed. Common cases:

| Reason | What happened | Usual action |
|---|---|---|
| `underpaid` | Less arrived than the order required | Ask the customer to top up, or refund |
| `overpaid` | More arrived than required | Deliver and refund the difference |
| `wrong_network` | Sent on a chain we did not quote | Manual recovery; often not recoverable |
| `duplicate` | The transaction already paid another order | Refuse. This is the double-spend guard working |
| `memo_mismatch` | TON payment without the right comment | Match manually against the sender |
| `outside_window` | Paid long after expiry | Approve if the funds are genuinely ours |

**Recheck** re-runs a real verification pass — try it first, since a payment
that simply arrived late will now verify on its own.

**Approve** requires the `payments.approve` permission, a written reason and a
confirmation. It is refused outright if the order already consumed a
transaction, so a manual approval can never double-credit an order.

## Refunds

**Orders → \<order\> → Refund**, or **Refunds** from the dashboard.

Refunds are a separate financial event from payment verification. A verified
payment is never rewound: the money arrived, and the ledger keeps saying so. A
refund is recorded alongside it as its own entry.

The flow is deliberately four steps:

1. **Request** — enter an amount and a reason (`10.00 duplicate payment`), or
   just a reason to refund the full remaining balance
2. **Confirm** — nothing is recorded until you confirm
3. **Approve** — authorises you to send the funds
4. **Mark sent** — attach the transaction reference of the transfer you made

The platform never moves funds itself. Sending crypto needs withdrawal-capable
credentials, which this platform never asks for and never stores; automating it
would mean holding keys that can drain the business. So you send the transfer
from your own wallet or exchange and record the proof here.

Guards that keep the refund ledger truthful:

- an unpaid order cannot be refunded
- you cannot refund more than actually arrived
- partial refunds accumulate and cannot overdraw the balance
- a refund cannot be completed without an external reference
- completing twice journals once
- a fully refunded order becomes `refunded`; a partial refund leaves the order
  alone, because the customer keeps what they bought

## Adding stock

**Products → \<product\> → Add stock**, then send the items one per line. They
are encrypted before storage, de-duplicated per product by fingerprint (so
importing the same file twice cannot sell one key to two customers), and your
message is deleted from the chat immediately afterwards.

## Editing a product

**Products → \<product\> → Edit** opens one screen holding every editable
field: name, prices, descriptions, the feature and requirement lists, quantity
limits, the low-stock threshold, sort priority and the reseller tier.

Each field is validated before it is saved, so a mistyped price is refused
rather than stored. Two rules are enforced here rather than left to care:

- reseller sales cannot be switched on until a wholesale price exists,
  otherwise resellers would buy at the retail price
- a maximum quantity below the minimum is corrected on save, so a product can
  never end up impossible to order

Every change is audit-logged with the field, the old value and the new one.

## Product images

**Products → \<product\> → Media**, then send a photo. Telegram's own
`file_id` is stored, so no image hosting is needed and the picture is re-sent
instantly on the product page. The first image is the one buyers see; use
**Make primary** to promote another, and **Remove** to delete one.

Only photos are accepted — a file or a text message is refused and the upload
stays open, so you can simply send the photo again.

## Categories

**Categories** lists every category with the number of products in it.

- **New category** — send a name; the slug is generated for you and stays
  unique. A name with no Latin characters still gets a usable slug.
- **Hide from shop** takes a category out of the storefront without touching
  the products in it.
- **Archive** is offered only once no product uses the category. Archiving one
  that products still point at would silently drop those products out of the
  shop, so it is refused rather than cascaded.

Move products between categories from **Products → \<product\> → Edit →
Change category**.

## Rotating a receiving address

1. **Providers → Methods → \<method\> → Change address**
2. The address is checked against the network's encoding — a Base58Check or
   Bech32 checksum failure is rejected before it can go live
3. Confirm — the change is audit-logged

Payments already in flight keep their original destination because each intent
froze it at creation. Only new intents use the new address.

## Setting a quote rate

**Providers → Methods → \<method\> → Set quote rate**, then send how much one
unit of the asset is worth in the currency your products are priced in.

A stablecoin priced in itself is `1` and needs nothing further. BTC and LTC have
no rate we can invent, so they ship at `0` and **cannot be enabled** until you
set one — otherwise a 15.00 order would ask the customer for 15 BTC.

The confirmation screen shows what a 100.00 order would cost at the new rate, so
a misplaced decimal point is visible before you accept it. The rate is frozen
onto each payment when it is created, so changing it never re-prices a payment
already waiting. The method screen warns once a volatile rate is over six hours
old.

## Rotating the encryption key

1. Add a new key as `SECURITY_SECRETS_ENCRYPTION_KEY`
2. Move the old key into `SECURITY_SECRETS_PREVIOUS_KEYS`
3. Deploy — old ciphertext still decrypts, new writes use the new key
4. Re-save provider credentials to migrate them onto the new key

Never remove an old key until everything encrypted with it has been re-written.

## Maintenance mode

**Settings → Enable maintenance** closes the storefront. Staff keep full access
and **the workers keep running**, so payments already in flight continue to be
verified and delivered. No active order is left broken by turning it on.

## Reconciliation

The reconciliation worker files anomalies for a human every 10 minutes:

| Kind | Meaning |
|---|---|
| `stuck_review` | Under review for more than 24 hours |
| `stuck_delivery` | Paid but undelivered after 2 hours |
| `orphan_payment` | Verified with no consumed transaction |
| `unmatched_transaction` | A consumed transaction whose intent is not verified |
| `expired_with_funds` | The window expired after money was detected |
| `amount_mismatch` | Under/overpayment |
| `wrong_network` | Paid on the wrong chain |

Each is deduplicated, so a recurring condition raises one item rather than one
per scan.

## Integrity audit

Run the financial integrity audit periodically, and always after a restore:

```bash
make audit          # or: python -m scripts.audit_financial
```

It checks twelve invariants that must hold in any healthy deployment, including
the ones that matter most:

- no transaction is consumed twice
- no order consumes two transactions
- no verified payment is underpaid
- **no completed delivery lacks a verified payment**
- no order item is delivered twice
- no stock item has two active reservations
- every verified payment is journalled exactly once

A non-zero exit code means an invariant is violated and needs investigation
before trading continues.

## Smoke testing a deployment

```bash
make smoke KEY=rt_live_... URL=https://your-host
```

Exercises health, authentication uniformity, product exposure, idempotent order
creation and the payment-integrity invariants through the public API. It never
fabricates a payment.

## Backup and recovery

**What must be backed up**

1. The PostgreSQL database — the source of truth for everything financial
2. `SECURITY_SECRETS_ENCRYPTION_KEY` — without it, the backup is unreadable

**Restore**

```bash
pg_restore -d "$DATABASE_URL" backup.dump
alembic upgrade head          # apply any migrations newer than the backup
python -m app.main worker     # workers resume from database state
```

Redis needs no restore. Rebuilt from empty it loses caches, advisory locks and
in-progress FSM state; customers may have to re-enter a half-finished checkout
step, and no payment, order or delivery is affected.

**After restoring, reconcile:** run the workers and review the reconciliation
queue. Any payment that arrived while the system was down will be detected on
the next verification pass, because the payment window includes a configurable
late-payment grace period.

## Incident playbook

| Symptom | Diagnosis | Response |
|---|---|---|
| Payments not verifying | Providers → Health | Test connection; check RPC quota |
| Every payment fails one check | Method misconfiguration | Verify address and token contract |
| Deliveries retrying | Orders → Fulfilling | Check stock; look at `last_error` |
| API returning 429 | Reseller exceeding limits | Raise their limit or ask them to back off |
| Webhooks failing | Reseller endpoint down | It auto-disables after 20 failures; tell them |
| Bot unresponsive | Check bot logs | Verify a single polling replica; check the token |

## Log queries

Logs are structured JSON, so filtering is direct:

```bash
# One customer journey, end to end
jq 'select(.correlation_id == "tg_abc123")' < logs.json

# Everything about one order
jq 'select(.order == "TG-10284")' < logs.json

# Payments that failed verification
jq 'select(.event == "payment.verification_attempt" and .outcome != "verified")' < logs.json

# Double-spend attempts blocked
jq 'select(.event == "payment.duplicate_transaction_blocked")' < logs.json

# High-risk admin actions
jq 'select(.event == "admin.action")' < logs.json
```
