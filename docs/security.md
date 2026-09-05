# Security

## Secrets

| Secret | Storage | Notes |
|---|---|---|
| Provider API keys/secrets | Fernet-encrypted in `payment_providers` | Never returned to any UI |
| Stock payloads (keys, credentials) | Fernet-encrypted in `inventory` | Decrypted only at delivery |
| Delivered payloads | Fernet-encrypted in `deliveries` | Re-readable by the buyer only |
| Webhook signing secrets | Fernet-encrypted in `webhook_endpoints` | Shown once at creation |
| Reseller API keys | **Peppered SHA-256 hash only** | The plaintext is never stored |
| Bot token, encryption key, pepper | Environment | Never in the database |

Two consequences worth stating plainly:

- An API key cannot be shown again, **including to an administrator**, because
  the platform does not have it. Admin screens show only the public id.
- Anyone with database access still cannot read provider credentials or stock
  payloads without `SECURITY_SECRETS_ENCRYPTION_KEY`, which lives only in the
  environment.

Log records are scrubbed defensively: keys whose name looks credential-shaped
are redacted, and anything matching a Telegram bot-token pattern is replaced,
even if a caller passes it by mistake.

## Authentication and authorisation

**Customers** are identified by Telegram user id. Every order and payment lookup
re-checks ownership; a customer cannot read another customer's order even with
its identifier.

**Administrators** hold database-assigned roles. `TELEGRAM_BOOTSTRAP_ADMIN_IDS`
grants `SUPER_ADMIN` on first `/admin` so a fresh deployment is reachable, and
the grant is persisted so it is visible and revocable.

**RBAC** is granular: 42 permissions across 7 roles. Being in the admin panel is
never itself authorisation — every handler re-checks its specific permission, so
a hand-crafted callback cannot reach an action the operator lacks.

**Resellers** authenticate with API keys carrying explicit scopes. Administrative
and financial scopes cannot be self-granted from the bot.

## High-risk actions

These require a permission, a written reason, an explicit confirmation and an
audit entry: payment approval and rejection, refunds, receiving-address changes,
token-contract changes, provider credential changes, inventory adjustments,
product archival, user bans, maintenance toggles, broadcasts, role changes and
forced delivery.

Confirmation tokens are bound to the operator who created them, stored in Redis
with a short TTL, and consumed atomically — so a leaked callback cannot be
replayed by someone else, a stale button cannot fire later, and a double-tap
cannot execute the action twice.

## Payment integrity

| Attack | Defence |
|---|---|
| Claiming payment without paying | Only an independent provider observation can verify |
| Reusing one transaction for two orders | `UNIQUE(payment_consumptions.fingerprint)` |
| Paying with a counterfeit "USDT" | Token contract/mint matching, not symbol matching |
| Underpaying | Exact base-unit comparison; underpayment is never credited |
| Paying on a cheaper chain | Network is matched against the frozen expectation |
| Editing the amount after quoting | `expected_amount` is written once and never updated |
| Changing the address to redirect a payment | Frozen per intent; changes are high-risk and audited |
| Racing checkout for the last item | `FOR UPDATE SKIP LOCKED` + unique active reservation |
| Replaying a delivery to get extra keys | `UNIQUE(deliveries.order_item_id)`; allocation is idempotent |
| Double-redeeming a coupon | `UNIQUE(coupon_usage.order_id)` |
| Self-referral | Referrer and referred user are compared; attribution is one-shot |

## API security

- Uniform `401` for every authentication failure, so keys cannot be probed
- Scope enforcement per endpoint
- Per-key rate limiting with `Retry-After`
- Optional IP allowlists per reseller
- Durable idempotency; key reuse with a different body is rejected
- Cross-tenant reads return `404`, not `403`, so ids cannot be confirmed
- Suspending a reseller revokes their live keys immediately

## SSRF protection

Reseller-supplied webhook URLs are validated on registration **and again at
delivery time**, because DNS can change in between. Blocked: non-HTTPS schemes,
credentials in the URL, `localhost`, `.local`/`.internal` suffixes, and any
loopback, private, link-local, reserved or multicast address — which covers
cloud metadata endpoints such as `169.254.169.254`. Redirects are not followed.

## Webhook security

Signed with `v1=hex(hmac_sha256(secret, "<timestamp>.<event_id>.<raw_body>"))`.
Receivers should reject timestamps outside a ±5 minute window to prevent replay,
and use `X-Event-Id` as an idempotency key since delivery is at-least-once.

## Injection and validation

All database access goes through SQLAlchemy with bound parameters; no SQL is
built by string concatenation. API input is validated by Pydantic. Telegram
callback data is parsed through typed `CallbackData` factories, so a malformed
or tampered payload fails to parse rather than reaching a handler with
unexpected values. All user-supplied text rendered into Telegram HTML is
escaped.

## Error handling

Customers and API clients receive only a safe message plus a correlation id.
Stack traces, SQL text, provider payloads, internal paths and secrets never
leave the process — they go to the structured log, keyed by the same id.

## Rate limiting

| Surface | Limit |
|---|---|
| Telegram per user | 20 actions / 10 s |
| Duplicate callbacks | Suppressed within 2 s |
| Reseller API | Per key, default 120/min |
| Outgoing Telegram | Paced to ~25 msg/s globally |
| Provider calls | Advisory locks prevent duplicate polling |

Rate limiting degrades **open** if Redis is unavailable: it is a protection, not
a correctness requirement, and failing closed would take the platform down with
it. The database constraints that guarantee financial correctness are unaffected.

## Reporting a vulnerability

Do not open a public issue. Contact the operator of your deployment privately.
