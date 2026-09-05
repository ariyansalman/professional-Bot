# Reseller API

Base URL: `/api/v1` · Interactive schema: `/api/v1/docs` · OpenAPI: `/api/v1/openapi.json`

## Authentication

Create a key in the bot: **Reseller Center → API Keys → Create API Key**. The
plaintext is shown exactly once — the platform stores only a peppered hash and
cannot show it to you again, nor can an administrator.

```http
Authorization: Bearer rt_live_a1b2c3d4_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Every failure mode — unknown key, revoked key, expired key, suspended account —
returns the same `401`, so the API cannot be used to discover which keys exist.

## Scopes

A key carries only the scopes it was created with:

| Scope | Grants |
|---|---|
| `products.read` | `GET /products`, `GET /products/{id}` |
| `orders.create` | `POST /orders`, `POST /orders/{id}/payment` |
| `orders.read` | `GET /orders`, `GET /orders/{id}` |
| `payments.read` | Payment details on order responses |
| `deliveries.read` | `GET /orders/{id}/delivery` |
| `webhooks.manage` | The `/webhooks` endpoints |

Administrative and financial scopes cannot be self-granted from the bot.

## Errors

```json
{
  "error": {
    "code": "insufficient_stock",
    "message": "Only 2 left in stock.",
    "request_id": "api_9f3c1d8b2a4e5f6071829304"
  }
}
```

| Status | Code | Meaning |
|---|---|---|
| 401 | `unauthenticated` | Missing, invalid, revoked or inactive key |
| 403 | `permission_denied` | The key lacks the required scope |
| 404 | `not_found` | Unknown, or belongs to another reseller |
| 409 | `idempotency_conflict` | Key reused with a different body |
| 409 | `out_of_stock` | Stock ran out during checkout |
| 422 | `validation_error` | Request body failed validation |
| 429 | `rate_limited` | Rate limit exceeded; see `Retry-After` |
| 502 | `provider_error` | A payment provider was unreachable |

Quote `request_id` when contacting support: it traces the exact request through
the platform's logs.

## Idempotency

```http
POST /api/v1/orders
Idempotency-Key: 6f1c2b9a-4e2d-4a1b-9c3e-2f7a8b1d0e5c
```

- Same key, same body → the original order, `200`
- Same key, different body → `409 idempotency_conflict`
- No key → a new order every time

Records are stored in PostgreSQL in the same transaction as the order, so an
operation that rolled back can never leave a stored response behind. They are
retained for `API_IDEMPOTENCY_TTL_SECONDS` (24 h by default).

## Rate limits

Applied per API key, defaulting to `API_DEFAULT_RATE_LIMIT_PER_MINUTE`. A `429`
carries `Retry-After` in seconds. An administrator can raise a specific
reseller's limit.

---

## Endpoints

### `GET /products`

```bash
curl -H "Authorization: Bearer $KEY" \
     "https://your-host/api/v1/products?page=1&per_page=25"
```

```json
{
  "data": [{
    "id": "9c1e...", "sku": "PREMIUM-01", "name": "Premium License",
    "pricing": {
      "currency": "USDT",
      "wholesale_price": "12.00000000",
      "minimum_price": "13.00000000",
      "recommended_price": "18.00000000",
      "list_price": "15.00000000"
    },
    "in_stock": true, "available_quantity": 42,
    "min_quantity": 1, "max_quantity": 10,
    "delivery_type": "stock_item"
  }],
  "meta": {"page": 1, "per_page": 25, "total": 1, "pages": 1, "has_next": false}
}
```

`wholesale_price` is what you are charged. `minimum_price` is the lowest price
you may resell at under the reseller terms.

### `POST /orders`

```bash
curl -X POST -H "Authorization: Bearer $KEY" \
     -H "Idempotency-Key: $(uuidgen)" -H "Content-Type: application/json" \
     -d '{"product_id":"9c1e...","quantity":1,
          "customer_reference":"your-customer-42",
          "reseller_reference":"your-order-1001",
          "payment_method":"usdt_trc20"}' \
     https://your-host/api/v1/orders
```

```json
{
  "id": "3f2a...", "reference": "TG-10284", "status": "payment_pending",
  "total": "12.00000000", "currency": "USDT",
  "payment": {
    "reference": "TG-10284", "status": "awaiting_payment",
    "asset": "USDT", "network": "trc20", "amount": "12.000000",
    "destination": "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE",
    "memo": null, "required_confirmations": 19, "confirmations": 0,
    "expires_at": "2026-09-05T12:30:00Z"
  },
  "delivery_status": "pending"
}
```

Show `payment.destination` and `payment.amount` to your customer. If `memo` is
present it is **mandatory** — the payment cannot be matched without it.

Omit `payment_method` to create the order first and attach a method later with
`POST /orders/{id}/payment?payment_method=...`.

### `GET /orders/{id}`

Accepts the order id or its reference (`TG-10284`). Poll this, or subscribe to
webhooks, to follow payment progress.

Order statuses: `created`, `payment_pending`, `payment_verified`, `fulfilling`,
`delivered`, `completed`, `cancelled`, `expired`, `manual_review`,
`delivery_failed`, `refunded`.

### `GET /orders/{id}/delivery`

```json
{"status": "completed", "delivered_at": "2026-09-05T12:34:56Z",
 "items": ["XXXX-YYYY-ZZZZ-1234"], "attempts": 1}
```

`items` is populated only once the payment is verified and delivery has
completed. Before that the array is empty.

---

## Webhooks

### Registering

```bash
curl -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"url":"https://your-app.com/webhooks/commerce",
          "events":["payment.verified","delivery.completed"]}' \
     https://your-host/api/v1/webhooks
```

The response contains `secret` (`whsec_...`) exactly once. Store it now.
An empty `events` array subscribes to everything. The URL must be HTTPS and must
not resolve to a private, loopback or link-local address.

### Events

`order.created`, `payment.pending`, `payment.detected`, `payment.verified`,
`payment.failed`, `delivery.processing`, `delivery.completed`,
`order.completed`, `order.cancelled`

### Verifying a delivery

Each request carries:

```
X-Event-Id: evt_payment_verified_3f2a1b9c8d7e
X-Event-Type: payment.verified
X-Timestamp: 1789012345
X-Signature: v1=9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08
X-Delivery-Attempt: 1
```

```python
import hashlib, hmac, time

def verify(secret: str, headers: dict, raw_body: bytes) -> bool:
    timestamp = int(headers["X-Timestamp"])
    # Reject replays of an old, captured request.
    if abs(time.time() - timestamp) > 300:
        return False
    payload = f"{timestamp}.{headers['X-Event-Id']}.".encode() + raw_body
    expected = "v1=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers["X-Signature"])
```

Sign over the **raw request body**, before any JSON parsing or re-serialisation.

### Delivery semantics

Delivery is **at-least-once**. Treat `X-Event-Id` as an idempotency key and make
your handler safe to run twice. Retries use exponential backoff
(30 s → 2 m → 5 m → 15 m → 30 m → 1 h → 2 h → 6 h) over 8 attempts. Any `2xx`
counts as success. After 20 consecutive failures the endpoint is disabled
automatically and the reseller is shown a degraded status in the bot.

---

## Integration flow

```
1. GET  /products                    cache the catalog
2. show products to your customer
3. POST /orders                      with an Idempotency-Key
4. show payment.destination + amount (and memo, when present)
5. wait for payment.verified         via webhook, or poll GET /orders/{id}
6. GET  /orders/{id}/delivery        retrieve the goods
7. hand the goods to your customer
```

Direct database access is never provided. Everything a reseller needs is served
through this API.
