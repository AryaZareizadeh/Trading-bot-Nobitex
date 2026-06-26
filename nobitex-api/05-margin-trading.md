## 8. Margin Trading & Positions

Margin (leveraged) positions let users trade with borrowed funds via a delegation mechanism. Using this feature means accepting Nobitex's [margin trading terms](https://nobitex.ir/position-terms/).

> ⚠️ Margin/position trading is currently in **beta**.

### List Margin Markets

**Endpoint:** `GET /margin/markets/list`
**Rate limit:** 30 requests/minute

```shell
curl 'https://apiv2.nobitex.ir/margin/markets/list' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
  "status": "ok", 
  "markets": {
    "BTCIRT": {
      "srcCurrency": "btc",
      "dstCurrency": "rls",
      "positionFeeRate": "0.001",
      "maxLeverage": "5",
      "sellEnabled": true,
      "buyEnabled": false
    },
    "BTCUSDT": {
      "srcCurrency": "btc",
      "dstCurrency": "usdt",
      "positionFeeRate": "0.001",
      "maxLeverage": "5",
      "sellEnabled": true,
      "buyEnabled": true
    },
    "DOGEIRT": {
      "srcCurrency": "doge",
      "dstCurrency": "rls",
      "positionFeeRate": "0.001",
      "maxLeverage": "4",
      "sellEnabled": true,
      "buyEnabled": false
    },
    "DOGEUSDT": {
      "srcCurrency": "doge",
      "dstCurrency": "usdt",
      "positionFeeRate": "0.001",
      "maxLeverage": "3",
      "sellEnabled": true,
      "buyEnabled": true
    }
  }
}
```

**Response**

| Field | Type | Description | Example |
|---|---|---|---|
| `status` | string | Response status | `ok` |
| (market symbol key) | MarginMarketSetting | Settings for that margin market | `{"srcCurrency": "btc", ...}` |

**MarginMarketSetting object**

| Field | Type | Description | Example |
|---|---|---|---|
| `srcCurrency` | string | Market source currency | `btc`, `eth`, `trx`, ... |
| `dstCurrency` | string | Market destination currency | `rls` or `usdt` |
| `positionFeeRate` | decimal | Daily renewal fee rate | `0.001` |
| `maxLeverage` | decimal | Maximum allowed leverage | `5` |
| `sellEnabled` | boolean | Margin-sell available | `true` |
| `buyEnabled` | boolean | Margin-buy available | `true` |

**Notes**
1. Max leverage depends on both market depth and the user's account level — send your auth token to get the leverage applicable to your account.
2. The daily renewal fee rate is tiered based on the value of delegated assets. See the margin trading terms for details.

### List Active Liquidity Pools

**Endpoint:** `GET /liquidity-pools/list`
**Rate limit:** 12 requests/minute

```shell
curl 'https://apiv2.nobitex.ir/liquidity-pools/list' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
  "status": "ok", 
  "pools": {
    "btc": { "capacity": "30", "filledCapacity": "28.082343" },
    "ltc": { "capacity": "160", "filledCapacity": "149.234" },
    "doge": { "capacity": "5000000", "filledCapacity": "5000000" }
  }
}
```

Liquidity pools supply the funds delegated for margin buy/sell positions. The total capacity available for margin trading is bounded by pool balances. Pools can be enabled/disabled over time — while a pool is disabled, no new margin orders can be placed against it (though closing an existing position is still possible).

### Transfer Funds to/from the Margin Wallet

**Endpoint:** `POST /wallets/transfer`
**Rate limit:** 10 requests/minute

```shell
curl -X POST 'https://apiv2.nobitex.ir/wallets/transfer' \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"currency": "rls", "amount": "2500000000", "src": "spot", "dst": "margin"}'
```

Example response:
```json
{
  "status": "ok", 
  "srcWallet": {
    "id": 53456,
    "currency": "rls",
    "balance": "1870000000",
    "blockedBalance": "420000000",
    "activeBalance": "1450000000",
    "rialBalance": "1870000000",
    "rialBalanceSell": "1870000000",
    "depositAddress": null,
    "depositTag": null,
    "depositInfo": { "FIAT_MONEY": { "address": null, "tag": null } }
  },
  "dstWallet": {
    "id": 86459,
    "currency": "rls",
    "balance": "2500000000",
    "blockedBalance": "0",
    "activeBalance": "2500000000",
    "rialBalance": "2500000000",
    "rialBalanceSell": "2500000000"
  }
}
```

Collateral funds must be moved into the margin wallet before you can be delegated assets for margin trading. Use this endpoint to transfer balance between your spot and margin wallets.

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `currency` | string | required | Collateral currency | `rls` or `usdt` |
| `amount` | monetary | required | Amount to transfer | `2500000000` |
| `src` | string | required | Source wallet type | `spot` or `margin` |
| `dst` | string | required | Destination wallet type | `spot` or `margin` |

**Notes**
1. `src` and `dst` must differ.
2. The margin wallet is created on your first transfer into it; afterwards you can see its balance via [Wallets — List](#wallets--list).

**Error responses:**
```json
{ "status": "failed", "code": "InsufficientBalance", "message": "Amount cannot exceed active balance" }
```

| Error code | Description |
|---|---|
| `ParseError` | Input doesn't match the expected format |
| `InvalidAmount` | Invalid amount |
| `SameDestination` | Source and destination wallet types match |
| `WalletNotFound` | A margin wallet doesn't exist for currencies other than RLS/USDT |
| `InsufficientBalance` | Amount exceeds source wallet's free balance |

### Get Margin Delegation Limits

**Endpoint:** `GET /margin/v2/delegation-limit`
**Rate limit:** 12 requests/minute

```shell
curl 'https://apiv2.nobitex.ir/margin/v2/delegation-limit?market=BTCUSDT' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
  "status": "ok",
  "limits": {
    "buy": [
      { "leverage": "1", "limit": "1250" },
      { "leverage": "1.5", "limit": "1875" },
      { "leverage": "2", "limit": "2500" }
    ],
    "sell": [
      { "leverage": "1", "limit": "0.021" },
      { "leverage": "1.5", "limit": "0.0315" },
      { "leverage": "2", "limit": "0.042" }
    ]
  }
}
```

Returns your remaining delegation limit for a given margin market. This replaces the legacy `GET /margin/delegation-limit` endpoint — note it takes `market` instead of `currency`. The response splits remaining limits by `buy`/`sell` side, per leverage level. On the `sell` side, `limit` is denominated in the market's source currency; on the `buy` side, in the destination currency.

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `market` | string | required | Margin market symbol | `BTCUSDT` or `BTCIRT` |

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `status` | string | Response status | `ok` |
| `limits` | object | Remaining delegation limits by side | `{"buy": [...], "sell": [...]}` |
| `limits.buy` | array | Margin-buy limits (in destination currency) | `[{"leverage": "1", "limit": "1250"}]` |
| `limits.sell` | array | Margin-sell limits (in source currency) | `[{"leverage": "1", "limit": "0.021"}]` |

**DelegationLimitItem object**

| Field | Type | Description | Example |
|---|---|---|---|
| `leverage` | decimal | Leverage tier | `1.5` |
| `limit` | decimal | Remaining usable limit at that side/leverage | `1875` |

**Error responses:**

| Error code | Description |
|---|---|
| `TradeLimitation` | Insufficient account verification level |
| `InvalidSymbol` | Invalid `market` symbol |
| `InvalidMarket` | Market not found |
| `UnsupportedMarginSymbol` | This market doesn't support margin trading |

**Notes**
1. The sample response above is truncated; each side's array continues from leverage `1` up to that market's max leverage in `0.5` steps.
2. `limit` is the remaining usable amount at that side/leverage.
3. These values shrink as you open margin positions/orders and grow back as you close/settle them.
4. For margin-sell orders, order amount must be ≤ `sell.limit` at the chosen leverage.
5. For margin-buy orders, total order value (`price × amount`) must be ≤ `buy.limit` at the chosen leverage.

### Place a Margin Order

**Endpoint:** `POST /margin/orders/add`
**Rate limit:** 300 requests / 10 minutes

```shell
curl -X POST 'https://apiv2.nobitex.ir/margin/orders/add' \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"srcCurrency": "btc", "dstCurrency": "rls", "leverage": "2", "amount": "0.01", "price": "6400000000"}'
```

Example response:
```json
{
  "status": "ok",
  "order": {
    "id": 25,
    "type": "sell",
    "execution": "Limit",
    "tradeType": "Margin",
    "srcCurrency": "btc",
    "dstCurrency": "rls",
    "price": "6400000000",
    "amount": "0.01",
    "status": "Active",
    "totalPrice": "0",
    "totalOrderPrice": "64000000",
    "matchedAmount": 0,
    "unmatchedAmount": "0.01",
    "leverage": "2",
    "side": "open",
    "partial": false,
    "fee": 0,
    "created_at": "2022-10-20T11:36:13.592827+00:00",
    "averagePrice": "0"
  }
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `execution` | string | `limit` | Execution mode | `limit`, etc. |
| `srcCurrency` | string | required | Market source currency | `btc`, ... |
| `dstCurrency` | string | required | Market destination currency | `rls` or `usdt` |
| `type` | string | `sell` | Buy or sell | `sell` or `buy` |
| `leverage` | monetary | `1` | Delegation leverage | `2` |
| `amount` | monetary | required | Requested delegated amount | `0.01` |
| `price` | monetary | required | Order price | `13400000000` |
| `stopPrice` | monetary | optional | Stop trigger price | `12600000000` |

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `order` | Order object | The margin order | `{"id": 25, ...}` |

#### OCO order

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `mode` | string | required | Order mode | `oco` |
| `srcCurrency` | string | required | Source currency | `btc`, ... |
| `dstCurrency` | string | required | Destination currency | `rls` or `usdt` |
| `type` | string | `sell` | Buy or sell | `sell` or `buy` |
| `leverage` | monetary | `1` | Leverage | `2` |
| `amount` | monetary | required | Requested amount | `0.01` |
| `price` | monetary | required | Order price | `13400000000` |
| `stopPrice` | monetary | required | Stop trigger price | `12600000000` |
| `stopLimitPrice` | monetary | required | Stop-limit fill price | `12610000000` |

**OCO response:** `orders` (list of Order objects), e.g. `[{"id": 25, ...}, {"id": 26, ...}]`

**Notes**
1. `execution` can be `limit`, `market`, `stop_limit`, or `stop_market`.
2. Margin orders show up in [List Your Orders](#list-your-orders).
3. Once a margin order fills, the corresponding position opens.
4. OCO price ordering: **buy** — `price` < market price < `stopPrice`/`stopLimitPrice`; **sell** — `price` > market price > `stopPrice`/`stopLimitPrice`.
5. Leverage must be between 1 and the market's max leverage, in steps of 0.5 (e.g. `1`, `1.5`, `2`, `2.5`, ...).

**Error responses:**

| Error code | Description |
|---|---|
| `FeatureUnavailable` | Not authorized for this beta feature |
| `ParseError` | Input format is invalid |
| `TradeLimitation` | Insufficient verification level |
| `InvalidMarketPair` | That src/dst pair isn't a Nobitex market |
| `MarketClosed` | Market closed / not yet open |
| `TradingUnavailable` | Trading restricted by support |
| `UnsupportedMarginSrc` | This currency doesn't support margin trading |
| `MarginClosed` | Margin trading closed / not open |
| `AmountUnavailable` | Requested sell-delegation amount unavailable in the pool |
| `ExceedDlegationLimit` | Exceeds your remaining delegation limit |
| `InsufficientBalance` | Margin wallet balance insufficient to cover collateral |
| `LeverageTooHigh` | Leverage exceeds max allowed (pool/account level) |
| `LeverageUnavailable` | Leverage restricted by support |
| `BadPrice` | Unreasonable order price (same as spot) |
| `SmallOrder` | Below minimum order size (same as spot) |
| `PriceConditionFailed` | Price condition not met (same as spot) |
| `DuplicateOrder` | Identical order submitted within last 10s (same as spot) |

### List Positions

**Endpoint:** `GET /positions/list`
**Rate limit:** 30 requests/minute
**Pagination:** Yes (default 50)

```shell
curl 'https://apiv2.nobitex.ir/positions/list?srcCurrency=btc&status=active' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
  "status": "ok",
  "positions": [
    {
      "id": 128,
      "createdAt": "2022-10-20T11:36:13.604420+00:00",
      "srcCurrency": "btc",
      "dstCurrency": "rls",
      "side": "sell",
      "status": "Open",
      "marginType": "Isolated Margin",
      "collateral": "320000000",
      "leverage": "2",
      "openedAt": "2022-10-20T11:36:16.562038+00:00",
      "closedAt": null,
      "liquidationPrice": "25174302690",
      "entryPrice": "6400000000",
      "exitPrice": null,
      "delegatedAmount": "0.03",
      "liability": "0.0300450676",
      "totalAsset": "831712000",
      "marginRatio": "1.49",
      "liabilityInOrder": "0",
      "assetInOrder": "0",
      "unrealizedPNL": "−576435",
      "unrealizedPNLPercent": "−0.09",
      "expirationDate": "2022-11-20",
      "extensionFee": "320000",
      "markPrice": "6430000000"
    },
    {
      "id": 32,
      "createdAt": "2022-08-14T15:09:58.001901+00:00",
      "srcCurrency": "btc",
      "dstCurrency": "usdt",
      "side": "sell",
      "status": "Closed",
      "marginType": "Isolated Margin",
      "collateral": "2130",
      "leverage": "1",
      "openedAt": "2022-08-14T15:10:19.937801+00:00",
      "closedAt": "2022-08-17T18:39:52.890674+00:00",
      "liquidationPrice": "38986.54",
      "entryPrice": "21300",
      "exitPrice": "19900",
      "PNL": "118.46",
      "PNLPercent": "5.56"
    }
  ],
  "hasNext": false
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `srcCurrency` | string | all | Source currency | `btc`, ... |
| `dstCurrency` | string | all | Destination currency | `rls` or `usdt` |
| `status` | string | `active` | Position status filter | `active` or `past` |

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `positions` | list of Position | The positions | `[{... Position ...}]` |
| `hasNext` | boolean | More pages? | `false` |

**Position object**

| Field | Type | Description | Example |
|---|---|---|---|
| `id` | int | Position ID | `128` |
| `createdAt` | string | When delegation/order was requested | `2022-10-20T11:36:13.604420+00:00` |
| `srcCurrency` | string | Source currency | `btc`, `eth`, `trx`, ... |
| `dstCurrency` | string | Destination currency | `rls` or `usdt` |
| `side` | string | Buy or sell | `sell` or `buy` |
| `status` | string | Status | `Open`, `Closed`, `Liquidated`, `Expired` |
| `marginType` | string | Collateral type | `Isolated Margin` |
| `collateral` | monetary | Collateral locked in margin wallet | `640000000` |
| `leverage` | decimal | Leverage | `2` |
| `openedAt` | string | When the position opened | `2022-10-20T11:36:16.562038+00:00` |
| `closedAt` | string | When the position was finally settled | `2022-10-25T09:57:38.560820+00:00` |
| `liquidationPrice` | monetary | Liquidation price | `25174302690` |
| `entryPrice` | monetary | Average opening fill price | `6400000000` |
| `exitPrice` | monetary | Average settlement fill price | `6200000000` |
| *(open positions only):* | | | |
| `delegatedAmount` | monetary | Currently delegated amount (source currency) | `0.03` |
| `liability` | monetary | Outstanding liability | `0.0300450676` |
| `totalAsset` | monetary | Total asset value (destination currency) | `831712000` |
| `marginRatio` | decimal | Margin ratio | `1.49` |
| `liabilityInOrder` | monetary | Liability tied up in an open settlement order | `0` |
| `assetInOrder` | monetary | Asset tied up in an open settlement order | `0` |
| `unrealizedPNL` | monetary | Unrealized PnL (destination currency) | `-576435` |
| `unrealizedPNLPercent` | decimal | Unrealized PnL % | `−0.09` |
| `expirationDate` | string | Final expiration date | `2022-11-20` |
| `extensionFee` | monetary | Daily renewal fee (destination currency) | `320000` |
| `markPrice` | monetary | Current mark price | `6430000000` |
| *(closed positions only):* | | | |
| `PNL` | monetary | Final realized PnL (destination currency) | `35584000` |
| `PNLPercent` | decimal | Final PnL % | `5.56` |

**Notes**
1. **`status` (request):** `active` = open/unsettled positions; `past` = closed, liquidated, or expired (settled) positions.
   **`status` (response)** can be: `Open` (some or all of the margin order has filled), `Closed` (delegated amount fully repaid), `Liquidated`, `Expired` (positions can stay open at most 30 days from initial order placement).
2. **Collateral type:** `Isolated Margin` (default — max loss is the position's collateral) or `Cross Margin` (max loss is the entire free margin-wallet balance — planned for the future).
3. **Buy liability** = delegated amount + buy-order fee (for short positions), or − buy-order fee (for long positions).
4. **Total asset** = collateral + remaining sale proceeds (short), or current value of purchased asset (long).
5. **Margin ratio:** starts at 2; liquidation occurs once it drops to 1.1.
6. There may be a short delay between liquidation/expiry and final settlement (system-driven order placement + execution); during that window the position still shows as active/open.

**Error responses:**

| Error code | Description |
|---|---|
| `ParseError` | Input format invalid |

### Get a Single Position

**Endpoint:** `GET /positions/:positionId:/status`
**Rate limit:** 100 requests / 10 minutes ([shared limit](#order-rate-limit-note))

```shell
curl 'https://apiv2.nobitex.ir/positions/128/status' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response: same `Position` object shape as above.

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `:positionId:` | url param | required | Position ID | `128` |

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `position` | Position object | The position | `{"id": 128, ...}` |

### Close a Position

**Endpoint:** `POST /positions/:positionId:/close`
**Rate limit:** 300 requests / 10 minutes

```shell
curl -X POST 'https://apiv2.nobitex.ir/positions/128/close' \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"amount": "0.0100150225", "price": "6200000000"}'
```

Example response:
```json
{
  "status": "ok",
  "order": {
    "id": 28,
    "type": "buy",
    "execution": "Limit",
    "tradeType": "Margin",
    "srcCurrency": "btc",
    "dstCurrency": "rls",
    "price": "6200000000",
    "amount": "0.0100150225",
    "status": "Active",
    "totalPrice": "0",
    "totalOrderPrice": "62093139",
    "matchedAmount": 0,
    "unmatchedAmount": "0.0100150225",
    "leverage": "2",
    "side": "close",
    "partial": false,
    "fee": 0,
    "created_at": "2022-10-25T09:57:38.560820+00:00",
    "averagePrice": "0"
  }
}
```

Closes a position by placing an order in the opposite direction to settle the liability — i.e. buying back to close a short, or selling to close a long.

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `:positionId:` | url param | required | Position ID | `128` |
| `execution` | string | `limit` | Execution mode | `limit`, etc. |
| `amount` | monetary | required | Order amount | `0.0100150225` |
| `price` | monetary | required | Order price | `12600000000` |
| `stopPrice` | monetary | optional | Stop trigger price | `12600000000` |

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `order` | Order object | The settlement order | `{"id": 25, ...}` |

#### OCO order

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `:positionId:` | url param | required | Position ID | `128` |
| `mode` | string | required | Order mode | `oco` |
| `amount` | monetary | required | Order amount | `0.0100150225` |
| `price` | monetary | required | Order price | `13400000000` |
| `stopPrice` | monetary | required | Stop trigger price | `13600000000` |
| `stopLimitPrice` | monetary | required | Stop-limit fill price | `13610000000` |

**OCO response:** `orders` — list of Order objects, e.g. `[{"id": 25, ...}, {"id": 26, ...}]`

**Notes**
1. `execution` can be `limit`, `market`, `stop_limit`, or `stop_market`.
2. Settlement orders also appear in [List Your Orders](#list-your-orders).
3. Once a settlement order fully fills, the position closes and PnL is credited.
4. Settlement orders are funded against the position's total assets. The settlement amount must satisfy:
   - The total open settlement-order amount must not exceed the position's liability (`liability - liabilityInOrder`).
   - Unless settling the entire remaining liability (final settlement), the order can't be smaller than the platform's minimum order size. (E.g., to settle a 0.001 BTC short worth 640,000 Toman with a 300,000 Toman minimum: you could place two 300,000-Toman buy orders, then one final small order for the remaining 40,000 Toman — but you can't place initial orders below 300,000 Toman, similar to converting small wallet balances.)
   - For closing a short (buy order), the total open buy-order value (amount × price) must not exceed the position's total asset value (`totalAsset - assetInOrder`).
5. Position liability has at most 10 decimal places of precision, and the full value must be sent for final settlement.
6. OCO price ordering: **closing a short** (with a buy order) — `price` < market price < `stopPrice`/`stopLimitPrice`; **closing a long** (with a sell order) — `price` > market price > `stopPrice`/`stopLimitPrice`.

**Error responses:**

| Error code | Description |
|---|---|
| `ParseError` | Input format invalid |
| `TradeLimitation` | Insufficient verification level |
| `TradingUnavailable` | Trading restricted by support |
| `NoOpenPosition` | No active position with that ID |
| `ExceedLiability` | Order amount exceeds remaining liability |
| `ExceedTotalAsset` | Order value exceeds the position's free total assets |
| `BadPrice` | Unreasonable order price (same as spot) |
| `SmallOrder` | Below minimum order size (same as spot) |
| `PriceConditionFailed` | Price condition not met (same as spot) |

### Edit Collateral on an Open Position

**Endpoint:** `POST /positions/:positionId:/edit-collateral`
**Rate limit:** 60 requests/minute

```shell
curl -X POST 'https://apiv2.nobitex.ir/positions/128/edit-collateral' \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"collateral": "230000000"}'
```

Example response:
```json
{
  "status": "ok",
  "position": {
    "id": 128,
    "createdAt": "2022-10-20T11:36:13.604420+00:00",
    "srcCurrency": "btc",
    "dstCurrency": "rls",
    "side": "sell",
    "status": "Open",
    "marginType": "Isolated Margin",
    "collateral": "230000000",
    "leverage": "2",
    "openedAt": "2022-10-20T11:36:16.562038+00:00",
    "closedAt": null,
    "liquidationPrice": "12230682570",
    "entryPrice": "6400000000",
    "exitPrice": "6100000000",
    "PNL": null,
    "delegatedAmount": "0.02",
    "liability": "0.0200300451",
    "totalAsset": "257023981",
    "marginRatio": "2.03",
    "liabilityInOrder": "0",
    "assetInOrder": "0",
    "unrealizedPNL": "23423565",
    "unrealizedPNLPercent": "10.18",
    "expirationDate": "2022-11-20",
    "extensionFee": "320000",
    "markPrice": "6090000000"
  }
}
```

Use this to increase or decrease the collateral backing an open position.

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `:positionId:` | url param | required | Position ID | `128` |
| `collateral` | monetary | required | New collateral amount | `230000000` |

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `position` | Position object | The updated position | `{"id": 25, ...}` |

**Notes**
1. If the position is profitable (margin ratio > initial margin ratio = `1 + 1/leverage`), you can withdraw the surplus collateral down to the initial margin ratio — note realized profit is only released when the position is fully closed.
2. If a position is approaching liquidation and you want to keep it open (betting on a reversal), increasing collateral improves your liquidation price.

**Error responses:**

| Error code | Description |
|---|---|
| `ParseError` | Input format invalid |
| `TryAgainLater` | Collateral changes temporarily unavailable |
| `LowMarginRatio` | Reducing collateral would breach the minimum margin ratio (2) |
| `InsufficientBalance` | Increase exceeds margin wallet's free balance |

---

