# Nobitex API Reference — Index

This directory holds the Nobitex API reference, split into 10 topic files so an agent (or person) can find what it needs by reading **one small file** instead of the full ~4,000-line document.

**Base REST host:** `https://apiv2.nobitex.ir` (a few legacy endpoints use `https://api.nobitex.ir`)
**WebSocket host:** `wss://ws.nobitex.ir/connection/websocket`

## How to use this index (for Claude Code / any agent)

1. **Grep this file first** for the endpoint path, error code, or keyword you're looking for (e.g. `grep -i "stop" INDEX.md`, `grep "margin/orders" INDEX.md`, `grep -i "DuplicateOrder" INDEX.md`).
2. That tells you which single file (≤671 lines) contains the answer. Read **only that file** — never the whole reference unless you're doing a full audit.
3. If your query is conceptual rather than a literal term (e.g. "how do I cancel everything at once"), check the **Concept lookup** table near the bottom first, then jump to the named file.
4. `endpoints.json` has the same endpoint table in machine-readable form, if you'd rather `jq`/`grep` it from a script than parse markdown.
5. Every endpoint inside each file is documented with its own `**Endpoint:**` line, request params, example request/response, and error codes — so once you're in the right file, a plain-text search (`grep`) for the path or error code will land you on the exact section.

## Files at a glance

| File | Lines | Covers |
|---|---|---|
| `01-getting-started.md` | 335 | Intro, auth & tokens, API keys (create/list/delete/update, permissions, signing), quickstart, login/logout endpoints |
| `02-market-data.md` | 255 | Public market data: order book, depth chart, trades list, market stats, OHLC candles |
| `03-user-data.md` | 483 | Profile, deposit addresses, bank cards/accounts, account limitations, wallets (list/balance), wallet transactions, deposits list, favorite markets |
| `04-spot-trading.md` | 548 | Spot orders: place (limit/market/stop/OCO), status, list, update status, bulk-cancel, list your trades, all order error codes |
| `05-margin-trading.md` | 671 | Margin markets, liquidity pools, margin wallet transfer, delegation limits, place margin order, list/get/close positions, edit collateral |
| `06-withdrawals.md` | 444 | Crypto withdrawal (submit/confirm/details), Rial withdrawal (submit/cancel/details), list all withdrawals |
| `07-websocket.md` | 506 | Centrifugo connection/auth, WS token endpoint, all public + private channels (order book, candles, trades, market stats, your orders, your trades) |
| `08-account-management.md` | 386 | Address book (CRUD + whitelist mode), security (login history, emergency cancel, anti-phishing code), referral program |
| `09-portfolio-and-other.md` | 238 | Portfolio P&L (weekly/monthly), system options endpoint (`/v2/options` — precision tables, withdraw limits, active currencies) |
| `10-reference-conventions.md` | 160 | Cross-cutting conventions: success/error response shape, HTTP status codes, pagination, date filters, monetary value handling, 2FA header, rate limits (incl. shared order limit & failed-auth limit), Pro mode, FAQ, valid market/currency/network symbol lists |

## Endpoint lookup (generated from the files — see `endpoints.json` for the machine-readable version)

| Method & Path / Channel | What it does | File |
|---|---|---|
| `POST /apikeys/create` | Create an API Key | `01-getting-started.md` |
| `GET /apikeys/list` | List API Keys | `01-getting-started.md` |
| `POST /apikeys/delete/<public_key>` | Delete an API Key | `01-getting-started.md` |
| `POST /apikeys/update/<public_key>` | Update an API Key | `01-getting-started.md` |
| `POST /auth/login/` | Login — Get Token | `01-getting-started.md` |
| `POST /auth/logout/` | Logout — Revoke Token | `01-getting-started.md` |
| `GET /v3/orderbook/SYMBOL` | Order Book (v3) | `02-market-data.md` |
| `GET /v2/depth/SYMBOL` | Depth Chart (Beta) | `02-market-data.md` |
| `GET /v2/trades/SYMBOL` | Trades List (public) | `02-market-data.md` |
| `GET /market/stats` | Nobitex Market Stats | `02-market-data.md` |
| `GET /market/udf/history` | Nobitex OHLC Stats | `02-market-data.md` |
| `GET /users/profile` | Profile | `03-user-data.md` |
| `POST /users/wallets/generate-address` | Generate Blockchain Deposit Address | `03-user-data.md` |
| `POST /users/cards-add` | Add Bank Card | `03-user-data.md` |
| `POST /users/accounts-add` | Add Bank Account | `03-user-data.md` |
| `POST /users/limitations` | User Limitations | `03-user-data.md` |
| `GET /users/wallets/list` | Wallets — List | `03-user-data.md` |
| `GET /v2/wallets` | Wallets — List (Selective) | `03-user-data.md` |
| `POST /users/wallets/balance` | Balance | `03-user-data.md` |
| `GET /users/wallets/transactions/list` | Wallet Transactions — List | `03-user-data.md` |
| `GET /users/transactions-history` | Transaction History (Selective) | `03-user-data.md` |
| `GET /users/wallets/deposits/list` | Deposits — List | `03-user-data.md` |
| `GET/POST/DELETE /users/markets/favorite` | Favorite Markets (get/add/remove) | `03-user-data.md` |
| `POST /market/orders/add` | Place a New Order (limit/market/stop/OCO) | `04-spot-trading.md` |
| `POST /market/orders/status` | Order Status | `04-spot-trading.md` |
| `GET /market/orders/list` | List Your Orders | `04-spot-trading.md` |
| `POST /market/orders/update-status` | Update Order Status (cancel/activate) | `04-spot-trading.md` |
| `POST /market/orders/cancel-old` | Bulk-Cancel Orders | `04-spot-trading.md` |
| `GET /market/trades/list` | List Your Trades | `04-spot-trading.md` |
| `GET /margin/markets/list` | List Margin Markets | `05-margin-trading.md` |
| `GET /liquidity-pools/list` | List Active Liquidity Pools | `05-margin-trading.md` |
| `POST /wallets/transfer` | Transfer Funds to/from the Margin Wallet | `05-margin-trading.md` |
| `GET /margin/v2/delegation-limit` | Get Margin Delegation Limits | `05-margin-trading.md` |
| `POST /margin/orders/add` | Place a Margin Order (limit/market/stop/OCO) | `05-margin-trading.md` |
| `GET /positions/list` | List Positions | `05-margin-trading.md` |
| `GET /positions/:positionId:/status` | Get a Single Position | `05-margin-trading.md` |
| `POST /positions/:positionId:/close` | Close a Position | `05-margin-trading.md` |
| `POST /positions/:positionId:/edit-collateral` | Edit Collateral on an Open Position | `05-margin-trading.md` |
| `POST /users/wallets/withdraw` | Submit a Crypto Withdrawal Request | `06-withdrawals.md` |
| `POST /users/wallets/withdraw-confirm` | Confirm a Crypto Withdrawal Request | `06-withdrawals.md` |
| `GET /withdraws/<WITHDRAW>` | Get Crypto Withdrawal Details | `06-withdrawals.md` |
| `POST /cobank/withdraw` | Submit a Rial Withdrawal Request | `06-withdrawals.md` |
| `POST /cobank/withdraw/<id>/cancel` | Cancel a Rial Withdrawal Request | `06-withdrawals.md` |
| `GET /cobank/withdraw/<id>` | Get Rial Withdrawal Details | `06-withdrawals.md` |
| `GET /users/wallets/withdraws/list` | List All Withdrawals (crypto + Rial) | `06-withdrawals.md` |
| `GET /auth/ws/token` | Get a WebSocket Token | `07-websocket.md` |
| `public:orderbook-*` (channel) | Order Book Channel | `07-websocket.md` |
| `public:candle-{symbol}-{resolution}` (channel) | OHLC/Candle Channel | `07-websocket.md` |
| `public:trades-{symbol}` (channel) | Public Trades Channel | `07-websocket.md` |
| `public:market-stats-{symbol}` / `-all` (channel) | Market Stats Channel | `07-websocket.md` |
| `private:orders#{authParam}` (channel) | Private Channel: Your Orders | `07-websocket.md` |
| `private:trades#{authParam}` (channel) | Private Channel: Your Trades | `07-websocket.md` |
| `GET/POST /address_book` | Address Book (list / add new address) | `08-account-management.md` |
| `DELETE /address_book/<address_id>/delete` | Delete an Address | `08-account-management.md` |
| `POST /address_book/whitelist/activate` | Enable Secure Withdrawal | `08-account-management.md` |
| `POST /address_book/whitelist/deactivate` | Disable Secure Withdrawal | `08-account-management.md` |
| `GET /users/login-attempts` | Login History | `08-account-management.md` |
| `GET /security/emergency-cancel/activate` | Activate Emergency Withdrawal Cancellation | `08-account-management.md` |
| `POST /security/anti-phishing` | Set Anti-Phishing Code | `08-account-management.md` |
| `GET /security/anti-phishing` | Get Anti-Phishing Code | `08-account-management.md` |
| `POST /users/referral/links-list` | List Invite Codes | `08-account-management.md` |
| `POST /users/referral/links-add` | Create Invite Code | `08-account-management.md` |
| `POST /users/referral/referral-status` | Referral Status | `08-account-management.md` |
| `POST /users/referral/set-referrer` | Set Referrer | `08-account-management.md` |
| `POST /users/portfolio/last-week-daily-profit` | Daily P&L — Last Week | `09-portfolio-and-other.md` |
| `POST /users/portfolio/last-week-daily-total-profit` | Cumulative Daily P&L — Last Week | `09-portfolio-and-other.md` |
| `POST /users/portfolio/last-month-total-profit` | Total P&L — Last Month | `09-portfolio-and-other.md` |
| `GET /v2/options` | System Options (precision/limits/active currencies) | `09-portfolio-and-other.md` |

*(`POST /v2/otp/request` is referenced inline wherever an OTP is needed — see `01-getting-started.md`, `06-withdrawals.md`, and `08-account-management.md`.)*

## Concept lookup (for fuzzy / non-literal queries)

| If you're looking for... | Go to |
|---|---|
| Auth header format, token lifetime, getting a token | `01-getting-started.md` |
| API key permissions (READ/TRADE/WITHDRAW), request signing (Ed25519) | `01-getting-started.md` |
| Stop-loss orders, OCO orders (spot) | `04-spot-trading.md` |
| Stop-loss orders, OCO orders (margin/leverage) | `05-margin-trading.md` |
| Leverage, liquidation price, margin ratio, collateral | `05-margin-trading.md` |
| Order error codes (`BadPrice`, `SmallOrder`, `DuplicateOrder`, `OverValueOrder`, etc.) | `04-spot-trading.md` (spot) / `05-margin-trading.md` (margin) |
| 2FA / OTP / `X-TOTP` header | wherever the specific action is (`01-getting-started.md` for login, `06-withdrawals.md` for withdraw confirm, `08-account-management.md` for anti-phishing/whitelist) |
| Withdrawal error codes, withdrawal limits | `06-withdrawals.md` |
| WebSocket ping/pong, Fossil delta compression, Centrifugo SDKs | `07-websocket.md` |
| `websocketAuthParam` (needed for private channels) | comes from `03-user-data.md` (Profile response); used in `07-websocket.md` |
| Rate limits (general policy, shared order limit, failed-auth limit) | `10-reference-conventions.md` |
| Pagination / date-range filtering conventions | `10-reference-conventions.md` |
| Monetary value precision / why to send strings not floats | `10-reference-conventions.md` |
| HTTP status code meanings | `10-reference-conventions.md` |
| "Pro mode" (`pro=yes`) | `10-reference-conventions.md` |
| Valid market symbols / currency symbols / network symbols / tag-required networks | `10-reference-conventions.md` (snapshot only — see warning there about calling `GET /v2/options` for live data) |
| Per-market min order size, amount/price precision, withdraw fees | `09-portfolio-and-other.md` (`GET /v2/options`) |

## Integrity note

These 10 files are a verified, byte-for-byte-complete split of the original consolidated reference — nothing was dropped or duplicated in the split (every line of the source was accounted for and the split was diffed against the original to confirm this). If the source reference is ever regenerated/updated, this index and the file split should be regenerated from it too, since the index is only correct as long as it matches the files.
