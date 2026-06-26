## 6. User Data

### Profile

**Endpoint:** `GET /users/profile`
**Token required:** Yes (no other input parameters needed)

```shell
curl 'https://apiv2.nobitex.ir/users/profile' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
  "status": "ok",
  "profile": {
    "firstName": "مهدی",
    "lastName": "رضایی",
    "nationalCode": "011122333",
    "email": "name@example.com",
    "username": "name@example.com",
    "phone": "02142719000-9012",
    "mobile": "09151111111",
    "city": "مشهد",
    "bankCards": [
      { "number": "6037-9900-0000-0000", "bank": "ملی", "owner": "مهدی رضایی", "confirmed": true, "status": "confirmed" }
    ],
    "bankAccounts": [
      { "id": 1999, "number": "0346666666666", "shaba": "IR460170000000346666666666", "bank": "ملی", "owner": "مهدی رضایی", "confirmed": true, "status": "confirmed" }
    ],
    "verifications": {
      "email": true, "phone": true, "mobile": true, "identity": true, "selfie": false,
      "bankAccount": true, "bankCard": true, "address": true, "city": true, "nationalSerialNumber": true
    },
    "pendingVerifications": {
      "email": false, "phone": false, "mobile": false, "identity": false,
      "selfie": false, "bankAccount": false, "bankCard": false
    },
    "options": {
      "fee": "0.35", "feeUsdt": "0.2", "isManualFee": false, "tfa": false, "socialLoginEnabled": false
    },
    "withdrawEligible": true
  },
  "tradeStats": {
    "monthTradesTotal": "10867181.5365000000",
    "monthTradesCount": 3
  },
  "websocketAuthParam": "1987577cdf7c7422dee369e188e276ee"
}
```

This endpoint returns your profile info, bank cards, bank accounts, verification status (email, phone, mobile, etc.), account settings (trading fee, USDT trading fee, etc.), and a summary of your trading stats.

### Generate Blockchain Deposit Address

**Endpoint:** `POST /users/wallets/generate-address`
**Rate limit:** 30 requests/hour

```shell
curl 'https://apiv2.nobitex.ir/users/wallets/generate-address' \
  -X POST \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"currency":"btc", "network": "BSC"}'
```

Example response:
```json
{ "status": "ok", "address": "LRf3vuTMy4UwD5b72G84hmkfGBQYJeTwUs" }
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `currency` <sup>(preferred)</sup> | string | required (or use `wallet`) | Wallet's currency | `btc` |
| `wallet` <sup>(legacy)</sup> | string | required only if `currency` omitted | Wallet ID | `4159` |
| `network` | string | optional | Network | `BSC` |

Note: prefer `currency` for the target wallet. Sending `wallet` is the legacy approach and not recommended.

### Add Bank Card

**Endpoint:** `POST /users/cards-add`
**Rate limit:** 30 requests / 30 minutes

```shell
curl 'https://apiv2.nobitex.ir/users/cards-add' \
  -X POST \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"number":"5041721011111111","bank":"رسالت"}'
```

Response: `{ "status": "ok" }`

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `number` | string | required | Card number | `5041721011111111` |
| `bank` | string | required | Bank name | `رسالت` |

### Add Bank Account

**Endpoint:** `POST /users/accounts-add`
**Rate limit:** 30 requests / 30 minutes

```shell
curl 'https://apiv2.nobitex.ir/users/accounts-add' \
  -X POST \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"number":"5041721011111111","shaba":"IR111111111111111111111111","bank":"رسالت"}'
```

Response: `{ "status": "ok" }`

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `number` | string | required | Account number | `5041721011111111` |
| `shaba` | string | required | IBAN/SHABA number | `IR111111111111111111111111` |
| `bank` | string | required | Bank name | `رسالت` |

### User Limitations

**Endpoint:** `POST /users/limitations`
No input parameters — just send your auth token in the header.

```shell
curl 'https://apiv2.nobitex.ir/users/limitations' \
  -X GET \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json"
```

Example response:
```json
{
  "status": "ok",
  "limitations": {
    "userLevel": "level2",
    "features": {
      "crypto_trade": false,
      "rial_trade": false,
      "coin_deposit": false,
      "rial_deposit": false,
      "coin_withdrawal": false,
      "rial_withdrawal": false
    },
    "limits": {
      "withdrawRialDaily": { "used": "0", "limit": "900000000" },
      "withdrawCoinDaily": { "used": "0", "limit": "2000000000" },
      "withdrawTotalDaily": { "used": "0", "limit": "2000000000" },
      "withdrawTotalMonthly": { "used": "0", "limit": "30000000000" }
    }
  }
}
```

Nobitex users have withdrawal/deposit/trading limits based on their account verification level. Users can raise their level (and limits) by submitting the required documents.

**`features` — account capabilities:**
- `crypto_trade` — can trade crypto-to-crypto
- `rial_trade` — can trade against IRR
- `coin_deposit` — can deposit crypto into the Nobitex wallet
- `rial_deposit` — can deposit IRR into the Nobitex wallet
- `coin_withdrawal` — can withdraw crypto out of the Nobitex wallet
- `rial_withdrawal` — can withdraw IRR to a bank account

**`limits` — account limits (all amounts in IRR):**
- `withdrawRialDaily` — allowed daily IRR withdrawal amount
- `withdrawCoinDaily` — allowed daily crypto withdrawal amount
- `withdrawTotalDaily` — allowed combined daily withdrawal amount (crypto + IRR)
- `withdrawTotalMonthly` — allowed combined monthly withdrawal amount (crypto + IRR)

See [Nobitex account levels](https://nobitex.net/policies/user-levels/) for full details on each level's limits, required documents, etc.

### Wallets — List

**Endpoint:** `GET /users/wallets/list`
**Rate limit:** 20 requests / 2 minutes

```shell
curl 'https://apiv2.nobitex.ir/users/wallets/list' \
  --header "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
  "status": "ok",
  "wallets": [
    {
      "depositAddress": null,
      "depositTag": null,
      "depositInfo": { "FIAT_MONEY": { "address": null, "tag": null } },
      "id": 2693280,
      "currency": "rls",
      "balance": "746212980",
      "blockedBalance": "0",
      "activeBalance": "746212980",
      "rialBalance": 746212980,
      "rialBalanceSell": 746212980
    },
    {
      "depositAddress": "bc1qp8dvtrhgjae6qhjfmvs2dj80ck0hgdjs6ts720",
      "depositTag": null,
      "depositInfo": {
        "BTC-LEGACY": { "address": null, "tag": null },
        "BTC": { "address": "bc1qp8dvtrhgjae6qhjfmvs2dj80ck0hgdjs6ts720", "tag": null },
        "BTCLN": { "address": null, "tag": null },
        "BSC": { "address": null, "tag": null }
      },
      "id": 133778,
      "currency": "btc",
      "balance": "0",
      "blockedBalance": "0",
      "activeBalance": "0",
      "rialBalance": 0,
      "rialBalanceSell": 0
    }
  ]
}
```

**Notes**
1. A wallet for a given crypto only gets created once the user has placed an order in that market, or generated a deposit address for it. (This applies to newer Nobitex listings; older legacy wallets remain regardless.)
2. This endpoint may occasionally fail to return the expected response under traffic-control conditions — retry, or use the [selective wallet list](#wallets--list-selective) endpoint below instead.
3. Use the `type` parameter to choose `spot` or `margin` wallets; `spot` is the default.

### Wallets — List (Selective)

**Endpoint:** `GET /v2/wallets`
**Rate limit:** 15 requests/minute

```shell
curl 'https://apiv2.nobitex.ir/v2/wallets?currencies=rls,btc' \
  --header "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
    "status": "ok",
    "wallets": {
        "RLS": { "id": 133777, "balance": "0E-10", "blocked": "0" },
        "BTC": { "id": 133778, "balance": "0E-10", "blocked": "0" }
    }
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `currencies` | string | optional | Comma-separated list of currencies | `rls,btc` |
| `type` | string | `spot` | Wallet type — spot or margin | `spot` or `margin` |

A wallet for a given crypto only gets created once the user has placed an order in that market, or generated a deposit address. Older legacy wallets remain regardless.

### Balance

**Endpoint:** `POST /users/wallets/balance`
**Rate limit:** 60 requests / 2 minutes

```shell
curl 'https://apiv2.nobitex.ir/users/wallets/balance' \
  -X POST \
  --header "Authorization: Token yourTOKENhereHEX0000000000" \
  --data '{"currency":"ltc"}'
```

Response:
```json
{ "balance": "10.2649975000", "status": "ok" }
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `currency` | string | required | Wallet currency | `ltc` |

**Notes**
1. The balance is returned as a string, since it can carry many decimal places.
2. If you're doing serious arithmetic on these values, use a fixed-precision decimal type rather than floats.

### Wallet Transactions — List

**Endpoint:** `GET /users/wallets/transactions/list`
**Rate limit:** 60 requests / 2 minutes
**Pagination:** Yes (default 50)

```shell
curl 'https://apiv2.nobitex.ir/users/wallets/transactions/list?wallet=4159' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
    "status": "ok",
    "transactions": [
        {
            "currency": "ltc",
            "created_at": "2018-10-17T09:41:08.519151+00:00",
            "calculatedFee": "0",
            "id": 99050,
            "amount": "4.3802000000",
            "description": "خرید 4.400 LTC به قیمت واحد ﷼7450000"
        },
        {
            "currency": "ltc",
            "created_at": "2018-10-04T13:05:01.384902+00:00",
            "calculatedFee": "0",
            "id": 96541,
            "amount": "-1.0000000000",
            "description": "Withdraw to \"Lgn1zc77mEjk72KvXPqyXq8K1mAfcDE6YR\""
        }
    ],
    "hasNext": true
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `wallet` | int | required | Wallet ID | `4159` |

### Transaction History (Selective)

**Endpoint:** `GET /users/transactions-history`
**Rate limit:** 60 requests/hour
**Pagination:** Yes (default 50)

```shell
curl 'https://apiv2.nobitex.ir/users/transactions-history?currency=ltc' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
    "status": "ok",
    "transactions": [
        {
          "id": 99050,
          "amount": "4.3802000000",
          "description": "خرید 4.400 LTC به قیمت واحد ﷼7450000",
          "created_at": "2018-10-17T09:41:08.519151+00:00",
          "balance": "21.2502000000",
          "tp": "buy",
          "calculatedFee": null,
          "type": "معامله",
          "currency": "ltc"
        },
        {
          "id": 96541,
          "amount": "-1.0000000000",
          "description": "Withdraw to \"Lgn1zc77mEjk72KvXPqyXq8K1mAfcDE6YR\"",
          "created_at": "2018-10-04T13:05:01.384902+00:00",
          "balance": "16.8700000000",
          "tp": "withdraw",
          "calculatedFee": null,
          "type": "برداشت",
          "currency": "ltc"
        }
    ],
    "hasNext": false
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `currency` | string | optional | Currency/crypto | `rls`, `usdt`, `btc`, ... |
| `tp` | string | optional | Transaction type | `deposit`, `withdraw`, etc. |
| `from` | datetime | optional | Start of range | `2018-10-01T00:00:00.000000+00:00` |
| `to` | datetime | optional | End of range | `2018-10-20T00:00:00.000000+00:00` |
| `from_id` | int | optional | Return IDs greater than this | `96124` |

**Notes**
1. `from` must be earlier than `to`.
2. The `from`–`to` window cannot exceed 90 days.
3. Possible `tp` values: `deposit`, `withdraw`, `buy` (trade proceeds received), `sell` (trade payment sent), `manual` (system-generated), `referral`, `transfer`, `pnl` (margin PnL), `delegate` (staking pool), `staking`, `yield_farming`, `discount`.

### Deposits — List

**Endpoint:** `GET /users/wallets/deposits/list`
**Rate limit:** 60 requests / 2 minutes
**Pagination:** Yes (default 10 for IRR, 20 for others)
**Date filter:** Yes

```shell
curl 'https://apiv2.nobitex.ir/users/wallets/deposits/list?wallet=4159' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
    "status": "ok",
    "deposits": [
        {
            "txHash": "c5d84268a0bf02307b5a0460a68b61987a9b3009d3a82a817e41558e619ec1d2",
            "address": "32KfyTNh162UoKithfDrWHZPYq5uePGmf7",
            "confirmed": true,
            "transaction": {
                "id": 10,
                "amount": "3.0000000000",
                "currency": "btc",
                "description": "Deposit - address:36n452uGq1x4mK7bfyZR8wgE47AnBb2pzi, tx:c5d84268a0bf02307b5a0460a68b61987a9b3009d3a82a817e41558e619ec1d2",
                "created_at": "2018-11-06T03:56:18+00:00",
                "calculatedFee": "0"
            },
            "currency": "Bitcoin",
            "blockchainUrl": "https://btc.com/c5d84268a0bf02307b5a0460a68b61987a9b3009d3a82a817e41558e619ec1d2",
            "confirmations": 2,
            "requiredConfirmations": 3,
            "amount": "3.0000000000"
        }
    ],
    "hasNext": true
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `wallet` | string | `all` | Wallet ID | `4159` |

**Notes**
- This endpoint only surfaces deposits from roughly the last 3 months.
- Pagination and date filtering only apply when `wallet` is specified; they don't work with the default `all`.

### Favorite Markets

Lets users mark which markets they want to favorite.

**Get favorites** — `GET /users/markets/favorite` · 6 req/min · token required

```shell
curl 'https://apiv2.nobitex.ir/users/markets/favorite' -X GET
```
```json
{ "status": "ok", "favoriteMarkets": ["XLMIRT", "BTCUSDT", "BTCIRT"] }
```

**Add favorites** — `POST /users/markets/favorite` · 12 req/min · token required

```shell
curl 'https://apiv2.nobitex.ir/users/markets/favorite' -X POST
```
```json
{ "status": "ok", "favoriteMarkets": ["BTCIRT", "DOGEUSDT"] }
```

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `market` | string | required | Comma-separated market symbol(s) | `BTCIRT` or `BTCIRT,DOGEUSDT` |

**Remove favorites** — `DELETE /users/markets/favorite` · 12 req/min · token required

```shell
curl 'https://apiv2.nobitex.ir/users/markets/favorite' -X DELETE
```
```json
{ "status": "ok", "favoriteMarkets": ["BTCIRT", "DOGEUSDT"] }
```

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `market` | string | required | Market symbol, or `All` to clear all favorites | `All` or `BTCIRT` |

---

