# Payment providers and verification

## The rule everything else follows

**A payment is credited only when an independent observation from the provider
or the chain satisfies every configured check.** Nothing a customer sends — a
tapped button, a pasted transaction id, a screenshot — can move a payment
towards verified on its own. A submitted transaction id is a *lookup hint*: it
tells the system where to look, and the system still validates every field
against the chain.

## Verification checks, in order

| # | Check | Failure outcome |
|---|---|---|
| 1 | Transaction succeeded | `FAILED_TRANSACTION` |
| 2 | Network matches | `WRONG_NETWORK` |
| 3 | Asset symbol matches | `WRONG_ASSET` |
| 4 | Token contract / mint matches | `WRONG_ASSET` |
| 5 | Receiver matches our destination | `WRONG_RECEIVER` |
| 6 | Memo matches (when required) | `MEMO_MISMATCH` |
| 7 | Amount matches exactly | `UNDERPAID` / `OVERPAID` |
| 8 | Inside the payment window | `OUTSIDE_WINDOW` |
| 9 | Confirmations satisfied | `PENDING_CONFIRMATION` |
| 10 | Transaction not already spent | `DUPLICATE` |

Check 4 is the one that stops the most common attack. Anyone can deploy a token
whose symbol is `USDT`. Matching on the symbol alone would credit worthless
tokens as real money, so the platform matches on the **contract address**
(EVM/TRON), **mint** (Solana) or **jetton master** (TON) configured for that
payment method. A method with no contract configured is treated as a native
asset.

Check 10 is not a code check but a database INSERT against
`UNIQUE(payment_consumptions.fingerprint)`, because only the database can make
it atomic across concurrent workers.

## Amount matching

Amounts are compared first as exact integers in the asset's base units — the
chain's own representation — and only then as quantised `Decimal`s. A shortfall
of a single base unit is an underpayment.

Underpayment is **never** treated as full payment. `PAYMENT_UNDERPAYMENT_TOLERANCE`
exists so an operator can deliberately absorb a known fee, and defaults to `0`.
Overpayment is never silently pocketed either: it goes to review so an operator
decides.

## Implemented providers

Each adapter declares its real capabilities. Where a capability is absent, the
platform routes the payment to manual review rather than emulating it.

### Binance — two separate, non-interchangeable integrations

**Wallet deposit history** — `GET /sapi/v1/capital/deposit/hisrec`
([docs](https://developers.binance.com/docs/wallet/capital/deposite-history)).
HMAC-SHA256 signing with an `X-MBX-APIKEY` header. Reports on-chain deposits
into the account with `txId`, `network`, `address`, `addressTag`, `amount`,
`coin`, `status` and `confirmTimes`. Only `status = 1` (success) is credited.
The endpoint covers a 90-day window per query.

**Binance Pay merchant order query** — `POST /binancepay/openapi/order/query`
([docs](https://developers.binance.com/docs/binance-pay/api-common)). Headers
`BinancePay-Timestamp`, `BinancePay-Nonce` (32 chars), `BinancePay-Certificate-SN`,
`BinancePay-Signature` = `hex(HMAC_SHA512(secret, "timestamp\nnonce\nbody\n")).upper()`.
Reports the status of a Pay order **this platform created**, keyed by
`merchantTradeNo`. Requires a Binance Pay *merchant* account.

These answer different questions and need different credentials. Configuring one
does not give you the other.

> **Documented limitation.** Without a Binance Pay merchant account there is no
> documented way to bind an arbitrary customer-supplied Pay reference to a
> specific order. Deployments in that position should verify exchange payments
> through on-chain deposits, or leave them to manual review. The platform does
> not fake this capability.

### Bybit — V5

`GET /v5/asset/deposit/query-record` (on-chain) and
`GET /v5/asset/deposit/query-internal-record` (Bybit-to-Bybit)
([docs](https://bybit-exchange.github.io/docs/v5/asset/deposit/deposit-record)).
Auth: `X-BAPI-SIGN = HMAC_SHA256(secret, timestamp + apiKey + recvWindow + queryString)`
with `X-BAPI-API-KEY`, `X-BAPI-TIMESTAMP`, `X-BAPI-RECV-WINDOW`. Only deposit
status `3` (success) is credited. Internal transfers have no chain and no
confirmations, so a method using them must be configured with the
`exchange_internal` network and `0` required confirmations.

### OKX — V5

`GET /api/v5/asset/deposit-history` ([docs](https://www.okx.com/docs-v5/en/)).
Auth: `OK-ACCESS-SIGN = base64(HMAC_SHA256(secret, timestamp + method + requestPath + body))`
plus `OK-ACCESS-KEY`, `OK-ACCESS-TIMESTAMP` (ISO-8601 with milliseconds) and
`OK-ACCESS-PASSPHRASE`.

> Deposit `state` semantics differ by currency and have changed across API
> revisions, so the states counted as credited are **configurable per
> deployment** (`config.credited_states`, default `["2"]`). Verify the current
> meaning against OKX's documentation before changing it. The configured
> confirmation requirement is enforced regardless, so a state mapping alone can
> never credit an under-confirmed deposit.

### EVM chains — BEP20, ERC20, Polygon, Arbitrum, Avalanche C-Chain

Standard JSON-RPC: `eth_getTransactionByHash`, `eth_getTransactionReceipt`,
`eth_blockNumber`, `eth_getBlockByNumber`. ERC-20 transfers are read from the
receipt logs matching
`keccak256("Transfer(address,address,uint256)")` =
`0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef`.
The log's `address` field is the emitting contract, which is what makes a
counterfeit token fail.

> **Documented limitation.** Plain JSON-RPC has no method to enumerate incoming
> transfers by address, so the customer must submit the transaction hash. Every
> field is still verified against the chain afterwards; the hash only says
> *where to look*. Adding an indexer would remove this requirement.

### TRON — TRC20

Full-node HTTP API: `/wallet/gettransactionbyid`,
`/wallet/gettransactioninfobyid`, `/wallet/getnowblock`. TRC20 transfers come
from the transaction-info logs; contract and account addresses are converted
between 21-byte hex and base58check. A TronGrid API key raises the rate limit.
Also requires a customer-submitted hash.

### Solana

JSON-RPC `getTransaction` (jsonParsed) and `getSlot`. SPL transfers are read
from the **settled** `preTokenBalances`/`postTokenBalances` delta rather than by
parsing instructions, which makes them immune to instruction-level tricks such
as a transfer followed by a claw-back in the same transaction. Matching is on
the token `mint`. Depth is derived from the slot difference.

### TON

Toncenter v3 `/api/v3/jettonTransfers` and `/api/v3/transactions`. This adapter
*can* enumerate incoming transfers, so no customer-submitted hash is needed.

> **Important.** A TON receiving wallet is normally shared by all customers, so
> the transfer's **comment/memo is what binds a payment to an order**. TON
> methods must be configured with `requires_memo`, and the platform enforces the
> reference match. Without it, two customers paying the same amount could not be
> told apart.

### Bitcoin and Litecoin

Esplora REST: `/api/tx/{txid}`, `/api/address/{addr}/txs`,
`/api/blocks/tip/height`
([docs](https://github.com/Blockstream/esplora/blob/master/API.md)).
Each output paying the receiving address becomes a separate, independently
consumable payment. Amounts are integer satoshis. An unconfirmed transaction is
reported so the customer sees "detected", but carries zero confirmations and
therefore cannot be credited.

## Credentials

Configured in the Telegram admin panel (**Providers → Configure credentials**),
never in environment variables. They are encrypted with
`SECURITY_SECRETS_ENCRYPTION_KEY` (Fernet) before storage, are never returned to
any UI, and the operator's message containing them is deleted from the chat
immediately after storage.

**Always use read-only API keys.** This platform never needs withdrawal
permission, and you should not grant it. Required permissions:

| Provider | Permission |
|---|---|
| Binance | Enable Reading |
| Bybit | Assets — read |
| OKX | Read (funding), plus a passphrase |

## Configuring a payment method

1. **Providers → \<provider\> → Configure credentials** (exchange providers only).
2. **Test connection** — proves connectivity *and* authentication, and shows the
   adapter's declared capabilities.
3. **Methods → \<method\> → Change address** — set the receiving address. This
   is a high-risk action: it requires the `blockchain.manage` permission, a
   confirmation step, and is written to the audit log.
4. Verify the **token contract** matches the asset you intend to accept.
5. Set **required confirmations** appropriately for the chain's finality.
6. **Set quote rate** — how much 1 unit of the payment asset is worth in the
   currency your products are priced in. For a stablecoin priced in itself this
   is `1`; for BTC or LTC it is the live price.
7. **Enable** the method.

Seeded defaults ship **disabled with no address**, so nothing can accept money
until an operator has explicitly said where it should go.

### What blocks enabling

The panel refuses to enable a method until all of these hold, and names the one
that is failing:

| Blocker | Why |
|---|---|
| No receiving address | Nowhere to send the money |
| Address fails validation | One wrong character sends every payment to nobody |
| Token without a contract | The symbol alone cannot tell real USDT from a counterfeit |
| Quote rate not positive | A rate of 0 would price the asset 1:1 against the order |
| Memo required, no template | The chain identifies payments by memo; without one nothing can be attributed |

### Address validation

Addresses are checked against the **encoding**, not just the shape, because a
single mistyped character is the failure mode that actually happens:

| Network | Checked |
|---|---|
| BTC, LTC | Base58Check checksum + version byte, or Bech32/Bech32m checksum with the right HRP and witness version (BIP-350) |
| TRON | Base58Check checksum + `0x41` version byte |
| TON | 48-char user-friendly form with its CRC-16/XMODEM checksum, mainnet flag enforced; or raw `<workchain>:<64 hex>` |
| Solana | Base58 decoding to exactly 32 bytes |
| EVM chains | `0x` + 40 hex characters |

Two of these carry no checksum and cannot be fully verified, which is a property
of the formats rather than a gap here:

* **Solana** addresses are raw 32-byte public keys.
* **EVM** addresses have no checksum unless they use EIP-55 mixed-case, and
  verifying that needs Keccak-256 — not the SHA3-256 in `hashlib`, and there is
  no declared dependency providing it.
* **Raw-form TON** addresses (`0:<hex>`) are plain hex.

In those three cases the operator must compare the address themselves. The admin
screen does not imply a check it did not perform.

### Quote rates

`quote_rate` is the price of one unit of the payment asset in the order
currency. It is **frozen onto each payment intent at creation**, so changing it
never re-prices a payment already awaiting settlement.

A rate of exactly `1` is treated as a peg and never goes stale. Any other rate
is a live price: the method screen shows when it was last set and warns once it
is more than six hours old, because a volatile asset priced from an old quote
mis-charges every order made from it.

The platform does **not** fetch prices. There is no rate feed configured, and
inventing one would mean either trusting an unverified source or pretending to a
freshness the system does not have. The rate is set by an operator, timestamped,
audit-logged, and its age is shown. Setting one requires a confirmation that
states what a 100.00 order would ask for at the new rate.

## Confirmation defaults

| Network | Default | Rationale |
|---|---|---|
| TRC20 | 19 | One SR round; effectively irreversible |
| BEP20 | 15 | ~45 s |
| ERC20 | 12 | Conventional finality |
| Polygon | 128 | Reorgs are deeper here |
| Arbitrum | 20 | L2 depth |
| AVAX-C | 15 | Fast finality |
| Solana | 32 | Slot depth |
| TON | 1 | An included, committed transaction is final |
| BTC | 2 | Raise for high-value orders |
| LTC | 6 | Faster blocks, so more of them |

Raise these for high-value products. They are per-method and frozen onto each
payment intent at creation, so changing them never retroactively alters how an
existing payment is judged.
