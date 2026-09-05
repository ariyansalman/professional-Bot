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

Refunds are separate from verification and are never automatic. Record the
refund against the order, execute the transfer from your own wallet or exchange,
then mark it complete with the external reference. The ledger keeps the payment
and the refund as distinct entries, so the financial history stays truthful.

## Adding stock

**Products → \<product\> → Add stock**, then send the items one per line. They
are encrypted before storage, de-duplicated per product by fingerprint (so
importing the same file twice cannot sell one key to two customers), and your
message is deleted from the chat immediately afterwards.

## Rotating a receiving address

1. **Providers → Methods → \<method\> → Change address**
2. The new address is validated for the network's format
3. Confirm — the change is audit-logged

Payments already in flight keep their original destination because each intent
froze it at creation. Only new intents use the new address.

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
