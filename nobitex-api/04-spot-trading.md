## 7. Trading — Spot Market Orders

### Place a New Order

**Endpoint:** `POST /market/orders/add`
**Rate limit:** 300 requests / 10 minutes ([shared limit](#order-rate-limit-note))

```shell
curl 'https://apiv2.nobitex.ir/market/orders/add' \
  -X POST \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"type":"buy","srcCurrency":"btc","dstCurrency":"rls","amount":"0.6","price":520000000,"clientOrderId":"order1"}'
```

Example response:
```json
{
  "status": "ok",
  "order": {
    "type": "sell",
    "srcCurrency": "Bitcoin",
    "dstCurrency": "ریال",
    "price": "520000000",
    "amount": "0.6",
    "totalPrice": "312000000.0",
    "matchedAmount": 0,
    "unmatchedAmount": "0.6",
    "id": 25,
    "status": "Active",
    "partial": false,
    "fee": 0,
    "created_at": "2018-11-28T11:36:13.592827+00:00",
    "clientOrderId": "order1"
  }
}
```

Placing an order does not guarantee an immediate trade — whether it fills depends on the order type/price and current market conditions. Use [Order Status](#order-status) to track it.

After submission, orders are re-validated before entering the order book; if invalid, they're set to "Rejected". If your orders are accepted but immediately rejected, double-check your parameters — especially price, amount, and account balance.

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `type` | string | required | Order side | `buy` or `sell` |
| `execution` | string | `limit` | Execution mode | `market` (market price), `limit` (set price) |
| `srcCurrency` | string | required | Source currency | `btc`, `eth`, `xrp`, ... |
| `dstCurrency` | string | required | Destination currency | `rls` or `usdt` |
| `amount` | monetary | required | Order volume | `0.0623` |
| `price` | monetary | required | Unit price | `1210000000` |
| `clientOrderId` | string | `null` | Client-supplied order ID, up to 32 chars, unique per user among open/active/inactive orders (beta) | `order1` |

#### Stop Order

Example:
```shell
curl 'https://apiv2.nobitex.ir/market/orders/add' \
  -X POST \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"type":"sell","srcCurrency":"doge","dstCurrency":"rls","amount":"64","execution":"stop_market","stopPrice":47500,"clientOrderId":"order1"}'
```

```json
{
  "status": "ok",
  "order": {
    "id": 26,
    "type": "sell",
    "execution": "StopMarket",
    "market": "DOGE-RLS",
    "srcCurrency": "Dogecoin",
    "dstCurrency": "﷼",
    "price": "market",
    "amount": "64",
    "param1": "47500",
    "totalPrice": "0",
    "totalOrderPrice": "3008000",
    "matchedAmount": "0",
    "unmatchedAmount": "64",
    "status": "Inactive",
    "partial": false,
    "fee": 0,
    "created_at": "2022-01-17T12:14:18.005896+00:00",
    "averagePrice": "0",
    "clientOrderId": "order1"
  }
}
```

A stop order activates once the market price reaches the stop price. Its primary purpose is loss prevention against sudden adverse price moves, letting you react to a price breakout without continuously watching the market.

**Stop-order specific parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `execution` | string | required | Execution mode | `stop_market` (stop-loss market), `stop_limit` (stop-loss limit) |
| `stopPrice` | monetary | required | Stop (trigger) price | `1180000000` |

#### OCO Order

Example:
```shell
curl 'https://apiv2.nobitex.ir/market/orders/add' \
  -X POST \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"type":"buy","srcCurrency":"btc","dstCurrency":"usdt","amount":"0.01","mode":"oco","price":42390,"stopPrice":42700,"stopLimitPrice":42715,"clientOrderId":"order1"}'
```

Example response:
```json
{
  "status": "ok",
  "orders": [
    {
      "id": 27,
      "type": "buy",
      "execution": "Limit",
      "market": "BTC-USDT",
      "srcCurrency": "Bitcoin",
      "dstCurrency": "Tether",
      "price": "42390",
      "amount": "0.01",
      "totalPrice": "0",
      "totalOrderPrice": "423.9",
      "matchedAmount": "0",
      "unmatchedAmount": "0.01",
      "status": "Active",
      "created_at": "2022-04-10T10:12:38.402795+00:00",
      "pairId": 28,
      "clientOrderId": "order1"
    },
    {
      "id": 28,
      "type": "buy",
      "execution": "StopLimit",
      "market": "BTC-USDT",
      "srcCurrency": "Bitcoin",
      "dstCurrency": "Tether",
      "price": "42715",
      "amount": "0.01",
      "param1": "42700",
      "totalPrice": "0",
      "totalOrderPrice": "427.15",
      "matchedAmount": "0",
      "unmatchedAmount": "0.01",
      "status": "Inactive",
      "created_at": "2022-04-10T10:12:38.402795+00:00",
      "pairId": 27,
      "clientOrderId": null
    }
  ]
}
```

An OCO ("one cancels the other") order is actually two orders at once: one **limit** order and one **stop-limit** order. Whichever one fills first automatically cancels the other. E.g., if price reaches your limit price, the limit order executes and the stop order is auto-cancelled — and vice versa.

**OCO-specific parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `mode` | string | required | Order mode | `oco` |
| `stopPrice` | monetary | required | Stop trigger price | `1180000000` |
| `stopLimitPrice` | monetary | required | Stop-limit fill price | `1179500000` |

⚠️ Price ordering constraints for OCO orders:
- **Buy:** `price` < current market price < `stopPrice`/`stopLimitPrice`
- **Sell:** `price` > current market price > `stopPrice`/`stopLimitPrice`

#### Error responses (order placement)

```json
{
  "status": "failed",
  "code": "ErrorCode",
  "message": "Human readable error message",
  "clientOrderId": "order1"
}
```

If the order request is invalid, you may receive one of these. In all cases the order was **not** placed — resubmit if you want it placed.

| Error code | Description |
|---|---|
| `InvalidOrderPrice` | `price` is missing or invalid |
| `BadPrice` | Regular order: price is too far from current market price — keep it within ~30% of the current price. Stop order: the order price can't be better than the stop price |
| `PriceConditionFailed` | The price condition (relative to market price) wasn't met |
| `OverValueOrder` | Sell `amount` (or buy `amount * price`) exceeds your wallet balance |
| `SmallOrder` | Order below minimum trade value — minimum is 3,000,000 IRR for IRR markets and 11 USDT for USDT markets; `amount * price` must exceed this |
| `DuplicateOrder` | An identical order from you was already submitted within the last 10 seconds |
| `InvalidMarketPair` | `srcCurrency`/`dstCurrency` invalid, or that market pair doesn't exist on Nobitex |
| `MarketClosed` | The market is temporarily closed |
| `TradingUnavailable` | You're not allowed to trade — complete identity verification |
| `FeatureUnavailable` | You're not authorized to use this beta feature |
| `DuplicateClientOrderId` | Duplicate `clientOrderId` (each user can have only one open/active/inactive order with a given ID at a time) |

**Notes**
1. **Units:** prices in IRR markets are in Rial (not Toman). Prices in USDT markets are in USDT. `amount` is denominated in `srcCurrency`.
2. **Market orders:** set `execution=market` for a fast fill at the best available price. ([Binance explainer](https://www.binance.vision/tutorials/what-is-a-market-order) / [Investopedia](https://www.investopedia.com/terms/m/marketorder.asp))
3. **Bounding a market order's price:** it's strongly recommended to also set `price` on market orders. This caps how far the fill price can drift from your estimate — e.g. a market buy with `price=100,000,000 Toman` will only fill while price stays under ~101,000,000 Toman. This protects you from sudden volatility spikes. If you omit `price` entirely, the order fills at whatever the live market price is, within a ~1% band.
4. **Monetary precision:** `amount`/`price` precision varies by market (0–8 decimal places). Extra decimals get rounded — down for amount, banker's rounding for price. See the [market precision table](https://nobitex.ir/policies/markets/) for details.
5. **`clientOrderId`** must be unique per user among that user's open/active/inactive orders.
6. **Duplicate-order protection:** if you submit two or more orders with identical type/price/amount within 10 seconds, only the first is accepted; the rest get a `DuplicateOrder` error for 10 seconds (disabled in "Pro" mode). If you use Pro mode, make sure to use `clientOrderId` to protect yourself from network-glitch duplicate submissions.

### Order Status

**Endpoint:** `POST /market/orders/status`
**Rate limit:** 300 requests/minute

```shell
curl 'https://apiv2.nobitex.ir/market/orders/status' \
  -X POST \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"id":5684}'
```

Example response:
```json
{
    "status": "ok",
    "order": {
        "unmatchedAmount": "3.0000000000",
        "fee": "0E-10",
        "matchedAmount": "0E-10",
        "partial": false,
        "price": "8500000.0000000000",
        "created_at": "2018-11-28T12:25:22.696029+00:00",
        "id": 5684,
        "srcCurrency": "Litecoin",
        "totalPrice": "25500000.00000000000000000000",
        "type": "sell",
        "dstCurrency": "﷼",
        "isMyOrder": false,
        "status": "Active",
        "amount": "3.0000000000",
        "clientOrderId": "order1"
    }
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `id` | int | optional | Order ID | `5684` |
| `clientOrderId` | string | optional | Client order ID (beta) | `order1` |

**Notes**
1. At least one of `id` / `clientOrderId` must be sent.
2. If both are sent, `id` takes priority.
3. `clientOrderId` is only searched among open/active/inactive orders.
4. `clientOrderId` is in beta and may change in the future.

**`status` values:**
- `Active` — order still has unmatched amount and is live on the book.
- `Done` — fully filled.
- `Inactive` — a stop order whose trigger price hasn't been hit yet.
- `Canceled` — cancelled before fully filling, either by the user or the system. The system may cancel an order if there wasn't enough liquidity within 1% of the target price to fill it, or if the user's wallet balance dropped below what's needed to fill the order (due to other transactions).

**Error responses:**
```json
{ "status": "failed", "code": "ErrorCode", "message": "Human readable error message" }
```

| Error code | Description |
|---|---|
| `NullIdAndClientOrderId` | One of `id` or `clientOrderId` must be sent |

### List Your Orders

**Endpoint:** `GET /market/orders/list`
**Rate limit:** 30 requests/minute
**Pagination:** Yes (default 100)

```shell
curl 'https://apiv2.nobitex.ir/market/orders/list?srcCurrency=btc&dstCurrency=usdt&details=2' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
  "status": "ok",
  "orders": [
    {
      "id": 173546223,
      "type": "sell",
      "execution": "Limit",
      "status": "Active",
      "srcCurrency": "Bitcoin",
      "dstCurrency": "Tether",
      "price": "9750.01",
      "amount": "0.0123",
      "matchedAmount": "0E-10",
      "averagePrice": "0",
      "fee": "0E-10",
      "clientOrderId": "order1"
    }
  ]
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `status` | string | `open` | Order status filter | `all`, `open`, `done`, `close` |
| `type` | string | all types | Order side filter | `sell` or `buy` |
| `execution` | string | all | Filter by execution mode | `limit`, `market`, `stop_limit`, `stop_market` |
| `tradeType` | string | all | Spot or margin orders | `spot` or `margin` |
| `srcCurrency` | string | all | Source currency | `btc`, `eth`, `xrp`, ... |
| `dstCurrency` | string | all | Destination currency | `rls` or `usdt` |
| `details` | int | `1` | Response detail level — higher returns more fields per order | `1` or `2` |
| `fromId` | int | `1` | Only return orders with ID greater than this | `100` |
| `order` | string | depends on `fromId`/`type` | Sort key | `id`, `created_at`, or `price` — ascending, or descending by prefixing with `-` |

**More on `status`:**
- `all` — every order regardless of status.
- `open` — active + inactive orders (e.g. stop orders not yet triggered).
- `done` — orders that are partially or fully filled.
- `close` — orders that ended up `Done` or `Canceled`.
- (the legacy `active`/`undone` values are no longer supported)

**More on `order` (sort):**
- `id` / `-id` — by order ID, ascending/descending.
- `created_at` / `-created_at` — by creation time.
- `price` / `-price` — by price.
- If omitted: if `fromId` was sent, results sort by ID descending; otherwise by creation time descending; if `type=buy`, by price descending; if `type=sell`, by price ascending.

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `orders` | list of Order | Your orders | `[{...Order...}, ...]` |

**Order object**

| Field | Type | Description | Example |
|---|---|---|---|
| `type` | string | Buy or sell | `buy` or `sell` |
| `execution` | string | Execution type | `Limit`, `Market`, `StopMarket`, `StopLimit` |
| `tradeType` | string | Spot or margin | `Spot` or `Margin` |
| `srcCurrency` | string | Source currency | `Bitcoin`, `Ethereum`, `TRON`, ... |
| `dstCurrency` | string | Destination currency | `﷼` or `Tether` |
| `price` | monetary | Order price | `2900000000` or `market` |
| `amount` | monetary | Order amount | `0.023324` |
| `matchedAmount` | monetary | Filled amount | `0.012001` |
| `param1` | monetary | Stop price (for stop orders) | `2790000000` |

When `details=2`, these additional fields are included per order:

| Field | Type | Description | Example |
|---|---|---|---|
| `id` | int | Order ID | `180258791` |
| `status` | string | Current order status | `New`, `Active`, `Inactive`, `Done`, `Canceled` |
| `fee` | monetary | Fee accrued so far | `0.00001` |
| `created_at` | datetime | Order creation time | `2020-07-15T11:32:38.326809+00:00` |
| `averagePrice` | monetary | Average fill price | `2899500000` |

**Notes**
1. Default response returns 100 orders, configurable up to 1000 via input params. Use `page` or `fromId` to retrieve more or older orders — note **you can't use both simultaneously**; if `fromId` is set, one page (sized via `pageSize`) is returned. If you keep many open orders, it's a good idea to save their IDs when placed and poll [Order Status](#order-status) per ID individually as needed. To see your filled trades specifically, use [List Your Trades](#list-your-trades) below.
2. `Done` means 100% filled. An order can fill incrementally while remaining `Active` — use `matchedAmount` to track fill progress. An order can also be cancelled before fully filling (user request, insufficient balance, or — for market orders — a sharp price move), in which case its status is `Canceled` even though `matchedAmount` may be greater than zero.
3. This API is analogous to Binance's [Current Open Orders](https://binance-docs.github.io/apidocs/spot/en/#current-open-orders-user_data) and [All Orders](https://binance-docs.github.io/apidocs/spot/en/#all-orders-user_data) endpoints.

### Update Order Status

**Endpoint:** `POST /market/orders/update-status`
**Rate limit:** 90 requests/minute

```shell
curl 'https://apiv2.nobitex.ir/market/orders/update-status' \
  -X POST \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"order":5684,"status":"canceled"}'
```

Example response:
```json
{
  "status": "ok",
  "updatedStatus": "Canceled",
  "order": {
    "amount": "60",
    "averagePrice": "0",
    "clientOrderId": null,
    "created_at": "2025-10-28T09:25:17.774332+00:00",
    "dstCurrency": "﷼",
    "execution": "Market",
    "fee": "0",
    "id": 5684,
    "market": "USDT-RLS",
    "matchedAmount": "0",
    "partial": false,
    "price": "market",
    "srcCurrency": "Tether",
    "status": "Canceled",
    "totalOrderPrice": "2550000",
    "totalPrice": "0",
    "tradeType": "Spot",
    "type": "sell",
    "unmatchedAmount": "60"
  }
}
```

Use this to change an order's status (cancel or activate).

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `order` | int | optional | Order ID | `5684` |
| `clientOrderId` | string | optional | Client order ID (beta) | `order1` |
| `status` | string | required | New status | `canceled` |

**Notes**
1. At least one of `order` / `clientOrderId` must be sent.
2. If both are sent, `id` takes priority.
3. `clientOrderId` is only searched among open/active/inactive orders.
4. `clientOrderId` is in beta and may change.
5. `status` can transition `new` → `active`, or `active`/`inactive` → `canceled`. Other transitions are rejected.
6. If the order is part of an unfinished OCO pair, both linked orders get cancelled together.

**Error responses:**
```json
{ "status": "failed", "code": "ErrorCode", "message": "Human readable error message" }
```

| Error code | Description |
|---|---|
| `NullIdAndClientOrderId` | One of `id`/`clientOrderId` must be sent |

### Bulk-Cancel Orders

**Endpoint:** `POST /market/orders/cancel-old`
**Rate limit:** 30 requests/minute

```shell
curl 'https://apiv2.nobitex.ir/market/orders/cancel-old' \
  -X POST \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"execution":"limit","srcCurrency":"btc","dstCurrency":"rls","hours":2.4}'
```

Response:
```json
{ "status": "ok" }
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `hours` | float | optional | Cancel orders older than this many hours | `4.2` |
| `execution` | string | optional | Filter by execution mode | `market`, `limit`, `stop_market`, `stop_limit` |
| `tradeType` | string | optional | Spot or margin | `spot` or `margin` |
| `srcCurrency` | string | optional | Source currency | `btc` |
| `dstCurrency` | string | optional | Destination currency | `rls` |

**Notes**
1. `hours` means "older than X hours ago" relative to the request time — e.g. `hours=2` cancels orders placed more than 2 hours ago.
2. If `hours` is omitted, all matching active orders are cancelled.
3. Inactive non-OCO stop orders are not affected by this bulk cancel.
4. You may receive an error in some conditions, returned in the `error` field.

**Error responses:**
```json
{ "status": "failed", "code": "ErrorCode", "message": "Human readable error message" }
```

### List Your Trades

**Endpoint:** `GET /market/trades/list`
**Rate limit:** 30 requests/minute
**Pagination:** Yes (default 30)

```shell
curl 'https://apiv2.nobitex.ir/market/trades/list?srcCurrency=usdt&dstCurrency=rls' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
  "status": "ok",
  "trades": [
    {
      "id": 123412,
      "orderId": 1231222,
      "srcCurrency": "Tether",
      "dstCurrency": "﷼",
      "market": "USDT-RLS",
      "timestamp": "2022-07-05T09:57:38.560820+00:00",
      "type": "sell",
      "price": "316800",
      "amount": "57.3605",
      "total": "18171806.4",
      "fee": "27257.7096"
    }
  ],
  "hasNext": false
}
```

Returns your trades from roughly the last 3 days.

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `srcCurrency` | string | optional | Source currency | `btc`, ... |
| `dstCurrency` | string | optional | Destination currency | `rls` or `usdt` |
| `fromId` | int | optional | Minimum trade ID | `10023` |

- For the market filter, both `srcCurrency` and `dstCurrency` must be set together, or both left empty.
- If `fromId` is set, only trades with ID ≥ that value are returned.

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `trades` | list of Trade | Your trades | `[{...Trade...}, ...]` |
| `hasNext` | boolean | More pages available? | `false` |

**Trade object**

| Field | Type | Description | Example |
|---|---|---|---|
| `id` | int | Trade ID | `122545` |
| `orderId` | int | Order ID | `214534534434` |
| `srcCurrency` | string | Source currency | `Bitcoin`, `Ethereum`, `TRON`, ... |
| `dstCurrency` | string | Destination currency | `﷼` or `Tether` |
| `market` | string | Market symbol | `USDT-RLS` |
| `timestamp` | string | Trade time | `2022-07-05T09:57:38.560820+00:00` |
| `type` | string | Buy or sell | `buy` or `sell` |
| `price` | monetary | Fill price | `316800` |
| `amount` | monetary | Filled amount | `57.3605` |
| `total` | monetary | Total trade value | `18171806.4` |
| `fee` | monetary | Trade fee | `27257.7096` |

---

