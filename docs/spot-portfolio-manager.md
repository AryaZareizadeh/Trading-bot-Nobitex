# Adaptive spot Portfolio Manager

## Scope and accounting boundary

The managed portfolio has its own virtual USDT cash and asset quantities in the
per-user `Store` namespace `spot_manager`. It starts at 200 USDT by default and
has no call to the authenticated Nobitex order API. The existing bots, margin
research engine, wallet valuation, portfolio snapshots, cash-flow history, and
manual exchange rebalance settings remain intact. The dashboard navigation now
opens one **Portfolio Manager** tab; the lower panels show that earlier exchange
history separately. No saved portfolio rows were migrated or deleted.

`spot_data.py` handles public source normalization, provenance, UTC completion
times, hashes, and quality audits. `spot_portfolio.py` owns the spot ledger,
allocation policy, drawdown controls, and simulated execution. Both
`spot_research.py` and `spot_paper.py` use that ledger; each supplies its own
market observations. `server.py` exposes authenticated controls through the
same per-user app and store as existing bots. A different future trading
method should provide its own execution and accounting adapter, not reuse a
margin position with leverage set to one.

## Data and executable prices

The candidate universe is BTC, ETH, BNB, XRP, SOL, ZEC, HYPE, ADA, XLM, NEAR,
UNI, LTC, SUI, ENA, and XAUT. USDT is the zero-return reserve. XAUT is a risky
holding. Each asset becomes eligible only when its own valid signal history and
direct Nobitex USDT candle history exist. An absent pair is reported and
excluded; IRT conversion is never inferred. On 2026-09-27 the public Nobitex
order-book response showed direct USDT books and amount precision entries for
all 15 candidates. Its public options response reported a 0.5 USDT minimum;
the local API snapshot documents an older 11 USDT minimum. Paper trading reads
the current public rule at each tick and adds 0.01 USDT to avoid the strict
minimum boundary. Historical replay conservatively assumes 11.01 USDT.

Binance spot is the normal signal source. KuCoin HYPE/USDT and Bitfinex
`tXAUT:UST` supply the longer historical spot series; `UST` is Bitfinex's USDT
quote symbol. Paper first attempts Binance for those two assets and falls back
to the supplement when Binance has fewer than 96 complete hourly bars. The
decision records the source. The research report compares overlapping closes
between each supplement and Binance, and between signals and Nobitex. It does
not assume differences converge. The first public samples returned Binance
and KuCoin hourly bars, four XAUT bars in a five-hour Bitfinex request, and
Nobitex hourly BTC/HYPE/XAUT data. Continuous coverage still requires the full
audit.

Every stored candle identifies exchange, instrument, quote, UTC open and
completion time, retrieval time, schema version and hash. Provider-specific
OHLCV order is mapped explicitly. Duplicate, gapped, incomplete, invalid and
extreme candles are reported; no pre-listing or sparse interval is filled.
Volume changes use each source's own rolling median. Source switches are
recorded rather than spliced into an artificial return series.

Public references: [Binance market data](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md),
[KuCoin current klines](https://www.kucoin.com/docs-new/v2/rest/ua/get-klines),
[Bitfinex candles](https://docs.bitfinex.com/reference/rest-public-candles),
[Nobitex API](https://apidocs.nobitex.ir/). KuCoin's current UTA endpoint differs
from the tested classic spot endpoint used in `spot_data.py`; its schema is
kept explicit. Provider limits and schemas should be rechecked after upgrades.

## Decisions, simulation and risk

The first candidate uses hourly completed spot candles with 1, 4, 24 and 72
hour returns, volatility, a range measure, within-source volume ratio and BTC
context. It trains 4, 24 and 72 hour return and downside-magnitude forecasts.
The current forecast chooses an asset's horizon at decision time; no realized
future return chooses it. Histogram gradient boosting, LightGBM, and
a fixed momentum/volatility baseline use the same chronological folds and
execution assumptions. A forecast magnitude must clear estimated round-trip
fees and slippage before allocation. Scores are not probabilities.

The allocator permits nonnegative weights and any residual USDT cash, including
100%. Initial, configurable policy values are 40% maximum per asset, 50%
turnover per decision, and a 2% no-trade band. These are cautious hypotheses
for a 200 USDT account, not inherited constraints from the old manual target
form. Current risk rules reduce total risky target weight to 25% at 7%
portfolio drawdown. At 10%, they seek USDT and pause new risk for 24 hours;
afterward cautious re-entry is capped at 10% until recovery. A further 2% loss
during that re-entry restarts the cooldown. Gaps and lack of depth can exceed
the target. Missing marks, stale books, missing market precision, and absent
signal history pause new trades. They do not trigger blind liquidation.

The ledger sells before buys and spends one shared virtual cash balance.
It rounds quantities down to the published Nobitex amount step and records
fees, partial estimates, unfilled orders, reasons and residual quantities.
Spot holdings incur no funding or borrowing charges. Marked USDT equity uses
Nobitex mid prices in paper trading; estimated liquidation value uses bids
minus fees. Peak and drawdown use managed equity adjusted for external flows.
No paper deposit/withdrawal control is exposed yet, so the initial 200 USDT
cash flow is the only flow. The old exchange portfolio flow form affects only
the separate exchange history.

Historical decisions occur after a signal candle completes; fills use the
next Nobitex candle open. The liquidity proxy uses only the **previous**
completed Nobitex candle's volume. Backtests assume a 10 bp spread, 10 bp
additional slippage per side, 25 bp fee per side, a maximum of 2.5% of that
previous candle's notional volume, and an 11.01 USDT minimum. These are
estimates: OHLC bars cannot reconstruct order-book depth, queue position,
partial market impact, or an actual limit fill. The stress replay doubles fees,
triples slippage and halves modeled depth. Paper polls public Nobitex books,
records hourly marks, and makes allocation decisions at four-hour boundaries.
Its loop checks every minute; model retraining is an explicit research action.
After restart the paper service is paused until explicitly started again.

## Validation contract

The report fixes 50% training / 15% validation / 15% second validation /
20% untouched test boundaries across the complete UTC timeline. Labels ending
at or beyond a training boundary are excluded. Each model is refit from data
before its fold. The method is selected from the two validation folds, then
refit without using final-test labels. The test is scored once. The report
versions the signal and venue data hashes, a compressed normalized-candle
snapshot, code hash, seed, source audits, costs, rules and windows.

Before calling a result credible enough for extended paper validation, all
of the following must hold: at least 365 days of evaluated source history;
positive net return in both validation folds; final net return exceeding USDT
and the equal-weight basket by at least two percentage points; final maximum
drawdown at most 10%; positive net return under the specified cost/depth
stress; and forecast mean absolute error better than a zero-return forecast
in at least two of three horizons. These are research gates, not a guarantee or
permission to use real money. The default 365-day request may still be
inconclusive if enough assets lack history or distinct market conditions.
Repeated manual tuning after observing the final test requires a **new**
untouched period. Each research run keeps its own report, normalized-candle
snapshot and model artifact; the tab links the current report and prior-run
index. The report includes net P&L, drawdown, fees, turnover,
trades, approximate holding time, USDT time, equity, allocations, decisions,
period and asset summaries, and forecast error separately. The USDT benchmark
earns zero. The basket makes one approximately equal-weight purchase among
assets eligible at its entry and then holds. PriceAI is not directly comparable
because its current simulator trades leveraged long/short margin positions.

### First public-data research run (2026-09-27)

An exploratory 365-day run used BTC, ETH, HYPE and XAUT. All four had direct
Nobitex USDT books. The final untouched period ran from 2026-07-16 to
2026-09-27. The validation-selected LightGBM policy returned **−3.91%** net
(−7.82 USDT on 200 USDT), with **4.46%** maximum drawdown and 72 estimated
fills. Its stressed return was **−2.61%**. The same-period USDT benchmark
returned 0%, the one-time equal basket **+19.77%**, and the momentum policy
**−4.42%**. One of the two validation folds was negative, and forecast MAE
did not beat a zero-return forecast at any of the three horizons. The stated
acceptance gate **failed**. This is evidence against deploying this candidate.

The [summary](../reports/spot-preview-2026-09-27/summary.json),
[full report](../reports/spot-preview-2026-09-27/report.json), normalized
spot-candle snapshot and versioned model artifact are saved under `reports/`.
The sample lacks historical Nobitex depth, so fill costs and capacity remain
assumptions. It covers four of the fifteen candidates; a universe-wide
evaluation remains outstanding. Reported net returns deduct modeled trading
costs; they exclude compute, hosting and data-service operating costs. There
are no managed live-trading returns to report.

## Phased delivery and remaining gates

1. **Public source adapters and audit:** Implemented for Binance, KuCoin,
   Bitfinex and Nobitex spot. A full continuous historical audit must still be
   run in the target deployment environment and inspected for sparse markets.
2. **Shared spot accounting and baseline replay:** Implemented with separate
   cash/holdings, cost-aware next-open replay, equal basket, momentum and USDT.
   Historical depth remains an explicit assumption.
3. **Multi-horizon forecasts and allocation:** Implemented with HGB and a
   LightGBM candidate. If LightGBM is absent in an older local installation,
   the report omits it. Wider parameter search and calibrated
   uncertainty are future experiments, conditioned on baseline evidence.
4. **Walk-forward evaluation and reports:** Implemented with two forward
   validation folds, untouched test, cost stress, data/code hashes, and JSON
   export. A four-asset real-data run failed acceptance; a universe-wide
   evaluation remains to be run and inspected.
5. **Live paper and dashboard:** Implemented with public feeds, persistent
   per-user virtual state, explicit start/stop and the replacement tab. Paper
   fills remain estimates; source-transfer quality requires monitoring.
6. **Future Nobitex live spot adapter:** Intentionally absent. Before it can
   be built, confirm fee tier and precision at execution time, add asset
   reservations shared with existing bots, durable order intents, partial-fill
   reconciliation, timeout recovery, and a separate explicit enable gate.
   No existing bot's real-order route is reused by the paper manager.

Run `python -m pytest tests/test_spot_manager.py tests/test_spot_research_integration.py`
and `node --test tests/dashboard_ui.test.cjs` for focused checks. Tests use
synthetic candles and fake storage; passing tests does not establish an edge.
