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
Optimizer objectives use this net research summary; the chosen metric and
held-out summary should be reviewed together.

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
