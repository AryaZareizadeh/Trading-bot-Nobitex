# Trading Bot Reliability Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task after implementation is requested. Steps use checkbox syntax for tracking.

**Goal:** Resolve the five identified limitations: incomplete dependencies, missing access control, volatile runtime state, simplified backtest economics, and documentation/test gaps.

**Architecture:** Keep the Python/FastAPI backend and existing JavaScript dashboard. Add small modules for authentication, SQLite persistence, and simulated execution/accounting; integrate them into the existing bot and research flows. Deliver each stage as a separately reviewable change.

**Tech Stack:** Existing FastAPI, pandas, TA-Lib, SciPy, requests and JavaScript; Python standard-library SQLite; pytest and an HTTP test client for verification.

**Spec:** The user's request to address the five limitations identified in the preceding repository analysis. This document proposes the design and implementation sequence; it does not authorize production trades or deployment.

## Global constraints and assumptions

- Confirmed deployment target: one operator on a local Windows dashboard now, followed by a private remote server when the project is ready. Keep one application process in both deployments. Multiple-user access is outside this plan.
- Configure bind address, port, data directory and environment-file location rather than relying on the working directory. Keep local HTTP/loopback and remote HTTPS cookie settings explicit.
- Preserve the existing uncommitted changes in strategy.py, bots.json, and optimizer.json.
- Keep Nobitex execution and optional Binance research data distinct and label data sources in results.
- Never use production credentials or orders in automated tests. Tests use temporary files, synthetic candles and a fake exchange.
- Preserve bot IDs and custom parameters during migration. Do not enable real bots automatically as a side effect of migration or server startup.
- Credentials stay outside the database, fixtures, logs, and Git.
- Each behavior-changing stage includes focused regression tests. No frontend framework rewrite or distributed service migration.
- New historical simulation assumptions must not silently change the order policy of existing real bots.

## Sequence and deliverables

| Stage | Limitation addressed | Depends on | Completion evidence |
| --- | --- | --- | --- |
| 1. Reproducible setup and baseline | Dependencies; initial test coverage | None | Fresh Windows installation, imports and offline smoke tests pass |
| 2. Operator authentication | Unprotected API and WebSocket | Stage 1 | Anonymous access blocked; authorized dashboard works |
| 3. Durable state and recovery | In-memory runtime state | Stage 1 | Crash/restart cases preserve ownership and avoid duplicate submission |
| 4. Simulation and accounting | Optimistic backtest assumptions | Stage 1; Stage 3 for live-ledger integration | Hand-calculated results, capital constraints and shared research metrics pass |
| 5. Documentation and release verification | Documentation drift and missing tests | Stages 1–4 | Documented setup and complete offline workflow pass in CI |

Stages 2 and 3 are logically independent. Execute them sequentially by default so each change remains easy to review.

## Stage 1 — Reproducible environment and baseline tests

**Files:** Modify requirements.txt and main.py; create requirements-dev.txt, a Windows-tested dependency lock/constraints file, tests/conftest.py, tests/test_imports.py, tests/test_baseline.py, tests/fakes.py, and .github/workflows/tests.yml.

- [ ] Inventory direct imports and declare NumPy, SciPy and TA-Lib explicitly. Declare Matplotlib wherever plotting is supported; make plotting imports lazy so starting the dashboard does not require plotting dependencies. Include tzdata for the existing Asia/Tehran ZoneInfo use on Windows.
- [ ] Select and verify a supported Python minor version against the actual native wheels, starting with the documented Python 3.13 target. Generate exact dependency constraints from a clean, passing environment; do not guess compatible versions.
- [ ] Add pytest and the HTTP test client to development dependencies. Keep the application dependency manifest separate from test dependencies.
- [ ] Create deterministic OHLCV fixtures spanning warmup, rising/falling prices, gaps and an intrabar liquidation breach. Create a fake exchange that records calls and can return partial fills, timeouts and delayed position visibility.
- [ ] Isolate tests from .env and real JSON configuration. Application creation must accept temporary storage and an injected exchange client; disable background exchange polling in tests. Refactor only the server initialization needed for this seam.
- [ ] Add import smoke tests and baseline behavior tests for all three strategies, parameter propagation, candle filtering, summaries and the optimizer's existing train/test boundary. Record deliberate changes to old results in Stage 4.
- [ ] Give main.py one useful, documented default: launch the loopback dashboard with the same server:app entry point. Keep plotting as a separate documented command rather than leaving a no-op demo.
- [ ] Add Windows CI: install from the constraints file, run pip check, import core modules, then run pytest. Exercise a small Windows multiprocessing optimizer job against a saved fixture, with bounded population and iterations.

**Acceptance:** A clean Windows environment installs without undocumented manual packages. Imports, an offline API startup smoke test, all strategy baselines, and a small optimizer run pass without network access or changes to the user's configuration.

**Verification commands:**

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt -c requirements-lock.txt
python -m pip check
python -m pytest tests/test_imports.py tests/test_baseline.py -q
```

The lock-file name above is the proposed artifact to generate during implementation, not a file currently present.

## Stage 2 — Single-operator authentication

**Files:** Create auth.py and tests/test_auth.py; modify server.py, static/index.html and .env.example.

**Design:** A configured operator password, stored as a salted password hash, unlocks an opaque server-side session. The browser uses an HttpOnly, SameSite=Strict session cookie. Use Secure cookies with HTTPS deployments; local HTTP remains restricted to loopback. Sessions expire, can be revoked, and are invalidated on restart. Do not put authentication tokens in URLs or browser localStorage.

- [ ] Add local password setup and a documented reset procedure. If no password is configured, expose only setup-required information; trading, wallet and credential routes remain locked. No default password or anonymous setup endpoint.
- [ ] Add login, logout and session-status routes. Rate-limit login attempts, compare credentials safely, and return generic authentication failures.
- [ ] Protect every /api route except the explicitly public session endpoints. Protect or disable interactive API documentation. Verify the session before accepting /ws; reject expired sessions and close existing sockets on logout/expiry.
- [ ] Add CSRF protection to state-changing requests and explicit Origin/Host validation for browser requests and WebSocket handshakes. Avoid permissive CORS settings.
- [ ] Add a small login view and a shared fetch wrapper to the current JavaScript. Handle session expiry and WebSocket reconnection consistently.
- [ ] Validate credential updates before mutating the environment; write .env atomically, redact secrets in errors, and return credential-presence metadata only.
- [ ] Test anonymous reads/writes, invalid and expired sessions, logout, cross-origin requests, protected WebSocket connections, and an authenticated bot-management flow.

**Acceptance:** Without a valid session, a client cannot inspect wallet/bot data, change credentials, start a bot, or subscribe to updates. The authorized operator can use the full dashboard. Secrets never appear in responses or captured logs.

**Private-server deployment:** Retain single-operator authentication. Terminate HTTPS at a reverse proxy, bind the application to server loopback, trust only the configured proxy, and restrict access through a private network/VPN or firewall allowlist. Use Secure cookies there. Multi-user accounts and per-user exchange credentials are outside this plan.

## Stage 3 — SQLite state, trade ledger and restart recovery

**Files:** Create storage.py, migrations/001_initial.sql, tests/test_storage.py and tests/test_recovery.py; modify bot_manager.py, bot.py, nobitex_client.py, server.py, backtester.py and optimizer.py.

**Design:** SQLite is the authoritative local store. Use schema versions, foreign keys, transactions, WAL mode and a busy timeout. Keep database connections out of optimizer worker processes; the parent process persists job outcomes. Use one application process, with an explicit guard against two runtimes owning the same trading database.

**Stored records:** Bot configuration and version; last processed candle; strategy state needed to continue simulation; open simulated positions and capital reservations; order intents and exchange order IDs; position ownership by account/environment and exchange position ID; actual fills and charges; trades; activity events; research-job inputs, results and status. Store financial values as decimal strings or scaled integers, and timestamps in UTC.

- [ ] Implement versioned migrations and a one-time import from bots.json, tester.json and optimizer.json. Preserve source files and IDs, make repeated imports idempotent, and surface malformed data rather than silently ignoring it.
- [ ] Persist every accepted configuration change and each completed candle's simulation state transactionally. A repeated candle must not duplicate trades or reset the balance.
- [ ] Persist a unique order intent before any exchange submission. Use a stable clientOrderId where supported, and retain that identity across retries. Record acknowledgement, partial fills, cancellation and final reconciliation separately.
- [ ] Verify the exchange's client-order-ID lookup and deduplication behavior against the local API reference and official documentation during implementation. A network timeout after submission means outcome unknown; reconcile it before retrying. If identity cannot be established, block a fresh submission and surface the unresolved intent.
- [ ] Give each exchange position a single recorded bot owner. On restart, use persisted order/position IDs, then reconcile against exchange truth. Do not assign a position to multiple bots based only on its symbol. Legacy or manual positions remain unassigned until ownership is resolved.
- [ ] Load durable state on startup, reconcile outstanding intents and owned positions, and preserve the existing stopped-by-default startup policy. Persist desired run state for visibility without automatically resuming order placement.
- [ ] Store actual live fills and fees separately from strategy estimates. Preserve position entry time, price and ownership even when the entry candle is older than the rolling 500-candle buffer.
- [ ] On shutdown, cancel and await application tasks, flush committed state, and record unresolved orders. Stopping the process must not silently mean closing an exchange position.
- [ ] Persist tester/optimizer completed results and inputs. Mark jobs interrupted after restart; do not pretend to resume an unfinished Differential Evolution population.
- [ ] Add backup/restore instructions using SQLite's backup facility. A restored database must reconcile with the exchange before trading; stale database restoration alone is not a safe trading rollback.

**Recovery acceptance cases:**

1. Crash before an order is sent: no exchange order exists; the saved intent is resolved safely.
2. Exchange accepts an order but the response is lost: restart finds or flags the original order without blindly submitting another.
3. Partial fill followed by restart: remaining quantity and position size match the exchange.
4. Two bots use the same market: ownership stays unique.
5. A position disappears or closes manually while offline: reconcile once, retaining history.
6. A simulation restarts across a candle boundary: trades, available capital and balance are unchanged by replay.
7. A write fails: return a visible error and do not claim configuration or trading intent was saved.

**Acceptance:** These cases pass with a fake exchange and temporary database. Migration can be repeated safely; database backup/restore is exercised. No production order is needed for verification.

## Stage 4 — Shared simulation, costs and risk accounting

**Files:** Create execution_model.py, accounting.py, tests/test_execution_model.py and tests/test_research_consistency.py; modify config.py, strategy.py, bot.py, backtester.py, optimizer.py, server.py and static/index.html.

**Design:** Separate signal decisions from simulated fills and accounting. All historical evaluation entry points use the same model and result schema. Persist an execution-model version and cost assumptions with every result so revised results are identifiable. Real-mode accounting uses exchange fills and reported charges, not simulated fills.

- [ ] Add a cost configuration with explicit entry/exit fee rates, adverse slippage, and an exchange-specific borrowing/delegation charge policy. Read applicable rates and billing semantics from verified exchange metadata/documentation during implementation. Do not assume a perpetual-futures funding model applies to Nobitex margin trading.
- [ ] Preserve gross P&L and expose fees, borrowing charges and net P&L separately. For a position with signed direction s, filled quantity q and fill prices entry/exit, gross P&L is s*q*(exit-entry); net P&L deducts incurred costs. Define the conversion of asset-denominated costs to the reporting currency.
- [ ] Evaluate signals using information available at a candle close. Default historical fills to the following candle's open plus adverse slippage. Signals on the final candle remain unfilled. Keep the old close-fill behavior only as a clearly labeled comparison model.
- [ ] Use candle low for long liquidation breaches and high for short breaches. Define gap handling and conservative ordering when multiple events occur in one candle; never use an entry candle's pre-entry price range. Flag that OHLCV cannot reconstruct exact intrabar order or exchange mark-price liquidation.
- [ ] Freeze quantity and reserved margin at entry, debit entry costs immediately, accrue holding charges, and release margin at exit. Reject simulated entries that lack available capital. Separate realized balance from unrealized equity and compute drawdown from marked equity including open positions.
- [ ] Address the current multi-position strategy versus single-position real-bot difference explicitly: provide a Nobitex-compatible research execution mode matching the real bot's net-position policy, and label unrestricted multi-position research results separately. Do not silently change strategy signals or existing live behavior.
- [ ] Fetch explicit indicator warmup history and set an evaluation boundary. Remove unconditional first-trade dropping only after warmup and boundary tests pass. At a train/test boundary, warm up indicators on training data but start evaluation flat, with no training trades included.
- [ ] Pass the same execution/cost configuration through single-bot backtests, Tester, optimizer workers and held-out evaluation. Optimize net results and show gross/net comparisons. Persist data source, candle window, data hash, random seed, parameters, model version and cost settings with results.
- [ ] Add hand-calculated long/short tests for entry and exit costs, slippage direction, held-position charges, entry-time quantity, capital exhaustion, intrabar breaches, gaps and final-candle signals. Add a consistency test asserting that the three research entry points return the same metrics for the same data and model.

**Concrete accounting oracle:** In an artificial fixture, quantity 2, long entry 100, exit 110, and a test fee of 0.1% on each fill produce gross P&L 20, fees 0.42 and net P&L 19.58 before holding costs. These are test assumptions, not claimed Nobitex rates. A short entry at 110 and exit at 100 gives the same values under those assumptions.

**Acceptance:** Exact small-fixture accounting passes; an intrabar breach with a recovered closing price is detected; position reservations prevent overspending; held-out data cannot affect parameter selection; all research paths agree. Existing stored results remain identifiable as using the earlier model.

## Stage 5 — Documentation, regression suite and release verification

**Files:** Create README.md, docs/architecture.md, docs/backtesting.md, docs/operations.md and tests/test_dashboard_flow.py; update .env.example, main.py and .github/workflows/tests.yml. Update the local CLAUDE.md guidance to match tracked documentation.

- [ ] Document the actual Python/FastAPI/plain-JavaScript architecture and Nobitex execution versus Binance research data. Explain each strategy and retain the ExtremeReversal beta label.
- [ ] Document clean installation, password setup, dashboard startup, credential configuration, simulation, optimization, train/test evaluation, database backup and restart reconciliation. List supported Python/platform versions established by Stage 1 verification.
- [ ] Explain cost assumptions, execution timing, warmup, gross/net P&L, liquidation limitations, model versions and what optimizer scores measure. Do not present backtest returns as evidence of future profitability.
- [ ] Document stop behavior, unresolved-order handling and how ownership is resolved for legacy positions. Explicitly distinguish stopping a bot from closing its exchange position.
- [ ] Add an offline end-to-end flow: log in, create a simulation bot, process candles, restart, inspect retained history, run Tester, run a small optimization with held-out evaluation, and verify chart/API results agree. Drive the browser against the fake exchange so no real credentials are required.
- [ ] Run all focused suites in CI. Cover malformed market data, retry exhaustion, partial fills, authentication expiry, restart recovery and optimizer cancellation. Prefer failure-oriented tests over tests that merely repeat implementation details.
- [ ] Verify README commands in a clean Windows environment. Confirm that no tests touch the repository's real JSON state, .env or exchange account.
- [ ] Add the later private-server runbook: choose and test the target server OS/runtime, install verified dependencies, configure HTTPS and private access, run one application process under an OS service manager, configure data/log paths and backups, and verify graceful shutdown and process restart. Keep development reload disabled. Add CI for the chosen server platform before moving live operation there.
- [ ] Rehearse moving a database backup and configuration to an isolated private-server installation using a fake exchange. During the eventual live cutover, stop the Windows trading runtime before activating the server runtime, transfer secrets securely, reconcile exchange state, and resume bots explicitly. Never operate both deployments against the account during cutover. Use the same one-active-runtime procedure for rollback.

**Acceptance:** A new user can install and operate the app from README alone. The documented offline workflow passes after a restart, and CI catches regressions in each of the five original limitation areas.

## Rollout and review boundaries

1. Deliver environment/tests first; record baseline behavior before accounting changes.
2. Deliver authentication and update the UI in the same change.
3. Back up JSON configuration, migrate to SQLite, and prove recovery using a fake exchange before migrating the operator's runtime.
4. Introduce revised simulation results with explicit version labels; re-evaluate saved optimized parameters without overwriting or deploying them automatically.
5. Complete documentation and the offline integration suite. A separately authorized exchange integration check may follow, but is not required to prove the deterministic behaviors in this plan.

For every stage: run its focused tests, inspect the diff, then make a scoped commit when implementation is underway. Stop expanding tests once acceptance passes unless a new failure or concern justifies it.

## Plan review

- Dependencies: explicit direct imports, verified native packages, lock file and clean Windows install.
- Authentication: REST, WebSocket, session lifecycle, credential updates and browser integration.
- Persistence: migration, simulation continuation, unique ownership, uncertain submission, interrupted jobs and backups.
- Backtest realism: costs, timing, intrabar checks, capital reservation, execution-policy alignment, warmup and consistent optimization.
- Documentation/testing: runnable entry point, tracked documentation, isolated fixtures, meaningful regression cases and offline end-to-end CI.

The largest implementation risks are order reconciliation after an uncertain submission and changing historical results when execution/accounting becomes more realistic. The planned identity checks, labeled model versions and deterministic fixtures directly target those risks.
