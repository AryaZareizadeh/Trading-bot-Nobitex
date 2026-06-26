## 19. General Notes (Rate Limits, Errors, Conventions)

### Troubleshooting checklist
- Double-check the HTTP method — you might be using `GET` where `POST` is required.
- Double-check the URL, including trailing slashes.

### Successful responses

```json
{ "status": "ok", "otherFields": "..." }
```

Every successful call returns `HTTP 200` with a JSON object whose `status` field is `"ok"`. Depending on the endpoint, other fields are included too.

### Failed responses

```json
{ "status": "failed", "code": "ErrorCode", "message": "Human-readable error message" }
```

If a request can't be processed, you get an unsuccessful response — either via a non-200 HTTP status, or (when input was understandable but the operation itself couldn't proceed) via `HTTP 200` with `"status": "failed"`. The `code` field gives the specific error (each endpoint's docs list its possible codes above); `message` gives a human-readable explanation, useful for debugging or potentially showing to end users.

> Tip: build your UI around the `code` field (and localize messages yourself for your users) — `message` is meant for debugging, not necessarily ideal for direct display.

**Common HTTP status codes**

| Code | Title | Description |
|---|---|---|
| 200 | OK | Request received and processed — actual outcome is in the `status` field of the response body |
| 400 | Bad Request | Malformed/insufficient parameters |
| 401 | Unauthorized | Not authenticated — see [Authentication](#2-authentication--tokens) |
| 403 | Forbidden | Operation not permitted |
| 404 | Not Found | Resource not found |
| 422 | Unprocessable Content | Parameters are valid but the request can't be fulfilled semantically |
| 429 | Too Many Requests | Rate limit exceeded |
| 500 | Internal Server Error | Temporary server-side issue |

### Pagination

Most list endpoints support:

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `page` | int | optional | Page number | `2` |
| `pageSize` | int | optional | Page size | `10` |

`page` and `pageSize` accept values from 1 to 100. Each endpoint's default page size varies, but 50 is typical.

### Date filtering

List endpoints that support date filtering accept:

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `from` | date | optional | Start date | `2022-05-12` |
| `to` | date | optional | End date | `2022-07-22` |

### Monetary values

Many request parameters are typed as `monetary`. For maximum precision, send these as **strings** — using floating-point types isn't recommended for financial values.

### Two-factor authentication

If 2FA is enabled on your account, certain endpoints (notably [login](#login--get-token)) require the OTP code in a request header:
`X-TOTP: 123456`

### Rate Limits

Example error:
```json
{
  "status": "failed",
  "code": "TooManyRequests",
  "message": "تعداد درخواست شما بیش از حد معمول تشخیص داده شده. لطفا 12 ثانیه صبر نمایید.",
  "backOff": 12,
  "limit": 60
}
```
(`message` translates to: "Your request rate has been flagged as unusually high. Please wait 12 seconds.")

Some Nobitex APIs are rate-limited per time window. Normal usage patterns shouldn't hit these limits. Limits are computed independently per endpoint, typically by IP and sometimes by user/token.

If you exceed a limit, you get `HTTP 429` with `code: "TooManyRequests"`. Wait `backOff` seconds before retrying; `limit` shows the cap for that window.

> ⚠️ Repeatedly and aggressively ignoring rate limits will get your token blocked across **all** APIs for 2 minutes.

Rate limits exist to protect platform capacity and account security, and to keep service quality consistent for everyone. During periods of heavy market activity, a given endpoint's per-user limit may be temporarily lowered to keep things fair.

If you're consistently hitting a limit and believe it should be raised, open an issue on [the docs GitHub repo](https://github.com/nobitex/docs-api/issues/new).

#### Shared order-placement rate limit

All order-placement endpoints share a combined limit — e.g. placing orders in both spot and margin markets simultaneously counts against the same shared limit.

**Shared limit:** 300 requests / 10 minutes

#### Failed-auth rate limit

For account security: if more than 100 requests with an invalid (or expired) token come from the same IP within 30 minutes, that IP is blocked for the remainder of that window. Manage your token renewal per the [Authentication](#2-authentication--tokens) section to avoid this.

### Standard vs. "Pro" mode

Some endpoints apply extra protective restrictions for typical users — these are noted in each endpoint's "Notes" section, marked "(disabled in Pro mode)". Setting `pro: "yes"` on the request disables that particular restriction. These restrictions exist to prevent common mistakes — only enable Pro mode if you understand and accept the implications.

---

## 20. FAQ

**I have a question about login and getting a token.**
First read [Authentication & Tokens](#2-authentication--tokens), the [Quickstart](#4-quickstart), and [Recommended way to get a token](#login-api-auth). Then check the [Login FAQ](#login-faq) for common questions.

**Does Nobitex have a test/sandbox environment?**
Yes. You can use the full Nobitex platform and APIs in the test environment:
- Test website: `https://testnet.nobitex.ir`
- Test API: `https://testnetapiv2.nobitex.ir`

**Are there restrictions on using the APIs?**
Yes — for example, requests must come from an Iranian IP, plus any endpoint-specific limits noted in each endpoint's documentation.

**What date/time format is used?**
Unix time — the number of seconds since 00:00:00 UTC on January 1, 1970 (not counting leap seconds). Negative numbers represent times before that. E.g., `1970-01-01 00:00:00` = `0`, and `1970-01-01 00:01:00` = `60`. See [Unix time on Wikipedia](https://en.wikipedia.org/wiki/Unix_time) for more.

**Is there sample code for the APIs?**
Yes — a full Postman collection covering all the APIs is available [here](https://documenter.getpostman.com/view/5722122/Szmcayjw?version=latest), with sample calls in multiple languages (PHP, Python, C#, etc.) that you can run directly.

---

## 21. Market & Currency Symbols

These lists are dynamically inserted on the live docs page wherever you see "valid market symbols are..." or similar notices. Current values as of this writing:

**Valid market symbols** (`market-symbols`):

```
BTCIRT, ETHIRT, LTCIRT, USDTIRT, XRPIRT, BCHIRT, BNBIRT, EOSIRT, XLMIRT, ETCIRT, TRXIRT, DOGEIRT, UNIIRT, DAIIRT, LINKIRT, DOTIRT, AAVEIRT, ADAIRT, SHIBIRT, FTMIRT, MATICIRT, AXSIRT, MANAIRT, SANDIRT, AVAXIRT, MKRIRT, GMTIRT, USDCIRT, BTCUSDT, ETHUSDT, LTCUSDT, XRPUSDT, BCHUSDT, BNBUSDT, EOSUSDT, XLMUSDT, ETCUSDT, TRXUSDT, PMNUSDT, DOGEUSDT, UNIUSDT, DAIUSDT, LINKUSDT, DOTUSDT, AAVEUSDT, ADAUSDT, SHIBUSDT, FTMUSDT, MATICUSDT, AXSUSDT, MANAUSDT, SANDUSDT, AVAXUSDT, MKRUSDT, GMTUSDT, USDCUSDT, CHZIRT, GRTIRT, CRVIRT, BANDUSDT, COMPUSDT, EGLDIRT, HBARUSDT, GALIRT, HBARIRT, WBTCUSDT, IMXIRT, WBTCIRT, ONEIRT, GLMUSDT, ENSIRT, 1M_BTTIRT, SUSHIIRT, LDOIRT, ATOMUSDT, ZROIRT, STORJIRT, ANTIRT, AEVOUSDT, 100K_FLOKIIRT, RSRUSDT, API3USDT, GLMIRT, XMRIRT, ENSUSDT, OMIRT, RDNTIRT, MAGICUSDT, TIRT, ATOMIRT, NOTIRT, CVXIRT, XTZIRT, FILIRT, UMAIRT, 1B_BABYDOGEIRT, BANDIRT, SSVIRT, DAOIRT, BLURIRT, ONEUSDT, EGALAUSDT, GMXIRT, XTZUSDT, FLOWUSDT, GALUSDT, WIRT, CVCUSDT, NMRUSDT, SKLIRT, SNTIRT, BATUSDT, TRBUSDT, NMRIRT, RDNTUSDT, API3IRT, CVCIRT, WLDIRT, YFIUSDT, SOLIRT, TUSDT, QNTUSDT, IMXUSDT, AEVOIRT, GMXUSDT, ETHFIUSDT, QNTIRT, GRTUSDT, WLDUSDT, FETIRT, AGIXIRT, NOTUSDT, LPTIRT, SLPIRT, MEMEUSDT, SOLUSDT, BALUSDT, DAOUSDT, COMPIRT, MEMEIRT, TONUSDT, BATIRT, SNXIRT, TRBIRT, 1INCHUSDT, OMUSDT, RSRIRT, RNDRIRT, SLPUSDT, SSVUSDT, RNDRUSDT, AGLDIRT, NEARUSDT, WOOUSDT, YFIIRT, MDTIRT, CRVUSDT, MDTUSDT, EGLDUSDT, LRCIRT, LPTUSDT, BICOUSDT, 1M_PEPEIRT, BICOIRT, MAGICIRT, ETHFIIRT, ANTUSDT, 1INCHIRT, APEUSDT, 1M_NFTIRT, ARBIRT, LRCUSDT, WUSDT, BLURUSDT, CELRUSDT, DYDXIRT, CVXUSDT, BALIRT, TONIRT, 100K_FLOKIUSDT, JSTUSDT, ZROUSDT, ARBUSDT, APTIRT, 1M_NFTUSDT, CELRIRT, UMAUSDT, SKLUSDT, ZRXUSDT, AGLDUSDT, ALGOIRT, NEARIRT, APTUSDT, ZRXIRT, SUSHIUSDT, FETUSDT, ALGOUSDT, 1M_PEPEUSDT, MASKIRT, EGALAIRT, FLOWIRT, 1B_BABYDOGEUSDT, MASKUSDT, 1M_BTTUSDT, STORJUSDT, XMRUSDT, OMGIRT, SNTUSDT, APEIRT, FILUSDT, ENJUSDT, OMGUSDT, WOOIRT, CHZUSDT, ENJIRT, DYDXUSDT, AGIXUSDT, JSTIRT, LDOUSDT, SNXUSDT
```

**Valid currency symbols** (`currency-symbols`):

```
rls, btc, eth, ltc, usdt, xrp, bch, bnb, eos, xlm, etc, trx, pmn, doge, uni, dai, link, dot, aave, ada, shib, ftm, matic, axs, mana, sand, avax, mkr, gmt, atom, uma, w, rsr, wld, 1m_nft, flow, agld, ton, mask, snt, agix, algo, ssv, band, omg, comp, zrx, rdnt, imx, 1inch, mdt, sushi, bico, gmx, zro, bal, dao, gal, not, nmr, xmr, enj, apt, lrc, dydx, grt, near, cvx, 100k_floki, fil, sol, ldo, crv, aevo, qnt, om, woo, storj, ant, 1m_btt, magic, ape, rndr, hbar, lpt, glm, blur, wbtc, meme, ethfi, egala, arb, fet, skl, cvc, snx, jst, ens, trb, chz, xtz, api3, slp, t, bat, 1b_babydoge, celr, yfi, egld, one, 1m_pepe, usdc
```

**Valid network symbols** (`network-symbols`):

```
FIAT_MONEY, ETH, BSC, ADA, ALGO, APT, ARB, BCH, BNB, BTC, BTCLN, DOGE, DOT, EOS, ETC, LTC, PMN, TRX, OMNI, ZTRX, XLM, XMR, XRP, ATOM, EGLD, FIL, FLR, FLOW, FTM, MATIC, AVAX, HBAR, NEAR, TON, SOL, XTZ, ONE
```

**Networks that require a tag/memo** (`tag-required-networks`):

```
BNB, EOS, PMN, XLM, XRP
```

> ⚠️ These lists change over time as Nobitex adds new markets, currencies, and networks. For the current, authoritative list, always check the live page at [apidocs.nobitex.ir](https://apidocs.nobitex.ir/#api) or call [`GET /v2/options`](#system-options) / [`GET /market/stats`](#nobitex-market-stats) programmatically rather than hardcoding this snapshot.

---

*End of reference. Generated from the official Nobitex API documentation source (github.com/nobitex/docs-api), consolidated and translated into English for developer reference. Verify against [apidocs.nobitex.ir](https://apidocs.nobitex.ir/#api) for the latest live version, since this is a point-in-time snapshot.*
