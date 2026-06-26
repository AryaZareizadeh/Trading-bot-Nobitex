## 17. Portfolio

Your "Portfolio" / P&L panel reports gains/losses (and percentages relative to your cost basis) driven by your trades and crypto price moves.

Currently available:
- Daily P&L for the last week
- Cumulative daily P&L for the last week
- Total P&L for the last month

> ⚠️ This feature is in **beta**.

### Daily P&L — Last Week

**Endpoint:** `POST /users/portfolio/last-week-daily-profit`
**Rate limit:** 10 requests / 3 minutes

```shell
curl -X POST 'https://apiv2.nobitex.ir/users/portfolio/last-week-daily-profit' \
  -H 'Authorization: Token yourTOKENhereHEX0000000000'
```

Example response:
```json
{
    "status": "ok",
    "data": [
        { "report_date": "2021-06-30", "total_profit": 0, "total_profit_percentage": 0, "total_balance": 0 },
        { "report_date": "2021-07-01", "total_profit": 0, "total_profit_percentage": 0, "total_balance": 0 },
        { "report_date": "2021-07-02", "total_profit": 0, "total_profit_percentage": 0, "total_balance": 0 },
        { "report_date": "2021-07-03", "total_profit": 0, "total_profit_percentage": 0, "total_balance": 0 },
        { "report_date": "2021-07-04", "total_profit": 0, "total_profit_percentage": 0, "total_balance": 0 },
        { "report_date": "2021-07-05", "total_profit": "0E-10", "total_profit_percentage": "0E-10", "total_balance": "4516559.9205900000" },
        { "report_date": "2021-07-06", "total_profit": "152570.1426800000", "total_profit_percentage": "3.3780165737", "total_balance": "4669130.0632700000" }
    ]
}
```

If this feature isn't enabled for your account:
```json
{ "status": "failed", "code": "PortfolioDisabled", "message": "Portfolio feature is not available for your user." }
```

If there's no P&L data available:
```json
{ "status": "failed", "code": "LastWeekDailyProfitFail", "message": "اطلاعاتی جهت نمایش وجود ندارد" }
```
(Translation: "No data available to display.")

**Notes**

By default this returns the last 7 days. Pass `monthly: true` to get the last 30 days instead.

### Cumulative Daily P&L — Last Week

**Endpoint:** `POST /users/portfolio/last-week-daily-total-profit`
**Rate limit:** 10 requests / 3 minutes

```shell
curl -X POST 'https://apiv2.nobitex.ir/users/portfolio/last-week-daily-total-profit' \
  -H 'Authorization: Token yourTOKENhereHEX0000000000'
```

Example response:
```json
{
    "status": "ok",
    "data": [
        { "report_date": "2021-06-27", "total_profit": 0, "total_profit_percentage": 0 },
        { "report_date": "2021-06-28", "total_profit": 0, "total_profit_percentage": 0 },
        { "report_date": "2021-06-29", "total_profit": "4507274.2415300000", "total_profit_percentage": "197.3669039200" },
        { "report_date": "2021-06-30", "total_profit": "9020935.7092505000", "total_profit_percentage": "-307.6135237294" },
        { "report_date": "2021-07-01", "total_profit": "-5373087.9340195000", "total_profit_percentage": "1302.7642741973" },
        { "report_date": "2021-07-02", "total_profit": "313580813.4306171358", "total_profit_percentage": "-899.0991999052" },
        { "report_date": "2021-07-03", "total_profit": "313580813.4306171358", "total_profit_percentage": "-899.0991999052" }
    ]
}
```

Same error responses as above (`PortfolioDisabled`, `LastWeekDailyProfitFail`).

### Total P&L — Last Month

**Endpoint:** `POST /users/portfolio/last-month-total-profit`
**Rate limit:** 10 requests / 3 minutes

```shell
curl -X POST 'https://apiv2.nobitex.ir/users/portfolio/last-month-total-profit' \
  -H 'Authorization: Token yourTOKENhereHEX0000000000'
```

Example response:
```json
{
    "status": "ok",
    "data": {
        "total_profit": "8987787000.0000000000",
        "total_profit_percentage": "83.60700983438201160181729556"
    }
}
```

Same error responses as above (`PortfolioDisabled`, `LastWeekDailyProfitFail`).

---

## 18. Other Endpoints

### System Options

**Endpoint:** `GET /v2/options`
**Token required:** No
**Input parameters:** None

```shell
curl 'https://apiv2.nobitex.ir/v2/options'
```

General response shape (keys documented below):
```json
{
    "status": "ok",
    "features": {
        "fcmEnabled": true,
        "chat": "livechat",
        "walletsToNet": false,
        "autoKYC": true,
        "enabledFeatures": ["PriceAlert", "StopLoss", "GiftCard", "OCO"],
        "betaFeatures": []
    },
    "coins": [
        {
            "coin": "rls",
            "name": "Rial",
            "defaultNetwork": "FIAT_MONEY",
            "displayPrecision": "10",
            "networkList": {
                "FIATMONEY": {
                    "addressRegex": "",
                    "coin": "rls",
                    "depositEnable": true,
                    "isDefault": true,
                    "minConfirm": 0,
                    "name": "FIAT",
                    "network": "FIAT_MONEY",
                    "withdrawEnable": true,
                    "withdrawFee": "4_000_0.00000000",
                    "withdrawIntegerMultiple": "0.10000000",
                    "withdrawMax": "500_000_000_0.00000000",
                    "withdrawMin": "15_000_0.00000000"
                }
            },
            "stdName": "﷼"
        }
        /* ... other coins ... */
    ],
    "nobitex": {
        "allCurrencies": ["rls", "btc" /* ... */],
        "activeCurrencies": ["rls", "btc", "eth", "ltc" /* ... */],
        "xchangeCurrencies": [],
        "topCurrencies": ["btc", "eth", "usdt", "doge", "shib", "trx", "ada", "ltc", "xrp"],
        "testingCurrencies": ["hbar"],
        "withdrawLimits": {
            "normal": { "dailyCoin": "0", "dailyRial": "0", "dailySummation": "0", "monthlySummation": "0" },
            "level0": { "dailyCoin": "0", "dailyRial": "0", "dailySummation": "0", "monthlySummation": "0" },
            "44": { "dailyCoin": "1000000000", "dailyRial": "200000000", "dailySummation": "1000000000", "monthlySummation": "15000000000" }
            /* ... other levels ... */
        },
        "minOrders": { "rls": "3000000", "usdt": "11", "2": "3000000", "13": "11" },
        "amountPrecisions": { "BTCIRT": "0.000001", "BTCUSDT": "0.000001", "ETHIRT": "0.00001" /* ... */ },
        "pricePrecisions": { "BTCIRT": "10", "BTCUSDT": "0.01", "ETHIRT": "10" /* ... */ },
        "giftCard": { "physicalFee": "360000" }
    }
}
```

Nobitex's platform behavior is driven by various system-wide settings, exposed here — active currencies, per-market minimum trade sizes, fee tiers, withdrawal limits, and more. The response has two top-level objects: `nobitex` (general system settings) and `coins` (per-currency settings). Each currency supports multiple networks, and some settings are network-level while others are currency-level (and may be overridden per network).

> Note: BabyDoge isn't listed in this endpoint's output for backward-compatibility reasons with older apps.

**`features` object**

| Key | Type | Description |
|---|---|---|
| `fcmEnabled` | bool | Whether push notifications (FCM) are enabled |
| `autoKYC` | bool | Whether automatic identity verification is enabled |
| `enabledFeatures` | list of str | Active features |
| `betaFeatures` | list of str | Beta features |

**`nobitex` object**

| Key | Type | Description |
|---|---|---|
| `allCurrencies` | list of str | All currencies across all markets |
| `activeCurrencies` | list of str | Main/tier-1 currencies |
| `xchangeCurrencies` | list of str | Exchange-specific currencies |
| `topCurrencies` | list of str | Top market currencies |
| `testingCurrencies` | list of str | Currencies in development/testing |
| `withdrawLimits` | dict | Daily/monthly limits per account level |
| `minOrders` | dict | Minimum order value in IRR/USDT markets |
| `amountPrecisions` | dict | Amount decimal precision per market |
| `pricePrecisions` | dict | Price decimal precision per market |
| `giftCard` | dict | Physical gift card printing fee |

**`coins[]` item fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `coin` | str | Currency symbol, usually lowercase | `rls`, `btc`, `eth`, ... |
| `name` | str | English name | `Bitcoin`, `TRON`, `Dogecoin`, ... |
| `default_network` | str | Default network (a key in this coin's `network_list`) | `FIAT_MONEY`, `BTC`, `BSC`, ... |
| `network_list` | dict | Dict of this coin's networks, keyed by network name → NetworkOptions | |
| `displayPrecision` | str | Default display decimal precision | `0.0001`, `0.1`, `1` |
| `stdName` | str | Display name | `ریال` |

**Network object (each entry in `network_list`)**

| Field | Type | Description | Example |
|---|---|---|---|
| `network` | str | Network symbol, usually uppercase | `FIAT_MONEY`, `BTC`, `BSC`, ... |
| `name` | str | Human-readable network name | `BTC`, `ERC20`, ... |
| `isDefault` | boolean | Is this the default network for the coin? | `false` |
| `beta` | boolean | Is this network still in beta? | `false` |
| `addressRegex` | str | Regex pattern for valid addresses | `^0x[0-9A-Fa-f]{40}$` |
| `memoRequired` | boolean | Does this network require a memo/tag? | `false` |
| `memoRegex` | str | Regex pattern for the memo, if applicable | `^[0-9A-Za-z]{1,28}$` |
| `depositEnable` | boolean | Are deposits enabled on this network? | `true` |
| `minConfirm` | int | Min. confirmations for initial deposit credit | `1` |
| `depositInfo` | dict | Deposit limit info | `{"standard": {"depositMin": "0.01"}}` |
| `depositInfo.standard.depositMin` | monetary | Min. deposit amount | `0` |
| `depositInfo.standard.depositMax` | monetary | Max. deposit amount | `1000` |
| `withdrawEnable` | boolean | Are withdrawals enabled on this network? | `false` |
| `withdrawIntegerMultiple` | monetary | Step size for withdrawal amounts | `0.000001` |
| `withdrawFee` | monetary | Withdrawal fee | `0.1` |
| `withdrawMin` | monetary | Min. withdrawal amount | `0.1` |
| `withdrawMax` | monetary | Max. withdrawal amount | `1000` |

---

