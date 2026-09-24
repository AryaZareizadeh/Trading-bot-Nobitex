# Agentic AI trading bot: research and implementation

Research reviewed 23 September 2026. This is a live capable decision system, not a claim of profitable performance. No exchange order was submitted during development.

## Evidence and design choice

| Evidence | What it supports | Design response |
| --- | --- | --- |
| [Agentic Trading survey, May 2026](https://arxiv.org/abs/2605.19337) reviewed 77 studies and found little comparable evidence on time consistent splits, costs, and reproducibility. | An LLM alone is not a reliable trading authority. | Use an auditable decision agent over numeric specialists; keep exchange execution in the existing reconciled order path. |
| [BTC trading under transaction costs, May 2026](https://arxiv.org/abs/2606.00060) tested XGBoost, LSTM, and iTransformer on hourly data with 27 walk forward folds; naïve sign trading failed after ten basis points of costs in its tested setups. | Forecast quality does not imply net trading profit. | Target moves that exceed fee, slippage, and an extra ten basis point buffer; demand specialist consensus before entry. |
| [scikit-learn probability calibration guidance](https://scikit-learn.org/stable/modules/calibration.html) describes calibrated probabilities, disjoint fitting/calibration data, and Brier score. | A model score must not be labeled confidence without validation. | Separate chronological fit, specialist calibration, ensemble calibration, and validation blocks. Report Brier against a constant rate baseline. |
| [scikit-learn TimeSeriesSplit guidance](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html) addresses ordered evaluation and gaps. | Random splits leak future market regimes. | Purge target horizon across split boundaries; join higher timeframe features only after their candles close. |
| [Nobitex margin position API](https://github.com/nobitex/docs-api/blob/master/source/includes/_position.md). | Live margin orders require exchange position and order reconciliation. | Reuse `OrderExecutor`, account ownership, wallet checks, and durable order intents. |

The academic results are context, not evidence that this strategy works on Nobitex, on other coins, or after future market changes. The selected tree model is a transparent, inexpensive baseline. A more complex transformer or an LLM would need to beat it on prospective, cost adjusted outcomes before replacing it.

## What the bot does

1. Fetches closed OHLCV candles for one selected cryptocurrency and two or three selected timeframes. Each bot uses one market; create another bot for another cryptocurrency.
2. Uses the shortest timeframe as the decision clock. Higher timeframe features become visible only when the exchange candle has closed. Missing or gapped candles stop a decision.
3. Fits two gradient boosted classifiers per timeframe: probability of a long move and probability of a short move beyond assumed round trip costs over the next four primary candles. Fits sigmoid calibrators on later data for each classifier and separate calibrators for the combined output on a third block.
4. Tests ensemble Brier score against a constant probability baseline on a fourth chronological block. A failed validation pauses trading. The user can inspect `ai_model.ensemble_validation` via the bot API.
5. The decision agent requires at least two thirds of specialists to agree and a calibrated ensemble probability of at least 64% before entering. It holds an owned position while supporting probability is at least 50%, and exits when evidence weakens or candle close shows a leveraged loss of 10% or more.
6. Records the action, calibrated probability, specialist probabilities, model version, candle time, and reason. Live signals go through the same durable Nobitex order executor and position reconciliation as existing live bots.

The percentage is the estimated probability of a **cost adjusted directional move over four primary candles** under the training distribution. It is not the probability that an actual Nobitex order will be profitable. Slippage, borrowing cost, gaps, partial fills, and regime changes can break that relationship.

## Operation and limits

- Configure positive entry and exit fees and a slippage scenario in Research settings. Fees are populated from available Nobitex account/market information; slippage remains a user scenario. The model does not submit orders if these values are absent.
- Choose a primary timeframe of 15 minutes, one hour, or four hours, and two or three timeframes total. The longest can be at most 24 times the primary duration. The default 1h/4h/1d setup requires enough uninterrupted history; newly listed markets may be rejected.
- Run the held out backtest before starting. The dashboard blocks a failed validation and requires an explicit start for AI bots. `Start All` excludes both AI strategies. Leverage is capped at 5× in this implementation.
- Initial model training requests up to 4,200 closed primary candles. The start backtest requests up to 6,000 primary candles, uses the first 70% for training/calibration/validation, and evaluates on the final 30% through the shared next open simulator.
- The current agent reads price and volume candles. It does not ingest news, order book depth, funding, or on chain data. It does not retrain while running; stopping and starting trains a new model. Those additions require their own timestamped, point in time data and prospective validation.
- Calibration on one historical block can still fail in a new regime. Paper trading across several market conditions and a small live pilot with monitored fill quality are needed before increasing capital. Historical backtests cannot verify actual Nobitex execution.
