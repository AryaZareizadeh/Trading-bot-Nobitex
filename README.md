# Nobitex trading dashboard

A Python dashboard for technical-analysis strategies, historical
research, simulated trading, and Nobitex margin execution. The interface is plain
JavaScript served by FastAPI. Binance supplies optional research candles; real
orders go to Nobitex.

The Docker deployment supports invitation-only accounts. Each user has a username,
password, confirmed email address, private bots and research, and separate encrypted
Nobitex credentials. Recovery codes are emailed. One Python process serves all users;
it does not start trading bots automatically after a restart.

## Docker setup

Docker Compose is the supported multi-user deployment path. From this directory:

```powershell
Copy-Item docker.env.example docker.env
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Put the generated value in `TRADER_MASTER_KEY` in `docker.env`. Set a long random
`TRADER_INVITE_CODE` and fill in `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, and `SMTP_FROM`. The mail server must support TLS. Then run:

```powershell
docker compose up --build -d
docker compose ps
```

After updating the source, back up `trader_data` and `docker.env` as described
under [operations](docs/operations.md), then run
`docker compose up --build -d dashboard`. The image copies the new Portfolio tab and SQLite migration; the
existing named volume keeps each user's history. Open **Portfolio** after signing
in to take the first complete snapshot. If real bots were running, inspect their
saved positions and unresolved orders before restarting them.

Open [the local dashboard](http://127.0.0.1:8011), create an account with the
invitation code, confirm the emailed code, and sign in. Other users can register
the same way. Password recovery sends an eight-digit one-time code to the verified
email address. The account database and per-user trading databases live in the
`trader_data` Docker volume. Back up that volume and `docker.env` together;
losing `TRADER_MASTER_KEY` makes saved exchange credentials unreadable.

The Compose port is bound to host loopback. For remote access, put an HTTPS reverse
proxy in front of it, set `DASHBOARD_ALLOWED_HOSTS` to the exact public hostname,
and set `DASHBOARD_SECURE_COOKIE=true`. Preserve the browser's Host header and
WebSocket upgrades. Run one application container against a data volume.

To import the previous single-operator database, stop the old runtime and use
`python main.py backup --output PATH` to make a consistent source copy. Register
and verify its owner's new account, but do not sign in yet. Stop the Compose
dashboard, mount that backup read-only into a one-off container, and run
`python main.py import-legacy --username USERNAME --source PATH` there with the
same `TRADER_MASTER_KEY` and old exchange credentials available in the container
environment. The command copies the database into that user's directory and
retains the source. Start Compose again, then inspect saved positions and
unresolved orders before starting any real bot. See
[operations](docs/operations.md) for recovery details.

## Windows setup

Use 64-bit Python **3.13**. The constraints pin versions with Windows wheels for
NumPy, SciPy and TA-Lib. Run these commands in the repository directory using
PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c requirements-lock.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe main.py setup-password
.\.venv\Scripts\python.exe main.py
```

Open [the local dashboard](http://127.0.0.1:8011). This is the legacy single-user mode.
Password setup prompts privately
for a password of at least 12 characters and stores a salted hash in `.env`.
Run the same command while the application is stopped to reset it. Restart after
changing the password. No password is preconfigured.

The launcher runs one process, without automatic reload. `--host` and `--port`
override the default loopback address and port. Use the remote deployment
instructions before exposing the service beyond your own computer.

## First use

1. Log in. Use the credential settings only when account features are needed.
   Credentials stay in the selected environment file; API responses show presence
   rather than secret values.
2. Create a bot with mode **simulation**, choose its market, timeframe, strategy,
   capital and leverage, then start it explicitly.
   Selecting a market checks the signed-in account's current Nobitex delegation
   tiers for long and short trades. The market list alone does not establish your
   usable leverage. The bot uses whole-number leverage up to the lower available
   side limit; Agentic AI has a separate 5× strategy cap. Margin-wallet USDT and
   the remaining delegation amount can further restrict an order when placed.
3. Inspect its candles, positions and trade history. Stopped bots remain stopped
   after restarting the application.
4. Open **Research assumptions** to load your Nobitex account fee and the
   selected market's daily renewal rate. Set slippage and capital allocation
   yourself, then save. Costs default to zero until configured.
5. Use **Optimizer** to search parameters. Choose **Optimizer Tester** for a
   held-out test on later candles. Review gross P&L, costs and net results
   before copying parameters to a bot.

Historical data and account functions need network access. The regression suite
uses synthetic data and fake exchanges and does not need credentials.

## Strategies

| Name | Approach |
| --- | --- |
| AdvRSI | Stochastic RSI extreme zones with RSI confirmation and configurable exits |
| TrendMomentum | EMA trend filter with MACD and Stochastic RSI momentum |
| ExtremeReversal | Extreme-reading reversals; beta/unfinished strategy |
| PriceAI | Price and volume model that forecasts four-candle returns and makes simulated enter, hold and exit decisions |

These strategies use deterministic rules. Differential Evolution searches their
numeric parameters; it does not train a machine-learning model.

### PriceAI first milestone

PriceAI runs only in **simulation** on 1-hour or 4-hour candles. Load your
Nobitex fee and market rate, then set positive entry fee, exit fee and slippage
assumptions in **Research assumptions** before
creating it. Select **Price AI (Simulation)** when creating a bot, then use
**Start** to review its held-out historical result before starting. Its latest
action and reason appear on the bot card; `GET /api/bots/{id}` returns recent
decisions and training metadata.

The model uses completed OHLCV candles and forecasts the next four candles'
return. It trains on history before the bot starts, remains fixed as new candles
arrive, and reconstructs the same model from saved candles after a restart.
The historical check trains on the first 70% of fetched candles and evaluates
on the later 30%, with signals filled at the next candle open and explicit
costs. A 10% leveraged loss threshold and 18-candle maximum holding period
also request an exit at the next open; gaps can produce larger losses. The
model and its return threshold are experimental and make no profit claim.
This milestone does not use news or place real orders.

### Portfolio tab

The **Portfolio** tab values spot and margin wallet balances in USDT and adds
open-position unrealized P&L. Blocked margin collateral remains inside wallet
balances, so it is not added a second time. If a held currency or position quote
has no USDT conversion price, the tab marks its valuation incomplete and omits
that snapshot from performance calculations. These are estimates based on current
exchange responses, not an exchange statement.

Snapshots are saved in the user's SQLite database when the tab is opened or
refreshed, and periodically while an online dashboard is running. Enter every
**external** deposit and withdrawal with its USDT value at the time it occurred.
Do not enter transfers between spot and margin wallets. Net P&L removes these
flows, and the return chains Modified Dietz approximations between
snapshots. Performance cannot be trusted if external flows are missing. Equity
history begins with the first complete snapshot and cannot reconstruct earlier
balances automatically.

Allocation targets apply to spot holdings only. They cap each asset at 35% and
reserve at least 20% as USDT. Indicative rebalance amounts are limited by cash,
10% spot turnover and 25% of visible top-five order-book depth. The estimate
uses the greater of saved entry fee/slippage assumptions or a 0.2% floor for
each. No portfolio endpoint places orders.

### Nobitex research assumption auto-fill

The dashboard reads the selected market's current rates when **Research
assumptions** opens or when you select another market. It copies the account's
`feeUsdt` or `fee` percentage from [the documented profile API](https://github.com/nobitex/docs-api/blob/master/source/includes/_user_data.md)
to both entry and exit fee fields. It copies `positionFeeRate` from
[the margin-market API](https://github.com/nobitex/docs-api/blob/master/source/includes/_position.md)
to the daily holding field, and fills the documented 1.1 minimum commitment
ratio. The holding model is an approximation: Nobitex says renewal charges can
vary with delegated value, while this simulator charges a fraction of entry
notional. The profile fee is used as a trading-fee scenario for margin fills;
the final charge on an actual order can differ. These are current account and
market rates, not historical rates.

Nobitex does not publish a fixed slippage value or choose what fraction of your
capital to allocate, so those fields remain editable. Position mode and fill
timing are simulator choices. Auto-fill never silently saves a setting; if the
API is unavailable, saved values remain in the form. A saved scenario applies
globally to future runs, so select the intended market before saving.

## Storage and configuration

The default runtime directory is `.runtime` next to the source. SQLite stores bot
configuration and recoverable runtime state. Existing `bots.json`, `tester.json`
and `optimizer.json` are migration inputs; keep originals as backups.

| Setting | Purpose |
| --- | --- |
| `TRADER_DATA_DIR` | Absolute directory for database and runtime lock |
| `TRADER_ENV_FILE` | Absolute environment-file path; defaults to repository `.env` |
| `TRADER_HOST`, `TRADER_PORT` | Launcher bind address and port; default `127.0.0.1:8011` |
| `DASHBOARD_PASSWORD_HASH` | Legacy single-user mode; created by `main.py setup-password` |
| `TRADER_MULTIUSER` | Enable invitation-only multi-user mode; set by Compose |
| `TRADER_MASTER_KEY` | Fernet key protecting each user's exchange credentials |
| `TRADER_INVITE_CODE` | Shared registration invitation code |
| `SMTP_*` | TLS email delivery for confirmation and recovery codes |
| `DASHBOARD_SECURE_COOKIE` | Enable for HTTPS private-server deployments |
| `DASHBOARD_ALLOWED_HOSTS` | Comma-separated permitted hostnames |
| `DASHBOARD_SESSION_SECONDS` | Session lifetime; default 43200 seconds |
| `OPTIMIZER_MAX_CORES_PER_JOB` | Optional cap on workers per optimizer job; unset offers all logical CPUs visible to the runtime |

Keep `.env`, runtime files, backups and exchange credentials outside Git. See
[operations](docs/operations.md) for recovery and private-server migration.

## Offline verification

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -c requirements-lock.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use temporary storage and block HTTP network access. Windows CI installs
the same constraints, checks dependencies, and runs the regression suite. Linux
CI covers the proposed future private-server target; require it to pass before
moving the runtime.

Optional desktop parameter charts use a separate dependency:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-plotting.txt -c requirements-lock.txt
.\.venv\Scripts\python.exe plot_bot_params.py
```

That legacy chart reads the original `bots.json`; it is not a database export.
Matplotlib is unnecessary for dashboard startup.

## Portfolio Manager research and paper trading

The **Portfolio Manager** tab contains a separate virtual spot portfolio. Its
initial balance defaults to 200 USDT; it never uses exchange credentials or
submits real orders. Select approved assets, run public-data research, then
explicitly start paper trading. The exchange wallet valuation and existing
cash-flow history remain in the lower part of the same tab. The manager's
virtual cash, holdings, and P&L do not include those wallet balances.

The research runner uses Binance spot signals, KuCoin HYPE and Bitfinex XAUT
historical spot supplements, and Nobitex USDT spot prices. It reports source
gaps, two chronological validation folds, an untouched test, cost stress,
forecast error, and USDT / equal basket / momentum comparisons. The report is
available through the tab's **Inspect full report** link. Historical fills are
estimates from candle data; this is not evidence of a profitable strategy.

See [the architecture, validation criteria, and phased plan](docs/spot-portfolio-manager.md)
for assumptions, current limits, and the separate live trading gate.

## Further reading

- [Architecture](docs/architecture.md)
- [Backtest execution and accounting](docs/backtesting.md)
- [Operations, backup and private-server migration](docs/operations.md)
