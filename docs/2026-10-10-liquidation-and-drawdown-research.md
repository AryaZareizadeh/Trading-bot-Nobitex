# Preventing liquidation while targeting a 10% account drawdown limit

Research date: 10 October 2026, Asia/Tehran. Scope: read-only inspection of the current working-tree optimizer, simulator and live execution, plus official exchange documentation and primary financial research. The user chose maximum account drawdown as the priority and specified 10%. The numerical examples below were independently calculated; no optimizer benchmark, historical replay, live account inspection or exchange order was performed. Proposed settings are starting hypotheses to validate, not demonstrated safe parameters.

**Recommendation.** Introduce a mandatory protective exit and size every position from its intended account loss. Make risk limits hard eligibility conditions across optimization, validation and deployment. Enforce a shared account controller independently of each strategy. Target 10% maximum drawdown, but begin reducing exposure well before it: an exit initiated at 10% cannot reliably finish at 10%. Gaps, liquidity shortages, reference-price differences and outages can exceed any intended stop or drawdown boundary. If eliminating exchange liquidation is an absolute requirement, unborrowed spot trading removes margin liquidation; price and account drawdown risk remain.

**What the current project actually permits.**

| Verified behavior | Why it matters | Code evidence |
| --- | --- | --- |
| Balanced requires a minimum closed-trade count and win rate, then ranks eligible candidates solely by net P&L | An otherwise profitable candidate can include liquidations; even negative-P&L candidates can be eligible | [optimizer.py](D:/GitHub/Trading-bot-Nobitex/optimizer.py:199), lines 199–223 |
| Profit, win rate and large-move objectives have no common risk gate | Changing objective does not enforce safety; large-move scoring even counts large losing price moves | [optimizer.py](D:/GitHub/Trading-bot-Nobitex/optimizer.py:205), lines 205–232 |
| Research has no initial price stop or account drawdown circuit breaker | Indicator exits can arrive after liquidation | [execution_model.py](D:/GitHub/Trading-bot-Nobitex/execution_model.py:32), lines 32–60 and 159–191 |
| Liquidation checks candle highs/lows; ordinary strategy exits fill at the next open | A four-hour strategy exit cannot protect against every move inside those four hours | [execution_model.py](D:/GitHub/Trading-bot-Nobitex/execution_model.py:159), lines 159–191 |
| Drawdown is computed from candle-close equity | A severe temporary loss that recovers before the close can be understated | [execution_model.py](D:/GitHub/Trading-bot-Nobitex/execution_model.py:191), lines 191–200 |
| Entry margin is starting credit times `margin_fraction`, capped by cash | Sizing is based on collateral allocation, rather than stop-defined risk to current account equity | [execution_model.py](D:/GitHub/Trading-bot-Nobitex/execution_model.py:136), lines 136–147 |
| Live size uses `credit * leverage / price`, without applying research `margin_fraction` | Reducing research allocation alone can produce a smaller simulated position than the live position | [bot.py](D:/GitHub/Trading-bot-Nobitex/bot.py:488), lines 488–501 |
| Native conditional close orders are not submitted | Local strategy failure can leave an exchange position without protective orders | [nobitex_client.py](D:/GitHub/Trading-bot-Nobitex/nobitex_client.py:257), lines 257–282 and 340–351; `exchange_execution.py`, lines 132–150 |
| Live liquidation fields are normalized but do not cause an early risk exit | A displayed liquidation price is not an enforced protection | [bot.py](D:/GitHub/Trading-bot-Nobitex/bot.py:453), lines 453–456 and 668–750 |
| Normal strategy evaluation follows closed candles; stopping the bot does not close its positions | The protection must survive strategy stop/error and must have explicit operational semantics | [bot.py](D:/GitHub/Trading-bot-Nobitex/bot.py:1299), lines 1299–1310 and 1420–1495 |

The default local SQLite store contains ZECUSDT / AdvRSI / four-hour / 2,100-candle preferences, $10 credit, 1x leverage, Balanced, and a 5,000-iteration DE budget. Its missing win-rate/trade-count fields merge with defaults of 50% and 30 trades. No execution preference record was present. Without a request override, this uses zero fees, zero slippage, no holding charges, full margin allocation, next-open fills and liquidation ratio 1.1. This describes the default local store, not an independently identified running server or request. The ETHUSDT daily configuration in [optimizer.json](D:/GitHub/Trading-bot-Nobitex/optimizer.json) is different and should not be treated as the active record. Preference loading is at [backtester.py:335–341](D:/GitHub/Trading-bot-Nobitex/backtester.py:335); execution selection is at [server.py:1183–1189](D:/GitHub/Trading-bot-Nobitex/server.py:1183).

Ordinary Optimization searches the displayed window in sample. The separate chronological tester reserves a later holdout and uses forward validation windows inside development data. Its fold aggregation pools profit and win rate but does not aggregate liquidation or drawdown constraints. Real-promotion checks already exclude ordinary in-sample runs and check positive performance against baselines and Nobitex transfer data, but still lack liquidation/drawdown gates. Retain these existing checks and add risk requirements. See [optimizer.py:248–261](D:/GitHub/Trading-bot-Nobitex/optimizer.py:248), `957–994`, `1026` and `1258–1290`.

**Understand the exchange boundary before choosing a stop.**

Nobitex/NoBitX's official liquidation guide states a commitment-ratio threshold of 1.1. Its concepts guide distinguishes long and short asset/liability accounting. Under the repository's simplified assumptions, with entry price P0, leverage L and ratio r:

```text
long liquidation price  = P0 * (r - 1/L)
short liquidation price = P0 * (L + 1) / (r * L)
```

These are model calculations, ignoring fees, collateral changes and exact exchange reference prices. They are not live liquidation quotes. [Official liquidation guide](https://nobitx.com/help/discover/position/what-is-liquidation-in-commitment-trading-and-when-does-it-happen/), [official accounting concepts](https://nobitx.com/help/discover/position/key-concepts-to-know-for-commitment-trading/).

| Model leverage, r = 1.1 | Long adverse price move to liquidation | Short adverse price move to liquidation |
| --- | ---: | ---: |
| 1x | 90.00% down | 81.82% up |
| 3x | 23.33% down | 21.21% up |
| 5x | 10.00% down | 9.09% up |

At 5x, those moves imply approximately 50% or 45.45% loss of posted margin before costs. Thus a 10% price movement, 10% collateral loss and 10% account drawdown are three different quantities. The simulator's 1x setting still represents a borrowed margin position; it does not switch to spot. Lowering position allocation reduces account damage but does not widen the modeled liquidation distance at unchanged leverage.

The current leverage help page lists 1–5x, subject to account and market restrictions. An older FAQ mentions higher leverage in its warning table, so the published pages are not fully consistent. Validate allowed leverage from the relevant market/account response and use fresh exchange position information. A constant 1.1 combined with arbitrarily high leverage is particularly unsuitable: at 10x, the model puts liquidation at entry. [Current leverage guide](https://nobitx.com/help/discover/position/how-much-leverage-can-i-get-against-margin/).

The FAQ describes isolated collateral and a weighted reference price combining international spot venues and NoBitX. Renewal deductions change liquidation distance. Its email warnings are supplementary. Market-price stop activation should not be assumed to use exactly the liquidation reference. Spare wallet cash alone does not automatically protect an isolated position. [Official margin FAQ](https://nobitx.com/help/discover/position/other-tips-and-questions-about-commitment-trading/).

**Choose the intended exit, then calculate quantity.**

A protective stop should represent where the trade thesis fails and leave room for normal volatility. CME's position-sizing guidance explicitly connects the stop location with the account amount the trader can afford to risk. [CME: Proper Position Size](https://www.cmegroup.com/education/courses/trade-and-risk-management/proper-position-size).

For this RSI strategy, test a small predeclared family: a recent structural invalidation level or an entry volatility stop, such as 1.5, 2, 2.5 and 3 times ATR. These multipliers are proposed experiments, not researched optimal settings. ATR must use information available before entry. Keep the initial risk boundary fixed; a trailing policy may tighten it but should not widen it after a loss. Compare policies on forward data rather than choosing whichever rescued the most historical losers.

Stops can alter the return characteristics of a strategy; research by Kaminski and Lo demonstrates that their effect depends on the underlying process. It does not establish an optimal stop for Nobitex or cryptocurrency RSI trading. The inference for this project is to test the combined entry, stop and sizing policy, particularly because repeated tight exits may impair a reversal strategy. [Kaminski and Lo, When Do Stop-Loss Rules Stop Losses?](https://dspace.mit.edu/entities/publication/bb69ca4b-0cdc-487f-831d-63b2e84fafee).

Let E be current account equity, r_trade the intended risk fraction, d_stop the adverse price distance as a fraction of entry, and c an allowance for round-trip fees and adverse execution. A simple sizing approximation is:

```text
intended loss budget = E * r_trade
position notional   = intended loss budget / (d_stop + c)
quantity            = position notional / entry price
required collateral ≈ position notional / leverage
```

Add a separate gap allowance where appropriate and avoid counting the same cost twice. Use exact per-unit costs and exchange precision in implementation. Cap quantity further by portfolio risk, available collateral, minimum/maximum order rules, delegation capacity and tradable liquidity. If the minimum order exceeds the budget, skip the trade instead of rounding risk upward.

Example: E = $1,000, intended risk = 0.5% = $5, stop distance = 2%, and an illustrative 0.3% cost/execution allowance. Notional is $5 / 0.023 = $217.39; at 3x, collateral is approximately $72.46. The intended loss is $5, not the collateral amount. The allowance is not a guaranteed fill bound. Changing leverage at unchanged notional mainly changes collateral requirements and liquidation distance; multiplying position notional by leverage increases price risk.

For every entry, require:

```text
adverse stop distance + execution/reference-price stress buffer
    < adverse distance to liquidation
```

Measure the buffer with venue data, spread/depth, liquidation-reference differences, observed fast moves and realistic interruption horizons. If a sensible strategy stop cannot satisfy the inequality, lower leverage or skip the entry. Do not force a stop into normal noise merely to make high leverage fit. Adding collateral improves liquidation distance but does not remove the existing position's dollar price risk; an automatic rescue policy can put more account capital behind a failing position.

**Translate the selected 10% account drawdown into enforceable controls.**

Measure drawdown from a cash-flow-adjusted high-water mark in a consistent reporting currency. E must include all relevant open P&L and incurred costs without double-counting position assets, wallet collateral or debt. Deposits, withdrawals, restarts and retuning must not erase a loss record. Existing portfolio cash-flow accounting is a starting point, but reporting alone is not enforcement ([portfolio.py:145–181](D:/GitHub/Trading-bot-Nobitex/portfolio.py:145)). If manual trades or other assets count toward the account target, include their risk; otherwise call the limit a bot-sleeve drawdown limit.

For a period without external cash flows:

```text
H        = highest account equity reached
drawdown = 1 - E/H
floor    = 0.90 * H
```

Proposed initial controls, subject to stress validation:

| Control | Starting design |
| --- | --- |
| Intended risk per trade | 0.5% of current account equity, less when remaining headroom requires |
| Combined planned risk to stops | At most 1.5% of equity across bots; correlated and pending exposures share the budget |
| Initial leverage | Research 1–2x first; consider 3x only after buffer, liquidity and stress checks |
| 5% drawdown | Halve the new-trade risk budget; do not increase position size to recover losses |
| 7% drawdown | Freeze new entries and reduce exposure if remaining stressed risk is excessive |
| 8% drawdown | Cancel opening orders and initiate closure of authorized bot-owned exposure; keep reconciling until settled |
| 10% drawdown | User's desired ceiling; never wait for this point to begin protective action |

The 8% threshold leaves two percentage points of high-water-mark equity for adverse execution. This is an illustrative reserve, not evidence that two points are enough for the chosen markets. Enlarge it, reduce exposure, move exits earlier or reject the policy if stress results require that. The stop/freeze state should persist across restarts and need a deliberate restart decision after review; a new optimization result must not silently clear it.

A continuous cash headroom constraint must override these staged thresholds:

```text
new additional stressed loss capacity = max(0, E - 0.90*H - X - B)
```

X is additional stressed loss on existing and pending exposure measured from current marks to adverse future execution, including future costs not already included in E. It is not entry-to-stop loss, because losses already incurred are already in E. B is an additional residual reserve, initially illustrated as 0.02*H. Deduct a reserve exactly once; if E is already stress-adjusted, change the formula accordingly. Evaluate joint scenarios so correlation, reference-price divergence and cost effects are captured. For absolute risk control, do not assume independent long and short bots perfectly hedge each other.

If X plus B exceeds remaining headroom, refuse new exposure and reduce existing risk immediately. This can happen before the 7% threshold. For example, a fixed 1.5% equity exposure allowance plus 2% H reserve would exhaust headroom around 6.6% drawdown. The table's thresholds are latest action points, not permission to keep taking risk until them.

A 0.5% trade budget cannot establish a 10% maximum: even perfect fills at that budget would exceed 10% after 22 consecutive losses when each trade risks 0.5% of remaining equity. Account headroom, exposure reduction and stopping rules are essential.

**Change selection from profitable-at-any-risk to profitable-within-the-policy.**

Risk eligibility should be shared by DE, TPE, every objective, individual long/short searches, combined portfolio evaluation and real-promotion checks. Reject candidates with a simulated liquidation, an unprotected position, insufficient liquidation buffer, unacceptable drawdown, or stressed account-floor breach. Do not implement liquidation as a soft profit penalty that exceptionally large profits can offset. Separate feasibility from ranking and return “no eligible result” when nothing meets the policy.

Use the proposed controller in every candidate simulation. Require base-case drawdown to fit the earlier operational exit policy, and account drawdown no greater than 10% in every predeclared stress scenario. Zero simulated liquidation remains a separate condition. Evaluate each validation window as well as the continuous combined portfolio; summing fold profit or separate maximum drawdowns cannot reconstruct the account's path. Open positions at a boundary still need risk and hypothetical liquidation-cost evaluation even when not forced closed in the main return calculation.

Among eligible candidates, compare forward net return, worst-window performance, drawdown, tail loss, turnover/cost sensitivity and neighboring-parameter stability. Expected shortfall describes average loss in a distribution's worst tail; it is useful alongside maximum drawdown, not a replacement for a hard floor. Thirty closed trades are too little to confidently characterize rare liquidation or a 5% tail; disclose uncertainty. [BIS: market risk framework in brief](https://www.bis.org/bcbs/publ/d457_inbrief.pdf).

**Execution and validation determine whether the protection is credible.**

The documented position-close endpoint supports `stop_market` and `stop_limit`. A submitted close is not a settled position: confirm acceptance and remaining liability after partial fills. The documented OCO consists of a limit leg and a stop-limit leg; do not represent it as a stop-market loss exit. [Official close-position API](https://apidocs.nobitex.ir/margin_trade/%D8%A8%D8%B3%D8%AA%D9%86-%D9%85%D9%88%D9%82%D8%B9%DB%8C%D8%AA), [official margin-order API](https://apidocs.nobitex.ir/margin_trade/%D8%AF%D8%B1%D8%AC-%D8%B3%D9%81%D8%A7%D8%B1%D8%B4-%D8%AA%D8%B9%D9%87%D8%AF%DB%8C).

For capital preservation, test native stop-market as the primary exit where available and accepted. It prioritizes execution but cannot guarantee the price. Stop-limit prioritizes a limit price and can leave exposure open. FINRA documents this execution tradeoff for securities; the principle is relevant here, while its regulatory rules do not establish Nobitex behavior. [FINRA Regulatory Notice 16-19](https://www.finra.org/rules-guidance/notices/16-19).

Protection should attach to actual fills, including partial entries, and remain supervised during strategy pause, process restart, model failure and stale candles. A risk exit must keep pursuing the remaining owned exposure after partial or rejected terminal orders. Preserve the existing durable-intent and uncertain-submission safeguards; urgent exits do not justify blindly duplicating orders. Coordinate protective exits with normal settlement orders because outstanding orders reserve liability/assets. Verify API behavior before choosing a cancel/replace sequence, and minimize any period without confirmed protection. A centralized risk monitor should respect shared API limits rather than running a separate rapid poller per bot.

Backtest native protective stops intrabar, including gaps through the stop and gaps directly beyond liquidation. Never credit a stop fill at a price the market skipped. When both stop and target are inside one OHLC candle, use finer data or adverse ordering; do not choose the profitable path. Lower-timeframe venue data is preferable, especially for four-hour strategies. Multi-asset bar highs/lows are not necessarily simultaneous: conservative aligned stress bounds can supplement, but should not be presented as observed intrabar portfolio equity.

Configure actual trading-cost tiers and deliberate adverse execution scenarios; include renewal costs and their effect on collateral/liquidation distance. Official help pages disagree about renewal cadence, so verify applicable rates and billing from current account/exchange evidence. Current fee snapshots do not recreate historical fees. Test normal costs and predeclared 2–3x friction scenarios, historical rapid moves, larger hypothetical gaps, reference-price divergence, partial fills and interruption scenarios. The stress multipliers are proposed tests, not forecasts or guarantees.

Use chronological development and walk-forward evaluation, with a final untouched period. Freeze the stop policy, risk budget, search bounds, cost assumptions and selection method before opening that final period. Treat repeated viewing/tuning of a holdout as development. Broad searches increase selection bias; Bailey and colleagues' backtest-overfitting research explains why the best historical configuration can be a false discovery. [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).

The optimizer currently uses Binance as its default data venue ([optimizer.py:59](D:/GitHub/Trading-bot-Nobitex/optimizer.py:59)), so evaluate finalists on reliable Nobitex data and compare liquidation-reference behavior. Block-resampling observed returns can explore clustered losses, but cannot invent unseen exchange outages or reproduce liquidation mechanics unless the price/execution paths are replayed appropriately. Include those scenarios explicitly. Paper operation should verify order protection, supervision and reconciliation before real capital; it does not reproduce the full liquidity of actual fills.

Record liquidation count, protective stop count, exit reason, worst realized loss, maximum adverse excursion, minimum liquidation distance, drawdown duration, continuous account drawdown, joint stressed loss, open exposure and failures of protection. Check representative cases: stop inside a bar, gap through stop, gap beyond liquidation, missing price feed, rejected/partial exit, restart with an open position, two correlated bots, and drawdown capacity exhausted by a pending entry.

The implementation order is: define account equity/headroom and the shared risk policy; build and validate protective execution in the historical model; apply hard optimizer/promotion gates; implement equivalent live position protection and independent account supervision; then rerun chronological research and verify paper operation. Existing optimizer profits must be reevaluated under the new policy. No production code or account settings were changed during this research.
