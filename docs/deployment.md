# Deployment

## Prerequisites

- PostgreSQL 14+ (Supabase works; see the pooler note below)
- Redis 6+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Python 3.12+ (or use the provided Docker image)

## 1. Configuration

```bash
cp .env.example .env
```

Generate the two secrets:

```bash
python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"  # SECURITY_SECRETS_ENCRYPTION_KEY
python -c "import secrets;print(secrets.token_urlsafe(32))"                               # SECURITY_API_KEY_PEPPER
```

> **Back up the encryption key.** It decrypts provider credentials, stock
> payloads and delivered goods. Losing it makes all of them unreadable. Changing
> the API-key pepper invalidates every issued reseller key.

Set `TELEGRAM_BOOTSTRAP_ADMIN_IDS` to your own Telegram user id (get it from
[@userinfobot](https://t.me/userinfobot)) so you can open `/admin` on a fresh
deployment.

## 2. Supabase

Project Settings → Database → Connection string (URI). Convert the scheme:

```
postgres://...            →  postgresql+asyncpg://...
```

Supabase offers two poolers, and the distinction matters:

| Use | Port | Note |
|---|---|---|
| Migrations | 5432 (session) | Prepared statements are supported |
| Application | 6543 (transaction) | Set `DATABASE_STATEMENT_CACHE_SIZE=0` |

The transaction-mode pooler (pgbouncer) rejects prepared statements, which is
why the application sets `statement_cache_size=0` on its asyncpg connections.

## 3. Migrate and seed

```bash
alembic upgrade head
python -m scripts.seed          # roles, providers, payment methods
python -m scripts.seed --demo   # additionally sample catalog (development only)
```

The seed is idempotent and never overwrites operator configuration. Payment
methods are created **disabled with no receiving address** by design.

## 4. Run

Locally:

```bash
make run-bot      # or: python -m app.main bot
make run-api
make run-worker
python -m app.main all   # everything in one process, for development
```

With Docker:

```bash
docker compose up -d --build
```

## 5. Railway

Create three services from this repository. Each uses the same Dockerfile and
differs only in its start command:

| Service | Start command | Replicas |
|---|---|---|
| `api` | `alembic upgrade head && python -m app.main api` | 1+ |
| `bot` | `python -m app.main bot` | **exactly 1** in polling mode |
| `worker` | `python -m app.main worker` | 1+ |

Notes:

- **Only the `api` service runs migrations**, so bot and worker replicas never
  race on a schema change.
- **The bot must be a single replica in polling mode.** Telegram delivers each
  update once, so two pollers would fight over updates. To scale the bot, switch
  to webhook mode by setting `TELEGRAM_WEBHOOK_BASE_URL`, then run as many
  replicas as you like behind the load balancer.
- Workers scale horizontally safely: every loop is idempotent and guarded by
  database constraints. To scale a specific loop, run a service with
  `python -m app.main worker --only payment_verification`.
- Add the Railway PostgreSQL and Redis plugins, or point at Supabase, and set
  the same environment variables on all three services.
- Health check path for the `api` service: `/health`.

## Health endpoints

| Endpoint | Meaning |
|---|---|
| `GET /health` | Process is alive. Use for liveness. |
| `GET /ready` | Database and Redis are reachable. Use for load-balancer gating. |

`/ready` returns `503` when the database is unreachable, and `degraded` (still
`200`) when only Redis is down, because Redis loss does not endanger financial
state.

## Scaling notes

| Component | How to scale |
|---|---|
| API | Horizontally; fully stateless |
| Bot (polling) | Single replica only |
| Bot (webhook) | Horizontally |
| Verification worker | Horizontally; advisory locks avoid duplicate provider calls |
| Delivery worker | Horizontally; the per-order-item unique constraint protects it |
| Webhook worker | Horizontally |
| PostgreSQL | Vertically, then read replicas for analytics |

## Post-deployment checklist

- [ ] `/health` and `/ready` both return `200`
- [ ] `/admin` opens for a bootstrap admin id
- [ ] Provider credentials configured, **Test connection** passes
- [ ] Receiving address and token contract set for each enabled method
- [ ] A test order completes end to end with a small real payment
- [ ] `SECURITY_SECRETS_ENCRYPTION_KEY` backed up somewhere safe
- [ ] Database backups enabled and a restore rehearsed
- [ ] `DEBUG=false` and `ENVIRONMENT=production`
