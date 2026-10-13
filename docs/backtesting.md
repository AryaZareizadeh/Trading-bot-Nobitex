# Backtest execution and accounting

The Research assumptions panel can auto-fill current account trading fees and
market renewal rates from Nobitex's documented profile and margin-market APIs.
Slippage, capital allocation, position mode and fill timing are research inputs
because the API does not supply fixed values for them. The market rate and the
simulator's entry-notional holding charge are an approximation, and current
rates do not reconstruct historical fee schedules.

## Model and inputs

The shared historical model is `ohlc-net-v1`. Results retain execution settings,
model version, candle data hash, data source and evaluation window. Revised
results should be compared only after checking these assumptions. Older results
without model metadata belong to the earlier calculation method.

Optimizer jobs retain their random seed (generated if omitted), input settings
and results. Candidate comparisons use a fixed evaluation boundary after enough
warmup for the search bounds. The train/test split applies to evaluation candles,
not the additional warmup history.

Research assumption controls cover entry/exit fees, adverse slippage, holding
charges, reserved margin and position mode. **Default costs are zero and mean
unconfigured.** No current exchange tariff is bundled. Enter a deliberate cost
scenario appropriate to the account and market before interpreting net returns.

| Setting | Unit / behavior |
| --- | --- |
| `entry_fee_rate`, `exit_fee_rate` | Fractions of each fill's quote notional; 0.001 means 0.1% |
| `slippage_bps` | Basis points adverse to the trade on each fill; 10 bps means 0.1% |
| `holding_policy` | `none` or `entry_notional_daily` |
| `holding_rate_per_day` | Fraction charged pro rata by elapsed days on entry quote notional |
| `margin_fraction` | Fraction of starting credit reserved per intended position, in (0, 1] |
| `position_mode` | `net` allows one position, `multi` permits capital-constrained multiple positions |
| `fill_timing` | `next_open` by default; `legacy_close` is an explicit comparison scenario |
| `liquidation_ratio` | Commitment-ratio threshold used by the simplified liquidation model |
| `max_hold_days` | Nobitex margin-order lifetime, refreshed from its [help article](https://nobitx.com/help/discover/position/how-long-can-i-extend-my-commitment-position/) when a research run starts |

Nobitex currently publishes a 60-day limit from margin **order creation**, even
if the order fills later. The public market API does not expose a lifetime field.
The app checks the official help article when the value is needed, at most once
every six hours, and retries after ten
minutes if unavailable. It retains the last verified value; a fresh process
falls back to the documented 60 days. The Nobitex assumptions response marks
an unverified fallback. Research jobs keep a snapshot of the value used for
that run, so results remain comparable. In the OHLC simulator, a signal fills
at the next open, which also stands in for order creation; unfilled order wait
time cannot be reconstructed from candles. A position reaching the limit exits
at the next available candle open. The real bot uses each exchange position's
`expirationDate` and tries to close an hour before it; date-only expiries are
interpreted conservatively at the start of the date in Tehran.

The daily holding scenario is a sensitivity assumption, not a claim about
Nobitex billing. It does not reproduce asset-denominated charges, hourly billing
steps, delegation renewal or account-specific discounts.

## Timing and warmup

A strategy observes a candle close. The default execution fills its decision at
the following candle's open, adjusted for slippage. A signal on the final candle
remains unfilled; open positions are marked to the last close and are not forced
to close. `legacy_close` changes fill timing only; it does not recreate every
behavior of the old engine.

Indicators receive history preceding the explicit evaluation boundary. No
unconditional first trade is discarded. A held-out evaluation begins flat at its
boundary, using earlier history only to warm indicators. Training trades and
open training positions are excluded from held-out results. Optimization must
select parameters using training candles only.

## Cash, margin and P&L

Quantity is fixed when a position opens. Margin is reserved and cannot fund
another position. Entry fees are debited immediately; holding charges accrue
while a position remains open; margin is released when it closes. Entry size
allows for fees and available cash. Additional positions are rejected when the
required margin is unavailable.

For direction sign `s` (+1 long, -1 short), quantity `q`, entry `a` and exit `b`:

```text
gross P&L = s × q × (b − a)
fees = q × a × entry_fee_rate + q × b × exit_fee_rate
net P&L = gross P&L − fees − holding charges
equity = cash balance + unrealized price P&L
available capital = cash balance − reserved margin
```

For quantity 2, long entry 100, exit 110 and 0.1% fees on both fills, gross P&L
is 20, fees are 0.42 and net P&L is 19.58 before holding charges. These are test
inputs. Adverse slippage raises buy prices and lowers sell prices, including
short entries and exits.

`total_pnl` is final marked equity minus starting credit, including open-position
P&L and costs already incurred. `closed_net_pnl` counts completed trades only;
`realized_pnl` includes cash costs on still-open positions. Drawdown uses marked
equity. Future exit costs on positions still open at the boundary are excluded.
New DE and TPE runs in the Optimization tab use all selected candles in one
continuous backtest, without training/test splits or validation folds. The
objective, progress metrics, chart, trades and default-input baseline use the
same window. Earlier history initializes indicators without reducing the selected
candle count; those warmup candles are omitted from the displayed chart.
These results are in-sample historical performance.

The Balanced objective requires a selected minimum win rate and a minimum number
of closed trades in the evaluated window (30 by default), then maximizes uncapped net P&L among
eligible parameter sets. The trade count is a sample-size guard, not statistical
proof. If no tested set meets both floors, the run reports no eligible result.
Win rate uses closed trades only, while net P&L includes marked open positions.
Explicit chronological searches and saved split runs apply these floors to the combined forward validation
windows, each of which starts flat. Optimization Progress and the validation
summary report that selection result. The full training chart, trade history,
training statistics and default-input baseline use a separate continuous run
over all training candles, so their trade counts and returns can differ from
validation. The final held-out window remains separate from both.

Saved chronological results from before this reporting separation recover
training statistics from their saved chart candles only when the replay matches
the saved trade log. This recovery leaves the stored result untouched. If the
replay cannot be verified, training statistics are shown as unavailable rather
than borrowing validation metrics or displaying zeros.

The floors apply to both DE and Optuna multivariate TPE searches. TPE uses one
backtest per trial; the independent long/short strategy uses the configured trial
budget separately for each side. Older saved runs retain their held-out win rate,
closed trades, net P&L and max drawdown for review in the saved-run results.

The DE tab offers all 12 [SciPy DE strategies](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.differential_evolution.html):
`best1bin`, `best1exp`, `best2bin`, `best2exp`, `rand1bin`, `rand1exp`,
`rand2bin`, `rand2exp`, `randtobest1bin`, `randtobest1exp`,
`currenttobest1bin` and `currenttobest1exp`. The `bin` and `exp` suffixes select
binomial and exponential crossover. The chosen variant is recorded with the
run and applies to both independent sides. Older runs use their original
`best2bin` behavior when no variant was recorded.

## Liquidation and position policy

Long liquidation checks candle lows; short liquidation checks highs. A gap past
the threshold fills at the worse opening price. Existing positions process gap
liquidation before pending exits. Positions filled at the open can encounter that
candle's range; positions filled at its close cannot be liquidated retroactively
by its earlier high/low.

OHLC candles cannot identify the exchange mark price or exact intrabar ordering.
This conservative model is not a liquidation engine replica. Missing liquidity,
market impact, exchange rejection and outages are not modeled.

The default `net` research mode matches a single-position policy. `multi` is
explicitly a different research assumption. Neither mode changes how existing
real bots submit orders. Historical results are experiments under their recorded
inputs, not evidence of future profitability.
