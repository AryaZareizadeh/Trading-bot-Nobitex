## 12. WebSocket API

Nobitex uses WebSocket (via a **Centrifugo** server) to push real-time data. Centrifugo offers SDKs for many languages so you can connect easily from your client.

**Official SDKs**

| SDK module | Use case |
|---|---|
| [centrifuge-js](https://github.com/centrifugal/centrifuge-js) | Browser, Node.js, React Native |
| [centrifuge-python](https://github.com/centrifugal/centrifuge-python) | Python (asyncio-based) |
| [centrifuge-java](https://github.com/centrifugal/centrifuge-java) | Java / Android |
| [centrifuge-go](https://github.com/centrifugal/centrifuge-go) | Go |
| [centrifuge-swift](https://github.com/centrifugal/centrifuge-swift) | iOS |
| [centrifuge-dart](https://github.com/centrifugal/centrifuge-dart) | Dart / Flutter |

**Unofficial SDKs**
- [centrifuge-csharp](https://github.com/charmy/centrifuge-csharp) — C# / .NET / Unity 2022.3+

### Connecting

Install via npm:
```bash
npm install centrifuge
```

Connect via the SDK:
```javascript
import { Centrifuge } from 'centrifuge';

const client = new Centrifuge('wss://ws.nobitex.ir/connection/websocket', {});
client.on('connected', (ctx) => {
    console.log('connected', ctx);
});
client.connect();
```

If you're not using the SDK, send this raw message to connect:
```json
{ "connect": {}, "id": 1 }
```

Direct browser usage — add this script tag at the end of `<body>`:
```html
<script src="https://unpkg.com/centrifuge@5.2.2/dist/centrifuge.js"></script>
```

**Connection details**
- **WebSocket URL:** `wss://ws.nobitex.ir/connection/websocket`
- **Rate limit:** max 100 simultaneous connections per IP
- **Token required:** No (for connecting; needed for private channels — see below)

After connecting, the Centrifugo server periodically sends `ping` messages. Official SDKs automatically respond with `pong`. If a `ping` isn't answered within 25 seconds, the server disconnects the client to manage resources efficiently. If you're not using an official SDK, make sure you respond with `PONG` before that window elapses (the official SDKs handle this automatically).

> The ping/pong mechanism works by sending an empty `{}` message to the client; the pong reply must also be `{}`.

#### Subscribing to multiple channels at once

You don't need separate clients for multiple channels — one client can subscribe to many channels simultaneously.

- **Subscription limit:** max 450 channels per connection

```javascript
const channels = ['public:orderbook-BTCIRT', 'public:orderbook-USDTIRT', 'public:orderbook-FTMIRT']
const subs = channels.map(channel => {
  const sub = client.newSubscription(channel, { delta: 'fossil' })
  sub.subscribe()
  sub.on('publication', (ctx) => {
    console.log(channel, ctx.data);
  })
  return sub
});
```

#### Note on other-language SDKs

For details on connecting using other language SDKs, see the documentation links above — there are minor differences between SDKs/environments. For example, in Node.js you need to install and explicitly pass the `websocket` module to the client at connection time, as covered in [that SDK's docs](https://github.com/centrifugal/centrifuge-js?tab=readme-ov-file#using-with-nodejs).

### Get a WebSocket Token

**Endpoint:** `GET /auth/ws/token`

```shell
curl 'https://apiv2.nobitex.ir/auth/ws/token/' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
  "token": "yJhbGciOiJFUzUxMiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxNjMiLCJleHAiOjE3MzE5Mzk0NDIsImlhdCI6MTczMTkzODI0MiwibWV0YSI6e319.AFZjNC0ugUcfchUKjjunNDl1kfemJkA0Y5IYRi1c0mSvIa_XxzQIeVeqg6qnTBzE-HG6zEqXXyCENJtAz7xc7wqcABWbpcHdH0fPtjG4pwDZco9O065RcXacXo7qpCN-IuC0te0qG2_2bAhc-aR7vWgHsTm6GXfrQQh_6uwVHShSarU7",
  "status": "ok"
}
```

You need to call this and obtain a token before subscribing to any `private:` prefixed channel.

### Authentication

```javascript
const centrifuge = new Centrifuge('wss://ws.nobitex.ir/connection/websocket', {
    token: '<CONNECTION_TOKEN>'
});
```

Automatic token refresh:
```javascript
import { Centrifuge, UnauthorizedError } from 'centrifuge';

async function getToken() {
    if (!loggedIn) {
        return "";
    }
    const res = await fetch('https://apiv2.nobitex.ir/auth/ws/token/', {
    method: 'GET',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Token ApiToken`
      }
    });
    if (!res.ok) {
        if (res.status === 403) {
            // Return special error to not proceed with token refreshes, client will be disconnected.
            throw new UnauthorizedError();
        }
        // Any other error thrown will result into token refresh re-attempts.
        throw new Error(`Unexpected status code ${res.status}`);
    }
    const data = await res.json();
    return data.token;
}

const client = new Centrifuge(
    'wss://ws.nobitex.ir/connection/websocket',
    {
        token: '<CONNECTION_TOKEN>',
        getToken: getToken
    }
);

client.on('connected', (ctx) => {
    console.log('connected', ctx);
});
client.connect();
```

Raw connect message with a token:
```json
{ "connect": {"token": "<CONNECTION_TOKEN>"}, "id": 1 }
```

Channels prefixed `private:` carry per-user data and require authenticating with a token plus the `{websocketAuthParam}` parameter.

**Channel types**

| Type | Pattern | Description | Example |
|---|---|---|---|
| **Public** | `public:channelName` | Available to everyone, no auth needed | `public:orderbook-BTCIRT` |
| **Private** | `private:channelName#{websocketAuthParam}` | User-specific data, requires `{websocketAuthParam}` + token | `private:trades#1987577cdf7c7422dee369e188e276ee` |

Get `{websocketAuthParam}` from the [Profile](#profile) endpoint.

**Example:** to receive your latest trades, subscribe to `private:trades#1987577cdf7c7422dee369e188e276ee` (using your own `websocketAuthParam`).

> `{websocketAuthParam}` is a fixed, unique value per user that never changes — no need to refresh it.

### Order Book Channel

Channels with this prefix carry **order book** data and push a message on every order book change:

**Pattern:** `public:orderbook-*`

> Replace `*` with the market symbol in UPPERCASE, e.g. `BTCIRT`, `ETHUSDT`, etc.

```javascript
const sub = client.newSubscription('public:orderbook-BTCIRT', { delta: 'fossil' });
sub.on('subscribed', (ctx) => {
    console.log('subscribed');
});
sub.on('publication', (ctx) => {
    console.log(ctx.data);
});
sub.subscribe();
```

Raw subscribe message:
```json
{ "id": 2, "subscribe": { "channel": "public:orderbook-BTCIRT" } }
```

The `ctx.data` payload (`console.log` output):
```json
{
  "asks": [
    ["35077909990", "0.009433"],
    ["35078000000", "0.000274"],
    ["35078009660", "0.00057"]
  ],
  "bids": [
    ["35020080080", "0.185784"],
    ["35020070060", "0.086916"],
    ["35020030010", "0.000071"]
  ],
  "lastTradePrice": "35077909990",
  "lastUpdate": 1726581829816
}
```

Raw push frame (if not using the official SDK):
```json
{
  "push": {
    "channel": "public:orderbook-BTCIRT",
    "pub": {
      "data": "{\"asks\": [[\"35077909990\", \"0.009433\"], [\"35078000000\", \"0.000274\"], [\"35078009660\", \"0.00057\"]], \"bids\": [[\"35020080080\", \"0.185784\"], [\"35020070060\", \"0.086916\"], [\"35020030010\", \"0.000071\"]], \"lastTradePrice\": \"35077909990\", \"lastUpdate\": 1726581829816}",
      "offset": 989153
    }
  }
}
```

**Example:** to receive BTC/IRT order book updates, subscribe to `public:orderbook-BTCIRT`.

**Important:** this channel only publishes when the order book actually changes — in low-volume markets, messages may be sparse. We recommend fetching the initial snapshot via [Order Book v3](#order-book-v3) before subscribing.

**Note on `{ delta: 'fossil' }`:** this optional flag on `newSubscription` makes the server send order book data as a diff, cutting bandwidth by roughly 60%. Official Centrifugo clients handle this transparently. If you're not using an official client, either avoid this flag, or implement the [Fossil delta algorithm](https://fossil-scm.org/) yourself to decompress the diffs.

**Response fields:** same shape as the REST [order book endpoint](#order-book-v3) — `asks`/`bids` arrays of "price, volume" pairs (sell/buy respectively), plus `lastTradePrice` (string) and `lastUpdate` (Unix ms).

### OHLC/Candle Channel

See [this link](https://en.wikipedia.org/wiki/Open-high-low-close_chart) for background on OHLC.

**Pattern:** `public:candle-{marketSymbol}-{resolution}`

> Candle data is published roughly every 10 seconds.
> Replace `{marketSymbol}` with the UPPERCASE symbol, e.g. `BTCIRT`, `ETHUSDT`.
> Replace `{resolution}` with a value from the table below, e.g. `5` or `2D`.

**Valid `{resolution}` values:**

| Minutes | | Hours | | Days | |
|---|---|---|---|---|---|
| `1` | 1 min | `60` | 1 hour | `D` | 1 day |
| `5` | 5 min | `180` | 3 hours | `2D` | 2 days |
| `15` | 15 min | `240` | 4 hours | `3D` | 3 days |
| `30` | 30 min | `360` | 6 hours | | |
| | | `720` | 12 hours | | |

```javascript
const sub = client.newSubscription('public:candle-BTCIRT-15', { delta: 'fossil' });
sub.on('subscribed', (ctx) => {
    console.log('subscribed');
});
sub.on('publication', (ctx) => {
    console.log(ctx.data);
});
sub.subscribe();
```

Raw subscribe message:
```json
{ "id": 2, "subscribe": { "channel": "public:candle-BTCIRT-15" } }
```

Payload (`ctx.data`):
```json
{ "t": 1731852900, "o": 6240000001.0, "h": 6250000000.0, "l": 6238000000.0, "c": 6238031033.0, "v": 1.26 }
```

Raw push frame:
```json
{
  "push": {
    "channel": "public:candle-BTCUSDT-15",
    "pub": {
      "data": "{\"t\":1731852900,\"o\":6240000001.0,\"h\":6250000000.0,\"l\":6238000000.0,\"c\":6238031033.0,\"v\":1.26}",
      "offset": 34575
    }
  }
}
```

**Example:** to receive 15-minute candles for BTC/IRT, subscribe to `public:candle-BTCIRT-15`.

**Note on `{ delta: 'fossil' }`:** same as above — optional, ~60% bandwidth savings, transparent in official clients.

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `t` | int | Candle open time | `1731852900` |
| `o` | float | Open price | `6240000001.0` |
| `h` | float | High price | `6250000000.0` |
| `l` | float | Low price | `6238000000.0` |
| `c` | float | Close price | `6238031033.0` |
| `v` | float | Volume | `1.26` |

### Public Trades Channel

Publishes every trade executed in a market.

**Pattern:** `public:trades-{marketSymbol}`

Example message:
```json
{ "price": "120000000000", "time": 1762781164192, "type": "sell", "volume": "0.000003" }
```

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `price` | monetary | Trade price | `12000.129` |
| `time` | int | Trade time | `1762781164192` |
| `type` | string | Side | `sell`, `buy` |
| `volume` | monetary | Trade volume | `13.002` |

### Market Stats Channel

Publishes general 24h market stats.

**Pattern (single market):** `public:market-stats-{marketSymbol}`
**Channel (all markets):** `public:market-stats-all`

Example message (single market):
```json
{
  "isClosed": false,
  "bestSell": "121073861950",
  "bestBuy": "120000000000",
  "volumeSrc": "0.000185131782",
  "volumeDst": "21590044.0170869949",
  "latest": "114879999920",
  "mark": "123288285750",
  "dayLow": "110850000360",
  "dayHigh": "121073861950",
  "dayOpen": "121073861910",
  "dayClose": "114879999920",
  "dayChange": "-5.12"
}
```

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `isClosed` | bool | Market open/closed | `true` |
| `bestSell` | monetary | Current best ask | `1000` |
| `bestBuy` | monetary | Current best bid | `1000` |
| `volumeSrc` | monetary | 24h trade volume | `13.002` |
| `volumeDst` | monetary | 24h trade value | `13002` |
| `latest` | monetary | Last traded price | `1002` |
| `mark` | monetary | Current mark price | `1002` |
| `dayLow` | monetary | 24h low | `800` |
| `dayHigh` | monetary | 24h high | `1200` |
| `dayOpen` | monetary | 24h open | `800` |
| `dayClose` | monetary | 24h close (latest) | `1200` |
| `dayChange` | float | 24h % change | `-2.12` |

Example message (all markets):
```json
{
  "btc-irt": {
    "isClosed": false, "bestSell": "121073861950", "bestBuy": "120000000000",
    "volumeSrc": "0.000185131782", "volumeDst": "21590044.0170869949",
    "latest": "114879999920", "mark": "123288285750", "dayLow": "110850000360",
    "dayHigh": "121073861950", "dayOpen": "121073861910", "dayClose": "114879999920",
    "dayChange": "-5.12"
  },
  "usdt-irt": {
    "isClosed": false, "bestSell": "121073861950", "bestBuy": "120000000000",
    "volumeSrc": "0.000185131782", "volumeDst": "21590044.0170869949",
    "latest": "114879999920", "mark": "123288285750", "dayLow": "110850000360",
    "dayHigh": "121073861950", "dayOpen": "121073861910", "dayClose": "114879999920",
    "dayChange": "-5.12"
  }
}
```

### Private Channel: Your Orders

Publishes every event related to your own orders.

**Pattern:** `private:orders#{websocketAuthParam}`

Requires authentication via [WebSocket token](#get-a-websocket-token).

Example message:
```json
{
  "amount": "0.0002",
  "avgFilledPrice": "114879999920",
  "clientOrderId": null,
  "dstCurrency": "rls",
  "eventTime": 1762779011366,
  "fee": "0.00000031",
  "filledAmount": "0.0002",
  "lastFillTime": 1762779011258,
  "marketType": "Spot",
  "orderId": 278339,
  "orderType": "Market",
  "param1": null,
  "price": null,
  "side": "Buy",
  "srcCurrency": "btc",
  "status": "Done",
  "tradeAmount": "0.0002",
  "tradeId": 92547,
  "tradePrice": "114879999920"
}
```

**Message fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `amount` | monetary | Order volume | `3.002` |
| `avgFilledPrice` | monetary | Filled value (IRR or USDT) | `520305923` |
| `clientOrderId` | string | Client tracking ID | `1032` |
| `dstCurrency` | string | Destination currency | `rls`, `usdt` |
| `eventTime` | int | Event time | `1762779011258` |
| `fee` | monetary | Order fee | `1.02` |
| `filledAmount` | monetary | Filled volume | `2.7812` |
| `lastFillTime` | int | Last fill time (nullable) | `1762779011258` |
| `marketType` | string | Order's market type | `Spot`, `Margin` |
| `orderId` | int | Order ID | `47150` |
| `orderType` | string | Order type | `Market`, `Limit` |
| `param1` | monetary | (stop price, when applicable) | `47150.7989334` |
| `price` | monetary | Order price (nullable) | `47150.7989334` |
| `side` | string | Order side | `Buy`, `Sell` |
| `srcCurrency` | string | Source currency | `btc` |
| `status` | string | Order status | `Done` |
| `tradeAmount` | monetary | Latest fill volume (nullable) | `0.002` |
| `tradeId` | int | Latest fill's trade ID (nullable) | `65451` |
| `tradePrice` | monetary | Latest fill price (nullable) | `47150.7989334` |

### Private Channel: Your Trades

Publishes details for every trade executed for you, the moment it happens.

**Pattern:** `private:trades#{websocketAuthParam}`

Requires authentication via [WebSocket token](#get-a-websocket-token).

```javascript
const sub = client.newSubscription('private:trades#1987577cdf7c7422dee369e188e276ee',  { delta: 'fossil' });
sub.on('subscribed', (ctx) => {
    console.log('subscribed');
});
sub.on('publication', (ctx) => {
    console.log(ctx.data);
});
sub.subscribe();
```

Raw subscribe message:
```json
{ "id": 2, "subscribe": { "channel": "private:trades#1987577cdf7c7422dee369e188e276ee" } }
```

Payload (`ctx.data`):
```json
{
    "srcCurrency": "btc",
    "dstCurrency": "rls",
    "timestamp": "2024-11-23T11:31:27.833332+00:00",
    "price": "66683959340",
    "amount": "0.000404",
    "total": "26940319.57336",
    "type": "sell",
    "fee": "47150.7989334",
    "id": 12942226,
    "orderId": 520305923
}
```

Raw push frame:
```json
{
  "push": {
    "channel": "private:trades#1987577cdf7c7422dee369e188e276ee",
    "pub": {
      "data": "{\"srcCurrency\":\"btc\",\"dstCurrency\":\"rls\", \"timestamp\":\"2024-11-23T11:31:27.833332+00:00\", \"price\":\"66683959340\",\"amount\":\"0.000404\",\"total\":\"26940319.57336\",\"type\":\"sell\",\"fee\":\"47150.7989334\",\"id\":12942226,\"orderId\":520305923}",
      "offset": 34575
    }
  }
}
```

**Message fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `id` | int | Trade ID | `12942226` |
| `orderId` | int | Order ID | `520305923` |
| `srcCurrency` | string | Source currency | `btc`, `eth`, `xrp`, ... |
| `dstCurrency` | string | Destination currency | `rls` or `usdt` |
| `timestamp` | string | Trade time | `2024-11-23T11:31:27.833332+00:00` |
| `type` | string | Side | `buy` or `sell` |
| `price` | monetary | Fill price | `66683959340` |
| `amount` | monetary | Filled amount | `0.000404` |
| `total` | monetary | Total value | `26940319.57336` |
| `fee` | monetary | Trade fee | `47150.7989334` |

---

