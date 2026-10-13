# Shared Candle History Implementation Plan

**Goal:** Every exchange candle download contributes complete, timestamped OHLCV to persistent history, and all requests for closed candles reuse that history before downloading missing data.

**Architecture:** Add a shared public-market SQLite database at `TRADER_DATA_DIR/candles.sqlite3`. Key candles by exchange, instrument, canonical timeframe, and the exchange's actual opening timestamp. Integrate the existing `DataFetcher`, `PublicSpotData`, and `NobitexPublicSpot` entry points so their callers automatically share history.

**Tech stack:** Python, SQLite, requests, existing pandas adapters; no additional packages.

**Requirements:** Preserve complete OHLCV, opening timestamps, exclusive closing boundaries, and provider metadata. Reuse data across features and restarts. Keep exchanges, instruments, and timeframes separate. Persist each successful page immediately, including extra closed candles returned by the exchange. Exclude unfinished or malformed candles from reusable history. Preserve existing return formats and cancellation behavior.

## Task 1: Durable shared storage

Files: `candle_store.py`, `tests/test_candle_store.py`.

- [x] Write and run failing behavioral tests for durable reopen, timeframe aliases, exchange separation, immutable duplicates, incomplete/malformed rows, interval coverage, empty-range expiry, calendar months, and concurrency.
- [x] Implement transactional SQLite storage with indexed candle identity, metadata, temporary successful no-data observations, and per-series locks.
- [x] Provide chronological range/latest reads and missing-range calculations based on actual stored timestamps and closing boundaries.
- [x] Run the storage tests and verify the data survives reopening the database.

## Task 2: Bots, backtests, optimization, and training

Files: `data.py`, `tests/test_candle_cache.py`.

- [x] Write and run failing integration tests with fake exchange responses and deterministic clocks.
- [x] Route both historical and latest-candle fetches through the shared store, keeping naive UTC pandas indexes and the existing OHLCV columns.
- [x] Bootstrap absent history using bounded backward pagination, save each page, then append newly closed candles and repair cached interior gaps with bounded requests.
- [x] Extend older history only when the requested count exceeds what is saved; avoid downloading stored ranges.
- [x] Use Binance's supplied closing timestamp and calendar-aware monthly fallback. Derive Nobitex freshness from returned opening times, preserving exchange alignment. Do not assume UTC-aligned Nobitex candles.
- [x] Verify cancellation, stalled pagination, all downloaded extra closed candles, repeated/latest requests, restarts, gaps, and timestamp/volume fidelity.

## Task 3: Portfolio research across all exchanges

Files: `spot_data.py`, `tests/test_spot_candle_cache.py`.

- [x] Write and run failing tests for Binance, KuCoin, Bitfinex, and Nobitex range reuse.
- [x] Use the same canonical store identities in both spot fetchers and preserve existing audit/normalized output formats.
- [x] Fetch only uncovered ranges, save each page immediately, and preserve provider hashes and retrieval timestamps.
- [x] Request Nobitex gaps with `from`/`to` without `countback`, which otherwise overrides the lower bound.
- [x] Verify overlapping ranges, new closures, malformed responses, durable reuse, and compatibility with candles downloaded through `DataFetcher`.

## Task 4: Verification and documentation

Files: `README.md`, this plan.

- [x] Document the database location, cache identity, incremental updates, and short retries for genuinely unavailable periods.
- [x] Run targeted storage and fetcher tests, then the complete Python and JavaScript test suites used by this repository.
- [x] Review the implementation independently for stale latest candles, timezone/monthly boundary errors, duplicate downloads, request bounds, and concurrency failures; fix material findings.
- [x] Record completed work and verification results in this plan.

## Review focus

1. A forming candle must never become an immutable historical record.
2. The last saved candle's existence does not prove freshness; newly closed candles must be fetched.
3. Count-based requests must still repair genuine interior gaps, and range requests must filter cached rows to their requested time bounds.
4. An empty or failed response must not permanently conceal missing history; successful empty observations expire, and errors remain retryable.
5. Historical Tehran alignment and calendar-month duration must not be replaced by a UTC floor or a fixed 30-day month.
6. Concurrent callers must recheck the database after acquiring a series lock, and successful pages must survive later cancellation or failures.

Execution is authorized by the user's request to plan and implement. Storage and spot integration run in parallel; the main agent implements `DataFetcher` and coordinates verification. Existing unrelated working-tree changes are preserved.

## Verification results

Completed on 2026-10-01. All planned storage, fetcher, documentation, and review work is complete.

- `python -m pytest -q -p no:cacheprovider --tb=short --basetemp=.runtime/candle-tests-full-final`: **405 passed** in 44.91 seconds. One existing Starlette/httpx deprecation warning.
- `node --test tests/dashboard_ui.test.cjs tests/model_trainer_ui.test.cjs tests/portfolio_manager_ui.test.cjs`: **70 passed**, zero failures.
- Independent review: **14 regression tests passed**, with no remaining correctness blockers. Proofs cover partial pages, latest-candle freshness, monthly pagination and closure, empty normalization, timeframe aliases, integer HTTP bounds, and historical Tehran daylight-saving transitions.
- `git diff --check` on the implementation, tests, README, and plan: clean.

All HTTP in tests is synthetic; the tests make no live exchange requests or trading calls. Runtime storage is created on demand. The shared candle database is separate from trading/account databases and requires no migration of those databases. Existing unrelated working-tree changes were preserved.
