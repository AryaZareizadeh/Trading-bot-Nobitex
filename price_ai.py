"""Price-only research policy for simulation bots.

The estimator is fitted on candles strictly before the first evaluated candle.
Features use information available at each completed candle; the shared OHLC
simulator applies the resulting decisions at the next open.
"""

from dataclasses import dataclass
import hashlib
import math

import numpy as np
import pandas as pd

from execution_model import execution_config, simulate_signals


STRATEGY_NAME = "PriceAI"
MODEL_VERSION = "price-ai-hgbr-v1"
HORIZON_CANDLES = 4
MIN_TRAIN_ROWS = 400
FEATURE_WARMUP = 30
MIN_EDGE_BUFFER_BPS = 10.0
MAX_HOLD_CANDLES = 18
MAX_POSITION_LOSS_FRACTION = 0.10
FEATURES = (
    "return_1", "return_3", "return_6", "return_12", "return_24",
    "range", "body", "volume_ratio", "volatility_12", "volatility_24",
)


@dataclass
class TrainedPriceModel:
    estimator: object
    metadata: dict


def _features(df: pd.DataFrame) -> pd.DataFrame:
    """Every row depends only on that row and earlier completed candles."""
    close = df["Close"].astype(float)
    previous = close.shift(1)
    log_return = np.log(close / previous)
    volume = df["Volume"].astype(float)
    average_volume = volume.rolling(24, min_periods=24).mean().shift(1)
    result = pd.DataFrame(index=df.index)
    for n in (1, 3, 6, 12, 24):
        result[f"return_{n}"] = np.log(close / close.shift(n))
    result["range"] = (df["High"] - df["Low"]) / close
    result["body"] = (close - df["Open"]) / df["Open"]
    result["volume_ratio"] = volume / average_volume.replace(0, np.nan)
    result["volatility_12"] = log_return.rolling(12, min_periods=12).std()
    result["volatility_24"] = log_return.rolling(24, min_periods=24).std()
    return result.replace([np.inf, -np.inf], np.nan)


def _cost_bps(execution) -> float:
    ex = execution_config(execution)
    return 10_000 * (ex.entry_fee_rate + ex.exit_fee_rate) + 2 * ex.slippage_bps


def validate_price_ai_execution(execution) -> None:
    ex = execution_config(execution)
    if ex.position_mode != "net" or ex.fill_timing != "next_open":
        raise ValueError("PriceAI requires net positions and next-open fills")
    if min(ex.entry_fee_rate, ex.exit_fee_rate, ex.slippage_bps) <= 0:
        raise ValueError("PriceAI requires positive entry fee, exit fee and slippage assumptions in Research settings")


def train_price_model(df: pd.DataFrame, train_end: int) -> TrainedPriceModel:
    """Fit a deterministic model; labels never touch the evaluation window."""
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error

    if not FEATURE_WARMUP < train_end <= len(df):
        raise ValueError("Invalid PriceAI training boundary")
    features = _features(df)
    target = 10_000 * np.log(df["Close"].shift(-HORIZON_CANDLES) / df["Close"])
    # A training label must end before the first evaluation candle.
    eligible = np.arange(len(df)) + HORIZON_CANDLES < train_end
    valid = eligible & features.notna().all(axis=1).to_numpy() & np.isfinite(target.to_numpy())
    x = features.loc[valid, list(FEATURES)]
    y = target.loc[valid]
    if len(x) < MIN_TRAIN_ROWS:
        raise ValueError(f"PriceAI needs at least {MIN_TRAIN_ROWS} complete training examples; got {len(x)}")

    def new_estimator():
        return HistGradientBoostingRegressor(
            max_iter=100, max_leaf_nodes=7, min_samples_leaf=30,
            l2_regularization=1.0, random_state=42,
        )

    split = int(len(x) * 0.8)
    eligible_indices = np.flatnonzero(valid)
    validation_start = int(eligible_indices[split])
    validation_train_rows = int(np.searchsorted(
        eligible_indices[:split] + HORIZON_CANDLES, validation_start,
        side="left",
    ))
    check = new_estimator()
    check.fit(x.iloc[:validation_train_rows], y.iloc[:validation_train_rows])
    predictions = check.predict(x.iloc[split:])
    validation_mae = float(mean_absolute_error(y.iloc[split:], predictions))
    zero_mae = float(mean_absolute_error(y.iloc[split:], np.zeros(len(predictions))))

    estimator = new_estimator()
    estimator.fit(x, y)
    training = df.iloc[:train_end]
    digest = hashlib.sha256(pd.util.hash_pandas_object(training, index=True).values.tobytes()).hexdigest()
    metadata = {
        "model_version": MODEL_VERSION,
        "feature_names": list(FEATURES),
        "horizon_candles": HORIZON_CANDLES,
        "training_rows": len(x),
        "training_window_start": str(training.index[0]),
        "training_window_end": str(training.index[-1]),
        "training_data_hash": digest,
        "validation_rows": len(predictions),
        "validation_train_rows": validation_train_rows,
        "validation_window_start": str(df.index[validation_start]),
        "validation_mae_bps": round(validation_mae, 4),
        "zero_forecast_mae_bps": round(zero_mae, 4),
    }
    return TrainedPriceModel(estimator, metadata)


def evaluate_price_model(df: pd.DataFrame, model: TrainedPriceModel, credit: float,
                         leverage: int, execution, start_idx: int) -> dict:
    """Replay model decisions through the same fill and accounting engine as Tester."""
    validate_price_ai_execution(execution)
    if not 0 <= start_idx <= len(df):
        raise ValueError("Invalid PriceAI evaluation boundary")
    features = _features(df)
    ready = features.notna().all(axis=1).to_numpy()
    estimates = np.full(len(df), np.nan)
    if ready.any():
        estimates[ready] = model.estimator.predict(features.loc[ready, list(FEATURES)])
    cost = _cost_bps(execution)
    threshold = cost + MIN_EDGE_BUFFER_BPS
    decisions = []

    def decide(i, positions):
        forecast = float(estimates[i])
        action, reason, entry, exits = "hold", "forecast below entry threshold", None, []
        if positions:
            pos = positions[0]
            direction = pos["direction"]
            sign = 1 if direction == "Long" else -1
            signed_change = sign * (float(df["Close"].iloc[i]) / pos["entry_price"] - 1)
            age = i - pos["entry_idx"]
            if signed_change * leverage <= -MAX_POSITION_LOSS_FRACTION:
                reason = "position loss limit reached at candle close"
            elif age >= MAX_HOLD_CANDLES:
                reason = "maximum holding period reached"
            elif math.isfinite(forecast) and sign * forecast < -cost / 2:
                reason = "forecast turned against open position"
            else:
                reason = "forecast supports holding open position"
            if reason != "forecast supports holding open position":
                exits = [direction]
                action = "exit_" + direction.lower()
        elif not math.isfinite(forecast):
            reason = "insufficient completed history"
        elif forecast > threshold:
            entry, action, reason = "Long", "enter_long", "forecast exceeds estimated round-trip cost and buffer"
        elif forecast < -threshold:
            entry, action, reason = "Short", "enter_short", "forecast exceeds estimated round-trip cost and buffer"
        decisions.append({
            "candle_time": str(df.index[i]),
            "action": action,
            "reason": reason,
            "forecast_return_bps": round(forecast, 4) if math.isfinite(forecast) else None,
            "entry_threshold_bps": round(threshold, 4),
            "model_version": model.metadata["model_version"],
        })
        return entry, exits

    result = simulate_signals(df, decide, credit, leverage, execution, start_idx)
    result["decisions"] = decisions
    result["metadata"].update(strategy=STRATEGY_NAME, model=model.metadata)
    result["summary"]["metadata"] = result["metadata"]
    return result


def train_and_evaluate(df: pd.DataFrame, credit: float, leverage: int, execution,
                       train_pct: float = 70.0) -> dict:
    """One chronological held-out experiment, useful before starting a simulation bot."""
    if not 50 <= train_pct <= 90:
        raise ValueError("train_pct must be between 50 and 90")
    train_end = int(len(df) * train_pct / 100)
    if len(df) - train_end < 50:
        raise ValueError("PriceAI needs at least 50 held-out candles")
    model = train_price_model(df, train_end)
    return evaluate_price_model(df, model, credit, leverage, execution, train_end)
