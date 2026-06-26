## 1. Introduction

Welcome to the API documentation for [Nobitex](https://nobitex.ir), Iran's largest cryptocurrency exchange. Nobitex has offered API access to all its users and developers since inception. With the Nobitex API you can, in addition to monitoring the latest prices and market status for crypto assets in Iran, manage your Nobitex account programmatically. Besides giving users new capabilities, API access enables automated trading, which helps markets become more liquid and price discovery fairer.

Before using the Nobitex API, make sure you are fully familiar with [Nobitex's Terms of Service](https://nobitex.ir/policies/terms/) and the [Nobitex API Terms of Use](/terms/). Because the platform and the API's structure/details may change over time, keep an eye on the [official Nobitex Telegram channel](https://t.me/nobitexmarket) and the [official Nobitex API Telegram channel](https://t.me/NobitexAPINews). Also continuously monitor the [Nobitex API changelog](/changelog/) to stay informed of all API changes.

When using the API, always make sure your code can gracefully handle error states and unexpected conditions. Given the sensitivity of financial markets, code that consumes this API should be built with sound engineering practices: rate limiting, error handling, prevention of cascading failures, defense-in-depth safeguards, monitoring/alerting systems, and risk-management mechanisms.

If this is your first time using the Nobitex API, see the [Quickstart](#4-quickstart) section below.

## 2. Authentication & Tokens

```
Authorization: Token yourTOKENhereHEX0000000000
```

Non-public APIs require sending a token. The token must be sent as an HTTP header in this form:

`Authorization: Token yourTOKENhereHEX0000000000`

Except for endpoints explicitly labeled "Public" in their title, every API requires a token to be sent. The token identifies which user is making the request.

> ⚠️ **Never share your token with anyone.** If your token is accidentally exposed, log out immediately and log back in to obtain a new token.

To obtain a token, log in to your Nobitex account, go to your profile, open the [Settings page](https://nobitex.ir/app/settings/), and retrieve your token there. If you selected "Remember me" when logging in to Nobitex, this token stays valid for up to 30 days or until you log out of Nobitex.

If you'd rather fetch a token programmatically and periodically, you can use the [Login API — Get Token](#16-login-api-auth). This isn't required for most users — getting a token directly from the user panel is the recommended approach. Only use the token-retrieval API if you understand the risks of storing your password in code, know secure practices for doing so, are experienced with API usage, and genuinely need fully automated token retrieval.

## 3. API Keys (Beta)

API Keys are designed to give more secure, scoped access to Nobitex APIs than a plain account token. An API Key lets you access Nobitex APIs in a controlled way — restricted to a specific IP — and lets you set an expiration date for the key.

### Why use an API Key?

- Greater security: define a restricted permission scope using one of three permissions — `READ`, `WITHDRAW`, `TRADE`
- **IP whitelist** support
- Custom expiration date
- Can be disabled/deleted without changing your main account password
- Well suited for bots and automated scripts

### Permission levels

- **READ** → fetch information without modifying anything in the database
- **TRADE** → trading-related operations that modify the database
- **WITHDRAW** → withdrawal operations

Note: APIs such as listing your orders require the `READ` permission. A single key may carry one or more permissions.

### Create an API Key

**Endpoint:** `POST /apikeys/create`

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `name` | string | required | Name to identify the key | `"my-api-key"` |
| `description` | string | empty (`''`) | Free-form description | `"API key for internal services"` |
| `permissions` | string (enum) | required | Allowed values: `READ`, `TRADE`, `WITHDRAW` (comma-separated string) | `"READ,TRADE"` |
| `ipAddressesWhitelist` | list | `[]` | List of allowed IPs (IPv4/IPv6) | `["192.168.1.10", "10.0.0.5"]` |
| `expirationDate` | datetime (UTC) | `None` | Key expiration date (optional) | `"2025-12-31T23:59:59Z"` |

**Security notes**
- Sending **OTP (2FA)** in the `X-totp` header is required.
- A notification email is sent to the user after a key is created.
- The key-creation event is logged.

Example request:
```json
{
  "name": "my-api-key",
  "description": "API key for internal services",
  "permissions": "READ,TRADE",
  "ipAddressesWhitelist": [
    "192.168.1.10",
    "10.0.0.5"
  ],
  "expirationDate": "2025-12-31T23:59:59Z"
}
```

Example response:
```json
{
  "key": {
    "createdAt": "2025-09-02T16:50:29.381869Z",
    "description": "API key for internal services",
    "expirationDate": "2025-12-31T23:59:59Z",
    "ipAddressesWhitelist": ["192.168.1.10", "10.0.0.5"],
    "key": "5XOCQZSPLQM4MiLzuUnZoBuqgYgTKl40W2X5j1pxfIA=",
    "name": "my-api-key",
    "permissions": "READ,TRADE",
    "updatedAt": "2025-09-02T16:50:29.381876Z"
  },
  "privateKey": "S5y19KewZzheCWCO4xqMcwwvtR8vQ-hHjE_cdjz-XxE=",
  "status": "ok"
}
```

⚠️ The `privateKey` is shown **only once**, in this response. Store it somewhere secure (e.g. a secret manager).

### Manage keys

**List keys** — `GET /apikeys/list` → returns the user's keys with their status and details.

**Delete a key** — `POST /apikeys/delete/<public_key>` (public key goes in the URL)
```json
{ "status": "ok" }
```

**Update a key** — `POST /apikeys/update/<public_key>`
Editable fields: `name`, `description`, `ipAddressesWhitelist`. Returns the updated key object.

Example request:
```json
{
  "name": "my-api-key",
  "description": "API key for internal services",
  "ipAddressesWhitelist": [
    "192.168.1.10",
    "10.0.0.5"
  ]
}
```

Example response:
```json
{
  "key": {
    "createdAt": "2025-09-02T16:50:29.381869Z",
    "description": "API key for internal services",
    "expirationDate": "2025-12-31T23:59:59Z",
    "ipAddressesWhitelist": ["192.168.1.10", "10.0.0.5"],
    "key": "5XOCQZSPLQM4MiLzuUnZoBuqgYgTKl40W2X5j1pxfIA=",
    "name": "my-api-key",
    "permissions": "READ,TRADE",
    "updatedAt": "2025-09-02T16:50:29.381876Z"
  },
  "status": "ok"
}
```

### Permission → endpoint mapping

**READ** grants access to:

*User info:* `users/preferences`, `users/profile`, `users/verification/status`, `notifications/list`, `users/limitations`

*Market:* `market/orders/status`, `market/orders/list`, `market/trades/list`

*Portfolio:* `users/portfolio/last-week-daily-profit`, `users/portfolio/last-week-daily-total-profit`, `users/portfolio/last-month-total-profit`, `users/portfolio/daily_total_balance`

*Instant deposit:* `users/payments/ids-list`

*Wallet:* `users/wallets/list`, `v2/wallets`, `users/wallets/balance`, `users/wallets/transactions/list`, `users/transactions-history`, `users/wallets/deposits/list`, `users/wallets/withdraws/list`, `users/wallets/deposit/bank`, `users/wallets/deposit/shetab`, `withdraws/<withdraw_id>`

*Margin trading:* `margin/fee-rates`, `margin/delegation-limit`, `margin/v2/delegation-limit`, `positions/list`, `positions/active-count`, `positions/<position_id>/status`, `positions/<position_id>/edit-collateral/options`, `margin/predict/<category>`

**TRADE** grants access to:

*Margin:* `margin/orders/add`, `positions/<int:pk>/close`, `positions/<int:pk>/edit-collateral`

*Spot market:* `market/orders/add`, `market/orders/batch-add`, `market/orders/update-status`, `market/orders/cancel-old`, `market/orders/cancel-batch`, `users/wallets/convert`

**WITHDRAW** grants access to: `withdraws/<withdraw_id>/update-status`, `users/wallets/withdraw`, `users/wallets/withdraw-confirm`, `users/wallets/withdraw-cancel`

We recommend always setting an IP restriction on any key carrying the `WITHDRAW` permission.

### Using an API Key

Every request must include three headers:

| Header | Description |
|---|---|
| `Nobitex-Key` | Your public key |
| `Nobitex-Signature` | Signature computed with `Ed25519` |
| `Nobitex-Timestamp` | Current Unix timestamp (UTC, seconds) |

**Computing the signature:**

```
signature = base64(Ed25519(timestamp + method + url + body))
```

- **timestamp** — Unix time (seconds, UTC)
- **method** — HTTP method (`GET`, `POST`, etc.)
- **url** — full request path, e.g. `/market/orders/list?fromId=123`
- **body** — raw request body (for `POST`, `PUT`, etc.)

`Nobitex-Key` is your public key and is sent unchanged on every request.

```bash
curl -X POST "https://apiv2.nobitex.ir/orders/cancel-old" \
  -H "Content-Type: application/json" \
  -H "Nobitex-Key: <Your-Public-Key>" \
  -H "Nobitex-Signature: <Generated-Signature>" \
  -H "Nobitex-Timestamp: <Unix-Timestamp>" \
  -d '{
        "order": 27032,
        "status": "canceled"
      }'
```

### User-Agent

To help Nobitex identify and properly support bots, it is strongly recommended that every request set a `User-Agent` header matching the pattern `TraderBot/XXXXX`, where `XXXXX` is any unique name you choose for your bot. Following this convention improves support response quality when debugging issues.

### Limits

Each API's documentation below states any applicable limits.

For account security, if your device is recognized as new at login, a one-hour withdrawal restriction is applied to your account. If your full login flow (including 2FA) completes in under an hour, you can pass the `device` value you received in the login response on subsequent steps/calls to avoid this restriction.

Alternatively, you can use long-lived tokens.

### Changes & deprecated endpoints

Because crypto markets and the platform evolve continuously, the Nobitex API changes over time too. Long-term support of old API versions is generally complex and slows down delivery of new platform features. As a result, while keeping its overall structure and core building blocks stable, the Nobitex API is continually improved and updated. Watch the [API changelog](/changelog/) to stay aware of changes that may require updates to your code.

Endpoints that previously existed but are no longer supported are kept, for the record, on the [deprecated APIs page](/deprecated/). There may be additional undocumented APIs beyond what's covered here; these are not official and have no guarantee of continued support. Likewise, current documented APIs may return (or accept) extra fields beyond what's documented. Until such fields are added to the documentation, treat them as experimental/temporary and don't rely on them — just make sure your code doesn't break on unexpected fields and simply ignores them.

### Troubleshooting

If you're not getting the response you expect, first double-check that you've followed the relevant documentation correctly. The best way to debug is to isolate variables and test each case systematically using standard debugging techniques.

If that doesn't resolve it, check the [FAQ](#20-faq) and [General Notes](#19-general-notes-rate-limits-errors-conventions) sections. If the problem is account-specific and can't be resolved via the API or the Nobitex website, contact [Nobitex online support](https://nobitex.ir/contact-us/).

If part of the documentation is unclear, you have a suggestion about an existing API, or — after thorough investigation — you're sure there's a bug in the Nobitex API, open a new issue on the [Nobitex API docs GitHub repo](https://github.com/nobitex/docs-api). Note this is a public channel — never post sensitive info, tokens, or other account details there.

## 4. Quickstart

If this is your first time using the Nobitex API or any crypto exchange API, we suggest:

- [Sign up](https://nobitex.ir/signup/) on Nobitex and complete identity verification to at least level one.
- Go to your Nobitex profile → [Settings](https://nobitex.ir/app/settings/) and grab your token. Access to this token equals full access to your account, so guard it carefully.
- If you're familiar with Postman, you can test calling Nobitex's main APIs using the [Nobitex Postman collection](https://documenter.getpostman.com/view/5722122/Szmcayjw?version=latest). Note this collection only covers a subset of Nobitex APIs and their usage — this documentation page remains the canonical reference.

Depending on your needs, you may also want to:

- Use the [Login API — Get Token](#16-login-api-auth) if you want to fetch tokens automatically on a recurring basis.


---

## 16. Login API (Auth)

### Recommended way to get a token

The recommended way to get a token is via the user panel: go to your Nobitex profile → Settings page and grab your token there. If you checked "Remember me" at login, this token stays valid for 30 days or until you log out.

Below is how to fetch a token programmatically. This usually isn't necessary — most users should just grab a token from the panel. Only use this API if you understand the risks of storing your password in code, know secure practices for it, are experienced with the Nobitex API, and genuinely need fully automated token retrieval.

We also recommend you build and test your initial integration using a token from the panel, and only automate token retrieval as a final step — since automated login involves extra considerations like 2FA and CAPTCHA that are worth reading carefully first.

### Login — Get Token

**Endpoint:** `POST /auth/login/`
**Rate limit:** 30 requests / 10 minutes

```shell
curl 'https://apiv2.nobitex.ir/auth/login/' \
  -X POST \
  --header "Content-Type: application/json" \
  --header "X-TOTP: <Your TOTP value>" \
  --header "User-Agent: TraderBot/<Your bot version and name>" \
  --data $'{"username":"name@example.com","password":"secret-password-1234","captcha":"api"}'
```

Example response:
```json
{
    "status": "success",
    "key": "db2055f743c1ac8c30d23278a496283b1e2dd46f",
    "device": "AlRyansW"
}
```

This is the only endpoint where you send your username and password — every other endpoint authenticates via token. Tokens issued here expire after **4 hours** by default; you'll need to log in again to get a new one. To get a longer-lived token, send `remember=yes` — that token stays valid for 30 days.

If you're calling this without solving an image CAPTCHA, you must set `captcha: "api"` **and** have 2FA enabled on the account, sending the current TOTP code in the request header. Also set the `User-Agent` header to `TraderBot/XXXXX` (your bot's unique name). See [API Keys / 2FA setup](#3-api-keys-beta) for more on enabling 2FA.

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `username` | string | required | User's email | `name@example.com` |
| `password` | string | required | User's password | `secret-password-1234` |
| `remember` | string | `no` | Issue a long-lived token? | `yes` or `no` |
| `captcha` | string | required | CAPTCHA value | `api` |

**Header parameters**

| Header | Default | Description | Example |
|---|---|---|---|
| `X-TOTP` | required | 2FA code | `123456` |
| `User-Agent` | required | Client identifier | `TraderBot/1.0.0` |

**Notes**

1. The request URL must end with a trailing `/`.
2. Tokens from this endpoint expire after their validity window or upon logout. Default validity is 4 hours; use `remember=yes` for a 30-day token.
3. Login requests must come from an Iranian IP, or you'll get a 429 error — any foreign VPN/VPS will trigger this. When calling from an Iranian IP, you can set `captcha` to `api`.
4. If you run into trouble getting a token this way, fall back to the [panel-based method](#login-api-auth) using the Settings page.

### Login FAQ

**Does Nobitex offer long-lived tokens?**
Tokens can be valid for up to one month. If you check "Remember me" when logging in on the website, the token shown in your profile is valid for a month. Via the API, use the `remember` parameter. Logging out anywhere invalidates the token.

**I signed up with Google — how do I use the API?**
The [recommended approach](#login-api-auth) — getting a token from the panel — works regardless of signup method. If you signed up with Google and want to use automated login, use "Forgot password" to set a password for your account first, then use the API.

**I get a `MissingCaptcha` error at login — what's wrong?**
Per the [recommended approach](#login-api-auth), get your token from Profile → Settings on the website instead. If you're using automated login, re-read the documentation above carefully.

**I get a `MissingOTP` error at login — how do I fix it?**
If 2FA is enabled on your account, you must also send the OTP code via the API, using the `X-TOTP` header. The code can be generated using the standard TOTP algorithm.

### Logout — Revoke Token

**Endpoint:** `POST /auth/logout/`

```shell
curl -X POST 'https://apiv2.nobitex.ir/auth/logout/' \
--header 'Authorization: Token yourTOKENhereHEX0000000000'
```

Example response:
```json
{ "detail": "خروج با موفقیت انجام شد.", "message": "خروج با موفقیت انجام شد." }
```
(Both fields translate to: "Logout completed successfully.")

---

