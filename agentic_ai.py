"""Causal, cost-aware multi-timeframe trading research and decision policy.

Each specialist sees only its own completed candles. A deterministic decision
agent combines calibrated probabilities and can abstain. No LLM can submit an
order. Live execution remains the responsibility of OrderExecutor in bot.py.
"""

from dataclasses import dataclass
import hashlib
import math

import numpy as np
import pandas as pd

from execution_model import execution_config, simulate_signals
from price_ai import _features


STRATEGY_NAME = "AgenticAI"
MODEL_VERSION = "agentic-ai-mtf-hgb-v1"
SECONDS = {"15": 900, "60": 3600, "240": 14400, "D": 86400}
HORIZON = 4
MIN_ROWS = 900
MIN_DISTINCT_HIGHER_BARS = 80
ENTRY_PROBABILITY = 0.64
HOLD_PROBABILITY = 0.50
MAX_LEVERAGED_LOSS = 0.10


def validate_timeframes(primary: str, selected: list[str] | None) -> list[str]:
    frames = selected if selected is not None else [primary, "240" if primary in ("15", "60") else "D"]
    if not isinstance(frames, list) or len(frames) not in (2, 3) or len(set(frames)) != len(frames):
        raise ValueError("AgenticAI needs two or three distinct timeframes")
    if any(tf not in SECONDS for tf in frames) or primary not in frames:
        raise ValueError("AgenticAI timeframes must include the primary candle and use 15m, 1h, 4h or 1d")
    ordered = sorted(frames, key=SECONDS.__getitem__)
    if ordered[0] != primary or SECONDS[ordered[-1]] / SECONDS[primary] > 24:
        raise ValueError("The primary candle must be the shortest; the longest can be at most 24 times longer")
    return ordered


def validate_execution(execution) -> None:
    ex = execution_config(execution)
    if ex.position_mode != "net" or ex.fill_timing != "next_open":
        raise ValueError("AgenticAI requires net positions and next-open research fills")
    if min(ex.entry_fee_rate, ex.exit_fee_rate, ex.slippage_bps) <= 0:
        raise ValueError("AgenticAI needs positive fee and slippage assumptions")


def _validate_frame(df: pd.DataFrame, tf: str) -> None:
    if df.empty or not isinstance(df.index, pd.DatetimeIndex) or not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError(f"{tf} candles are missing or out of order")
    if not np.isfinite(df[["Open", "High", "Low", "Close", "Volume"]].to_numpy(dtype=float)).all():
        raise ValueError(f"{tf} candles contain invalid numbers")
    if (df[["Open", "High", "Low", "Close"]] <= 0).any().any():
        raise ValueError(f"{tf} candles contain nonpositive prices")
    if (df.Volume < 0).any() or (df.High < df[["Open", "Close", "Low"]].max(axis=1)).any() or (df.Low > df[["Open", "Close", "High"]].min(axis=1)).any():
        raise ValueError(f"{tf} candles have inconsistent OHLCV values")
    gaps = df.index.to_series().diff().dropna().dt.total_seconds()
    if (gaps != SECONDS[tf]).any():
        raise ValueError(f"{tf} candle history has a gap")


def feature_matrix(frames: dict[str, pd.DataFrame], primary: str, selected: list[str]) -> pd.DataFrame:
    """Join specialist features only after their source candle has closed."""
    base = frames[primary]
    _validate_frame(base, primary)
    base_close = pd.DatetimeIndex(base.index) + pd.Timedelta(seconds=SECONDS[primary])
    out = pd.DataFrame(index=base.index)
    for tf in selected:
        source = frames[tf]
        _validate_frame(source, tf)
        features = _features(source).add_prefix(f"{tf}_")
        available = pd.DatetimeIndex(source.index) + pd.Timedelta(seconds=SECONDS[tf])
        right = features.copy()
        right.index = available
        joined = pd.merge_asof(
            pd.DataFrame({"decision_close": base_close}),
            right.reset_index(names="available_at"),
            left_on="decision_close", right_on="available_at", direction="backward",
            tolerance=pd.Timedelta(seconds=SECONDS[tf]),
        )
        joined.index = base.index
        out = pd.concat([out, joined.drop(columns=["decision_close", "available_at"])], axis=1)
    return out.replace([np.inf, -np.inf], np.nan)


def _cost_bps(execution) -> float:
    ex = execution_config(execution)
    return 10000 * (ex.entry_fee_rate + ex.exit_fee_rate) + 2 * ex.slippage_bps


@dataclass
class Specialist:
    long_model: object
    short_model: object
    long_calibrator: object
    short_calibrator: object


@dataclass
class TrainedAgent:
    specialists: dict[str, Specialist]
    ensemble_long_calibrator: object
    ensemble_short_calibrator: object
    metadata: dict


def train_agent(frames: dict[str, pd.DataFrame], primary: str, selected: list[str],
                execution, train_end: int | None = None) -> TrainedAgent:
    """Use disjoint chronological fit, calibration, and validation windows."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import brier_score_loss

    selected = validate_timeframes(primary, selected)
    validate_execution(execution)
    base = frames[primary]
    if train_end is None:
        train_end = len(base)
    if train_end > len(base) or train_end < MIN_ROWS:
        raise ValueError(f"AgenticAI needs at least {MIN_ROWS} primary candles")
    matrix = feature_matrix(frames, primary, selected)
    cost = _cost_bps(execution) + 10.0
    future = 10000 * np.log(base.Close.shift(-HORIZON) / base.Close)
    # No label may end inside the evaluation period.
    eligible = np.arange(len(base)) + HORIZON < train_end
    split1, split2, split3 = int(train_end * 0.50), int(train_end * 0.68), int(train_end * 0.83)
    if min(split2 - split1, split3 - split2, train_end - split3) < 120:
        raise ValueError("AgenticAI has too little separate calibration or validation history")
    specialists, metrics = {}, {}
    for tf in selected:
        columns = [c for c in matrix if c.startswith(f"{tf}_")]
        valid = matrix[columns].notna().all(axis=1).to_numpy() & np.isfinite(future.to_numpy()) & eligible
        fit = np.flatnonzero(valid & (np.arange(len(base)) + HORIZON < split1))
        calibrate = np.flatnonzero(valid & (np.arange(len(base)) >= split1) & (np.arange(len(base)) + HORIZON < split2))
        ensemble_cal = np.flatnonzero(valid & (np.arange(len(base)) >= split2) & (np.arange(len(base)) + HORIZON < split3))
        verify = np.flatnonzero(valid & (np.arange(len(base)) >= split3))
        if min(len(fit), len(calibrate), len(ensemble_cal), len(verify)) < 100:
            raise ValueError(f"{tf} specialist lacks 100 examples in every chronological split")
        if tf != primary:
            distinct = frames[tf].index[(frames[tf].index + pd.Timedelta(seconds=SECONDS[tf])) <= base.index[split1]]
            if len(distinct) < MIN_DISTINCT_HIGHER_BARS:
                raise ValueError(f"{tf} specialist needs {MIN_DISTINCT_HIGHER_BARS} distinct training candles")
        x = matrix[columns]
        fitted = []
        for direction, labels in (("long", future > cost), ("short", future < -cost)):
            y = labels.astype(int).to_numpy()
            if len(set(y[fit])) < 2 or len(set(y[calibrate])) < 2:
                raise ValueError(f"{tf} {direction} specialist has only one outcome class")
            model = HistGradientBoostingClassifier(max_iter=80, max_leaf_nodes=7,
                       min_samples_leaf=40, l2_regularization=2.0, random_state=42)
            model.fit(x.iloc[fit], y[fit])
            raw_cal = model.predict_proba(x.iloc[calibrate])[:, 1]
            raw_val = model.predict_proba(x.iloc[verify])[:, 1]
            # Platt calibration is fitted on a separate time block.
            calibrator = LogisticRegression(random_state=42)
            calibrator.fit(raw_cal.reshape(-1, 1), y[calibrate])
            probabilities = calibrator.predict_proba(raw_val.reshape(-1, 1))[:, 1]
            brier = float(brier_score_loss(y[verify], probabilities))
            baseline_rate = float(y[calibrate].mean())
            baseline_brier = float(brier_score_loss(y[verify], np.full(len(verify), baseline_rate)))
            metrics[direction + "_brier"] = round(brier, 5)
            metrics[direction + "_baseline_brier"] = round(baseline_brier, 5)
            fitted.append((model, calibrator))
        specialists[tf] = Specialist(fitted[0][0], fitted[1][0], fitted[0][1], fitted[1][1])
        metrics[tf] = {k: metrics.pop(k) for k in ("long_brier", "long_baseline_brier", "short_brier", "short_baseline_brier")}
        metrics[tf]["validation_rows"] = len(verify)
        metrics[tf]["calibration_rows"] = len(calibrate)
        metrics[tf]["fit_rows"] = len(fit)
    all_columns = list(matrix.columns)
    complete = matrix[all_columns].notna().all(axis=1).to_numpy() & np.isfinite(future.to_numpy()) & eligible
    cal_idx = np.flatnonzero(complete & (np.arange(len(base)) >= split2) & (np.arange(len(base)) + HORIZON < split3))
    val_idx = np.flatnonzero(complete & (np.arange(len(base)) >= split3))
    if min(len(cal_idx), len(val_idx)) < 100:
        raise ValueError("AgenticAI ensemble lacks chronological calibration or validation examples")
    ensemble_calibrators, ensemble_metrics = {}, {}
    for direction in ("long", "short"):
        def mean_probability(indices):
            predictions = []
            for tf, specialist in specialists.items():
                cols = [c for c in matrix if c.startswith(f"{tf}_")]
                model = getattr(specialist, direction + "_model")
                calibrator = getattr(specialist, direction + "_calibrator")
                raw = model.predict_proba(matrix[cols].iloc[indices])[:, 1]
                predictions.append(calibrator.predict_proba(raw.reshape(-1, 1))[:, 1])
            return np.mean(predictions, axis=0)
        y = ((future > cost) if direction == "long" else (future < -cost)).astype(int).to_numpy()
        if len(set(y[cal_idx])) < 2:
            raise ValueError(f"AgenticAI ensemble has only one {direction} outcome in calibration")
        raw_cal = mean_probability(cal_idx)
        raw_val = mean_probability(val_idx)
        calibrator = LogisticRegression(random_state=42)
        calibrator.fit(raw_cal.reshape(-1, 1), y[cal_idx])
        forecast = calibrator.predict_proba(raw_val.reshape(-1, 1))[:, 1]
        baseline_rate = float(y[cal_idx].mean())
        ensemble_metrics[direction + "_brier"] = float(brier_score_loss(y[val_idx], forecast))
        ensemble_metrics[direction + "_baseline_brier"] = float(brier_score_loss(y[val_idx], np.full(len(val_idx), baseline_rate)))
        ensemble_calibrators[direction] = calibrator
    ensemble_metrics["calibration_rows"] = len(cal_idx)
    ensemble_metrics["validation_rows"] = len(val_idx)
    ensemble_metrics["passed"] = all(ensemble_metrics[d + "_brier"] < ensemble_metrics[d + "_baseline_brier"] for d in ("long", "short"))
    training = base.iloc[:train_end]
    digest = hashlib.sha256(pd.util.hash_pandas_object(training, index=True).values.tobytes()).hexdigest()
    return TrainedAgent(specialists, ensemble_calibrators["long"], ensemble_calibrators["short"],
        {"model_version": MODEL_VERSION, "timeframes": selected,
        "primary_timeframe": primary, "training_rows": train_end, "training_end": str(training.index[-1]),
        "training_data_hash": digest, "validation": metrics, "ensemble_validation": ensemble_metrics,
        "horizon_primary_candles": HORIZON,
        "target": "next four primary candle return exceeds round-trip costs plus 10 bps",
        "validation_passed": ensemble_metrics["passed"]})


def probability_evidence(model: TrainedAgent, row: pd.Series) -> dict[str, dict]:
    result = {}
    for tf, specialist in model.specialists.items():
        columns = [c for c in row.index if c.startswith(f"{tf}_")]
        if row[columns].isna().any():
            raise ValueError(f"{tf} specialist has no fresh completed candle")
        x = pd.DataFrame([row[columns]])
        p_long = specialist.long_calibrator.predict_proba(
            specialist.long_model.predict_proba(x)[:, 1].reshape(-1, 1))[0, 1]
        p_short = specialist.short_calibrator.predict_proba(
            specialist.short_model.predict_proba(x)[:, 1].reshape(-1, 1))[0, 1]
        result[tf] = {"long_probability": round(float(p_long), 4),
                      "short_probability": round(float(p_short), 4)}
    aggregate = {}
    for direction in ("long", "short"):
        mean = float(np.mean([v[direction + "_probability"] for v in result.values()]))
        calibrator = getattr(model, "ensemble_" + direction + "_calibrator")
        aggregate[direction + "_probability"] = round(float(calibrator.predict_proba(np.array([[mean]]))[0, 1]), 4)
    result["agent"] = aggregate
    return result


def decide(evidence: dict, position: str = "none", leveraged_return: float | None = None) -> dict:
    """Decision agent: consensus, abstention, and hard position loss limit."""
    specialists = {k: v for k, v in evidence.items() if k != "agent"}
    longs = [v["long_probability"] for v in specialists.values()]
    shorts = [v["short_probability"] for v in specialists.values()]
    long_mean, short_mean = evidence["agent"]["long_probability"], evidence["agent"]["short_probability"]
    long_votes = sum(p >= ENTRY_PROBABILITY for p in longs)
    short_votes = sum(p >= ENTRY_PROBABILITY for p in shorts)
    needed = math.ceil(len(specialists) * 2 / 3)
    candidate = "none"
    if long_votes >= needed and long_mean >= ENTRY_PROBABILITY and long_mean > short_mean + .15:
        candidate = "long"
    elif short_votes >= needed and short_mean >= ENTRY_PROBABILITY and short_mean > long_mean + .15:
        candidate = "short"
    if position != "none":
        supporting = long_mean if position == "long" else short_mean
        if leveraged_return is not None and leveraged_return <= -MAX_LEVERAGED_LOSS:
            signal, reason = "none", "position loss limit reached"
        elif supporting >= HOLD_PROBABILITY and candidate in ("none", position):
            signal, reason = position, "position evidence supports holding"
        else:
            signal, reason = "none", "position evidence weakened or reversed"
    else:
        signal = candidate
        reason = "specialists agree on cost-adjusted opportunity" if candidate != "none" else "no specialist consensus"
    confidence = long_mean if signal == "long" else short_mean if signal == "short" else None
    action = "hold" if signal == position and position != "none" else ("enter_" + signal if signal != "none" else "exit" if position != "none" else "abstain")
    return {"action": action,
            "signal": signal, "confidence": round(confidence, 4) if confidence is not None else None,
            "confidence_meaning": "validated calibrated ensemble probability of a cost-adjusted directional move over the next four primary candles",
            "reason": reason, "specialists": specialists, "model_version": MODEL_VERSION}


def evaluate_agent(frames: dict[str, pd.DataFrame], primary: str, selected: list[str],
                   model: TrainedAgent, credit: float, leverage: int, execution,
                   boundary: int) -> dict:
    base = frames[primary]
    matrix = feature_matrix(frames, primary, selected)
    decisions = []

    def policy(i, positions):
        if not model.metadata["validation_passed"]:
            decisions.append({"candle_time": str(base.index[i]), "action": "abstain",
                "signal": "none", "confidence": None,
                "reason": "chronological calibration validation did not beat baseline",
                "model_version": MODEL_VERSION})
            return None, []
        evidence = probability_evidence(model, matrix.iloc[i])
        position = positions[0]["direction"].lower() if positions else "none"
        leveraged_return = None
        if positions:
            p = positions[0]
            sign = 1 if position == "long" else -1
            leveraged_return = sign * (float(base.Close.iloc[i]) / p["entry_price"] - 1) * leverage
        decision = decide(evidence, position, leveraged_return)
        decision["candle_time"] = str(base.index[i])
        decisions.append(decision)
        entry = decision["signal"].title() if position == "none" and decision["signal"] != "none" else None
        exits = [position.title()] if position != "none" and decision["signal"] == "none" else []
        return entry, exits

    result = simulate_signals(base, policy, credit, leverage, execution, boundary)
    result["decisions"] = decisions
    result["metadata"].update(strategy=STRATEGY_NAME, model=model.metadata)
    result["summary"]["metadata"] = result["metadata"]
    return result


def backtest(frames: dict[str, pd.DataFrame], primary: str, selected: list[str],
             credit: float, leverage: int, execution) -> dict:
    boundary = int(len(frames[primary]) * .7)
    model = train_agent(frames, primary, selected, execution, boundary)
    return evaluate_agent(frames, primary, selected, model, credit, leverage, execution, boundary)
