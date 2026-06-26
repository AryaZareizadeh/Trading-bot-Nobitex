## 5. Market Data (Public)

### Order Book (v3)

**Endpoint:** `GET /v3/orderbook/SYMBOL`
**Rate limit:** 300 requests/minute
**Token required:** No

⚠️ Use the `GET` method with the symbol in the URL path. `POST` is not supported on this endpoint.

```shell
curl 'https://apiv2.nobitex.ir/v3/orderbook/BTCIRT'
```

Example response:
```json
{
    "status": "ok",
    "lastUpdate": 1644991756704,
    "lastTradePrice": "35650565900",
    "asks": [
        ["1476091000", "1.016"],
        ["1479700000", "0.2561"]
    ],
    "bids": [
        ["1470001120", "0.126571"],
        ["1470000000", "0.818994"]
    ]
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `SYMBOL` | string | required | Market symbol (path param) | `BTCIRT` |

Use `all` as the symbol to get the order book for every market in a single call:

```shell
curl 'https://apiv2.nobitex.ir/v3/orderbook/all'
```

```json
{
  "status": "ok",
  "BTCIRT": {
    "lastUpdate": 1644991756704,
    "asks": [["1476091000", "1.016"], ["1479700000", "0.2561"]],
    "bids": [["1470001120", "0.126571"], ["1470000000", "0.818994"]]
  },
  "USDTIRT": {
    "lastUpdate": 1644991767392,
    "asks": [["277990", "6688.3"], ["278000", "28185.03"]],
    "bids": [["277960", "119.31"], ["271240", "1079.75"]]
  }
}
```

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `asks` | array | "price, volume" pairs for **sell** orders | `[["1231","0.1"],["1243","1.02"]]` |
| `bids` | array | "price, volume" pairs for **buy** orders | `[["1243","1"],["1231","2"]]` |
| `lastTradePrice` | string | Price of the last trade | `"35602702700"` |
| `lastUpdate` | int | Last update time (Unix ms) | `1726651067347` |

**Recommended way to keep an order book in sync:** use the [WebSocket API](#12-websocket-api). If WebSocket isn't an option and you need to keep a local copy of the order book, poll in a loop with **at least 1–10 seconds** between iterations — never poll back-to-back without delay. Because of caching layers, requests within the same second return the previous cached payload. If you're active on most Nobitex markets, fetch the full order book (`all`) each time rather than polling multiple symbols concurrently. If your code runs across multiple threads/processes/servers, fetch the order book from one single place and share it across instances.

### Depth Chart (Beta)

**Endpoint:** `GET /v2/depth/SYMBOL`
**Rate limit:** 300 requests/minute
**Token required:** No

⚠️ Use `GET` with the symbol in the URL.

```shell
curl 'https://apiv2.nobitex.ir/v2/depth/BTCIRT'
```

Example response:
```json
{
  "status": "ok",
  "lastUpdate": "1658565992195",
  "bids": [["7309999000", "2.200274"], ["7310000000", "2.199856"], ["7310025000", "2.102569"]],
  "asks": [["7409894000", "0.054789"], ["7447665000", "0.055276"], ["7450000000", "0.059756"]],
  "lastTradePrice": "7417000000"
}
```

**Request parameters:** same as order book — `SYMBOL` (path, required), e.g. `BTCIRT`.

**Response fields:** `asks`/`bids` are "price, volume" pairs (buy in `bids`, sell in `asks`), with price aggregated/rounded to the market's precision. `lastUpdate` is Unix time; `lastTradePrice` is the last trade's price.

**Recommended polling approach:** same caching/loop guidance as the order book — wait 1–10s between calls; sub-1-second calls return cached data.

### Trades List

**Endpoint:** `GET /v2/trades/SYMBOL`
**Rate limit:** 60 requests/minute
**Token required:** No

```shell
curl 'https://apiv2.nobitex.ir/v2/trades/BCHIRT'
```

Example response:
```json
{
    "status": "ok",
    "trades": [
        {
            "time": 1588689375067,
            "price": "1470000110",
            "volume": "0",
            "type": "sell"
        },
        {
            "time": 1588689360464,
            "price": "1470000110",
            "volume": "0.002",
            "type": "buy"
        }
    ]
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `symbol` | string | required | Market symbol | `BTCIRT` |

The returned `time` is the exact Unix timestamp of when the trade occurred.

### Nobitex Market Stats

**Endpoint:** `GET /market/stats`
**Rate limit:** 20 requests/minute

```shell
curl 'https://apiv2.nobitex.ir/market/stats?srcCurrency=btc&dstCurrency=rls'
```

Example response:
```json
{
  "status": "ok",
  "stats": {
    "btc-rls": {
      "isClosed": false,
      "bestSell": "749976360",
      "bestBuy": "733059600",
      "volumeSrc": "0.2929480000",
      "volumeDst": "212724856.0678640000",
      "latest": "750350000",
      "mark": "747461987",
      "dayLow": "686021860",
      "dayHigh": "750350000",
      "dayOpen": "686021860",
      "dayClose": "750350000",
      "dayChange": "9.38"
    }
  }
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `srcCurrency` | string | optional | Source currency/currencies | `btc,usdt` |
| `dstCurrency` | string | optional | Destination currency | `rls` |

Both parameters are optional; omitting them returns stats for the whole market. Either can be passed alone — e.g. setting only `dstCurrency=rls` returns all IRR-quoted markets.

⚠️ For backward compatibility, the response object also includes Binance market stats under the `global` key — this is deprecated and will be removed entirely in a future version.

### Nobitex OHLC Stats

**Endpoint:** `GET /market/udf/history`

See [this link](https://en.wikipedia.org/wiki/Open-high-low-close_chart) for background on OHLC data.

```shell
curl 'https://apiv2.nobitex.ir/market/udf/history?symbol=BTCIRT&resolution=D&from=1562058167&to=1562230967'
```

Example response:
```json
{
  "s": "ok",
  "t": [1562095800, 1562182200],
  "o": [146272500, 150551000],
  "h": [155869600, 161869500],
  "l": [140062400, 150551000],
  "c": [151440200, 157000000],
  "v": [18.221362316, 9.8592626506]
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `symbol` | string | required | Market symbol | `BTCIRT` |
| `resolution` | string | required | Candle resolution | `D` |
| `to` | int | required | End of range (Unix seconds) | `1562230967` |
| `from` | int | optional | Start of range (Unix seconds) | `1562058167` |
| `countback` | int | optional | Number of candles before `to` (takes priority over `from`) | `4` |
| `page` | int | `1` | Page number | `3` |

Invalid `resolution` response:
```json
{ "s": "error", "errmsg": "Invalid resolution!" }
```

**Valid `resolution` values:**

| Minutes | | Hours | | Days | |
|---|---|---|---|---|---|
| `1` | 1 min | `60` | 1 hour | `D` | 1 day |
| `5` | 5 min | `180` | 3 hours | `2D` | 2 days |
| `15` | 15 min | `240` | 4 hours | `3D` | 3 days |
| `30` | 30 min | `360` | 6 hours | | |
| | | `720` | 12 hours | | |

`from`/`to` mark the search range start/end, in Unix seconds.

No-data response:
```json
{ "s": "no_data" }
```

Note: 1-minute candles are only available starting from the beginning of the Iranian year 1401 (~March 2022); earlier 1-minute candles aren't available.

**Response fields**

| Field | Description | Type | Example |
|---|---|---|---|
| `s` | Response status | string | `ok` |
| `t` | Candle open time | [ ]int | `[1562182200]` |
| `o` | Open price | [ ]float | `[150551000]` |
| `h` | High price | [ ]float | `[161869500]` |
| `l` | Low price | [ ]float | `[150551000]` |
| `c` | Close price | [ ]float | `[157000000]` |
| `v` | Volume | [ ]float | `[9.8592626506]` |

Each request returns at most **500 candles**. Use `page` to paginate through ranges containing more than 500 candles.

---

