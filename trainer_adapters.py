"""Persist the existing CPU research pipelines as private trainer artifacts.

Tree models retain their original causal features, targets and estimators. The
first 70% of history is supplied to their fitting pipeline, followed by a
separate 15% validation block and an untouched 15% test block. Labels crossing
either boundary are purged. These artifacts are forecasts; existing trading
bots do not load them. PyTorch is never needed by this module.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import math
import os
from pathlib import Path
import re
import tempfile

import numpy as np
import pandas as pd


ARCHITECTURES = ("price-ai-hgbr-v1", "agentic-ai-mtf-hgb-v1", "spot-hgb-v1", "spot-lightgbm-v1")
SECONDS = {"15": 900, "60": 3600, "240": 14400, "D": 86400}
COLUMNS = ("Open", "High", "Low", "Close", "Volume")


class TrainingCancelled(RuntimeError):
    """A cancellation requested before a tabular checkpoint was published."""


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise TrainingCancelled("Model training cancelled")


def runtime_info(architecture):
    """Describe this CPU pipeline without importing any CUDA runtime."""
    if architecture not in ARCHITECTURES:
        raise ValueError("Unsupported tabular architecture")
    available, errors = {}, []
    for module, package in (("sklearn", "scikit-learn"), ("joblib", "joblib")) + (
            (("lightgbm", "lightgbm"),) if architecture == "spot-lightgbm-v1" else ()):
        try:
            imported = importlib.import_module(module)
            available[module + "_available"] = True
            available[module + "_version"] = str(getattr(imported, "__version__", "unknown"))
        except (ImportError, OSError):
            available[module + "_available"] = False
            errors.append(f"Install {package} to train this model (python -m pip install {package}).")
    return {**available, "available": not errors, "cpu_available": not errors,
            "device": "cpu", "device_name": "CPU", "cuda_available": False,
            "error": " ".join(errors) or None}


def validate_tabular_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("Model configuration must be an object")
    config = deepcopy(raw)
    architecture = config.get("architecture")
    if architecture not in ARCHITECTURES:
        raise ValueError("Unsupported tabular architecture")
    symbol = config.get("symbol")
    if not isinstance(symbol, str):
        raise ValueError("Use a USDT market such as btcusdt")
    symbol = symbol.strip().lower().replace("/", "")
    if not re.fullmatch(r"[a-z0-9]{2,20}usdt", symbol):
        raise ValueError("Use a USDT market such as btcusdt")
    config["symbol"] = symbol
    timeframe = config.get("timeframe")
    if architecture.startswith("spot-"):
        from spot_data import APPROVED_ASSETS
        if timeframe != "60":
            raise ValueError("Spot trainers require hourly (60) candles")
        if symbol[:-4].upper() not in APPROVED_ASSETS:
            raise ValueError("Spot trainers require approved assets quoted in USDT")
        config["horizon"] = 72
    else:
        if timeframe not in ("60", "240"):
            raise ValueError("PriceAI and AgenticAI require 1-hour and 4-hour primary candles")
        config["horizon"] = 4
    count = config.get("candles", 2000)
    if isinstance(count, bool) or not isinstance(count, int) or not 500 <= count <= 20000:
        raise ValueError("candles must be an integer from 500 to 20000")
    config["candles"] = count
    for key, default in (("fee_bps", 10), ("exit_fee_bps", 10), ("slippage_bps", 5)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value < 10000:
            raise ValueError(f"{key} must be finite and positive and less than 10000")
        config[key] = float(value)
    if architecture == "agentic-ai-mtf-hgb-v1":
        from agentic_ai import validate_timeframes
        config["specialist_timeframes"] = validate_timeframes(timeframe, config.get("specialist_timeframes"))
    config["device"] = "cpu"
    return config


def _frame(df, timeframe):
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise ValueError(f"{timeframe} candle history is empty")
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.hasnans:
        raise ValueError("Candles require valid datetime timestamps")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError("Candle timestamps must be sorted and unique")
    if any(column not in df for column in COLUMNS):
        raise ValueError("Candles require Open, High, Low, Close and Volume")
    result = df.loc[:, list(COLUMNS)].astype(float).copy()
    result.index = result.index.tz_localize("UTC") if result.index.tz is None else result.index.tz_convert("UTC")
    from agentic_ai import _validate_frame
    _validate_frame(result, timeframe)
    if (result.index + pd.Timedelta(seconds=SECONDS[timeframe]) > pd.Timestamp.now(tz="UTC")).any():
        raise ValueError("Candle history includes an incomplete candle")
    return result


def _hash(frame):
    return hashlib.sha256(pd.util.hash_pandas_object(frame, index=True).values.tobytes()).hexdigest()


def _execution(config):
    from execution_model import ExecutionConfig
    return ExecutionConfig(entry_fee_rate=config["fee_bps"] / 10000,
                           exit_fee_rate=config["exit_fee_bps"] / 10000,
                           slippage_bps=config["slippage_bps"])


def _boundaries(frame):
    fit_end, val_end = int(len(frame) * .7), int(len(frame) * .85)
    if min(val_end - fit_end, len(frame) - val_end) < 50:
        raise ValueError("Need at least 50 candles in separate validation and test windows")
    return fit_end, val_end


def _metadata(config, frame, fit_end, val_end):
    return {"architecture": config["architecture"], "model_version": config["architecture"],
            "symbol": config["symbol"], "timeframe": config["timeframe"], "source": "nobitex",
            "device": "cpu", "device_name": "CPU", "bot_compatible": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "horizon": config["horizon"], "horizon_candles": config["horizon"],
            "lookback": config.get("lookback", 96), "candle_count": len(frame),
            "training_window_start": frame.index[0].isoformat(),
            "training_window_end": frame.index[fit_end - 1].isoformat(),
            "validation_window_start": frame.index[fit_end].isoformat(),
            "validation_window_end": frame.index[val_end - 1].isoformat(),
            "test_window_start": frame.index[val_end].isoformat(),
            "test_window_end": frame.index[-1].isoformat(),
            "data_hash": _hash(frame), "training_data_hash": _hash(frame.iloc[:fit_end]),
            "execution": {"entry_fee_rate": config["fee_bps"] / 10000,
                          "exit_fee_rate": config["exit_fee_bps"] / 10000,
                          "slippage_bps": config["slippage_bps"]},
            "split_policy": "chronological 70/15/15; labels crossing each boundary purged; no test refit"}


def _price_metrics(model, frame, start, end):
    from price_ai import FEATURES, HORIZON_CANDLES, _features
    features = _features(frame)
    labels = 10000 * np.log(frame.Close.shift(-HORIZON_CANDLES) / frame.Close)
    positions = np.arange(len(frame))
    eligible = ((positions >= start) & (positions + HORIZON_CANDLES < end)
                & features.notna().all(axis=1).to_numpy() & np.isfinite(labels.to_numpy()))
    if not eligible.any():
        raise ValueError("Insufficient purged PriceAI evaluation examples")
    actual = labels.loc[eligible].to_numpy()
    estimates = model.estimator.predict(features.loc[eligible, list(FEATURES)])
    if not np.isfinite(estimates).all():
        raise ValueError("PriceAI produced a non-finite forecast")
    return {"rows": int(eligible.sum()), "mae_bps": float(np.abs(actual - estimates).mean()),
            "baseline_mae_bps": float(np.abs(actual).mean()),
            "sign_accuracy_pct": float((np.sign(actual) == np.sign(estimates)).mean() * 100)}


def _agent_probabilities(model, matrix):
    result = {}
    for direction in ("long", "short"):
        probabilities = []
        for tf, specialist in model.specialists.items():
            columns = [column for column in matrix if column.startswith(tf + "_")]
            raw = getattr(specialist, direction + "_model").predict_proba(matrix[columns])[:, 1]
            probabilities.append(getattr(specialist, direction + "_calibrator").predict_proba(raw.reshape(-1, 1))[:, 1])
        average = np.mean(probabilities, axis=0)
        result[direction] = getattr(model, "ensemble_" + direction + "_calibrator").predict_proba(average.reshape(-1, 1))[:, 1]
    return result


def _agent_metrics(model, matrix, frame, start, end, baseline_rates, execution):
    from agentic_ai import HORIZON, _cost_bps
    from sklearn.metrics import brier_score_loss
    target = 10000 * np.log(frame.Close.shift(-HORIZON) / frame.Close)
    positions = np.arange(len(frame))
    eligible = ((positions >= start) & (positions + HORIZON < end)
                & matrix.notna().all(axis=1).to_numpy() & np.isfinite(target.to_numpy()))
    if not eligible.any():
        raise ValueError("AgenticAI lacks purged evaluation examples with completed specialist history")
    probabilities = _agent_probabilities(model, matrix.loc[eligible])
    cost = _cost_bps(execution) + 10
    result = {"rows": int(eligible.sum())}
    for direction in ("long", "short"):
        actual = ((target.loc[eligible] > cost) if direction == "long" else (target.loc[eligible] < -cost)).astype(int)
        result[direction + "_brier"] = float(brier_score_loss(actual, probabilities[direction]))
        result[direction + "_baseline_brier"] = float(brier_score_loss(actual, np.full(len(actual), baseline_rates[direction])))
    return result


def _specialist_frames(frame, config, fetcher, cancel=None, progress=None):
    primary = config["timeframe"]
    frames = {primary: frame}
    for tf in config["specialist_timeframes"]:
        if tf == primary:
            continue
        _check_cancel(cancel)
        if progress:
            progress({"stage": "fetching", "detail": f"Fetching {tf} specialist history", "epoch": 0, "epochs": 1})
        # Extra older bars warm the 24-bar specialist features before the first
        # primary observation and retain distinct higher bars in its fit split.
        span = (frame.index[-1] - frame.index[0]).total_seconds() + SECONDS[primary]
        count = math.ceil(span / SECONDS[tf]) + 128
        source = _frame(fetcher(config["symbol"], tf, count), tf)
        _check_cancel(cancel)
        latest_close = frame.index[-1] + pd.Timedelta(seconds=SECONDS[primary])
        source = source.loc[source.index + pd.Timedelta(seconds=SECONDS[tf]) <= latest_close]
        if source.empty:
            raise ValueError(f"{tf} specialist has no completed candle available at the primary close")
        frames[tf] = source
    return frames


def _bars(frame, asset):
    return [{"asset": asset, "exchange": "nobitex", "quote": "USDT", "interval": "1h",
             "open_time": timestamp.isoformat(),
             "available_at": (timestamp + pd.Timedelta(hours=1)).isoformat(),
             "open": float(row.Open), "high": float(row.High), "low": float(row.Low),
             "close": float(row.Close), "volume_base": float(row.Volume)}
            for timestamp, row in frame.iterrows()]


def _spot_frames(frame, config, fetcher, cancel=None):
    from spot_research import feature_frame
    asset = config["symbol"][:-4].upper()
    context = frame if asset == "BTC" else _frame(fetcher("btcusdt", "60", len(frame) + 96), "60")
    _check_cancel(cancel)
    btc = feature_frame(_bars(context, "BTC"))
    features = btc if asset == "BTC" else feature_frame(_bars(frame, asset), btc)
    if features["btc_return_24"].iloc[-1:].isna().any():
        raise ValueError("BTC hourly context is missing at the selected asset's completed candle")
    return {asset: features}, context


def _spot_metrics(frames, start, end, method, models):
    from spot_research import HORIZONS, _matrix, forecast_quality
    metrics = forecast_quality(frames, start, end, method, models)
    if set(metrics) != {str(horizon) for horizon in HORIZONS}:
        raise ValueError("Insufficient purged evaluation history for every Spot horizon")
    for horizon in HORIZONS:
        rows = _matrix(frames, start, end, horizon, training=True)
        target = rows[f"label_{horizon}"].to_numpy()
        metrics[str(horizon)]["zero_downside_mae_bps"] = float(np.maximum(-target, 0).mean() * 10000)
    return metrics


def train_tabular(df, config, *, fetcher, progress=None, cancel=None):
    """Fit one symbol's original research pipeline and return a saved artifact.

    Cancellation is cooperative between feature preparation, fitting and
    scoring. The underlying sklearn/LightGBM fit call must finish before a
    cancellation can be observed; a cancelled fit never returns a checkpoint.
    """
    _check_cancel(cancel)
    config = validate_tabular_config(config)
    runtime = runtime_info(config["architecture"])
    if not runtime["available"]:
        raise ValueError(runtime["error"])
    frame = _frame(df, config["timeframe"])
    fit_end, val_end = _boundaries(frame)
    metadata = _metadata(config, frame, fit_end, val_end)
    architecture = config["architecture"]
    if progress:
        progress({"stage": "training", "epoch": 0, "epochs": 1, "detail": "Preparing chronological fit, validation and test windows"})
    checkpoint = {"schema_version": 1, "architecture": architecture, "config": config, "metadata": metadata}
    if architecture == "price-ai-hgbr-v1":
        from price_ai import FEATURES, train_price_model
        _check_cancel(cancel)
        model = train_price_model(frame, fit_end)
        _check_cancel(cancel)
        validation = _price_metrics(model, frame, fit_end, val_end)
        test = _price_metrics(model, frame, val_end, len(frame))
        metadata.update(feature_names=list(FEATURES), training_rows=model.metadata["training_rows"],
                        validation_rows=validation["rows"], test_rows=test["rows"],
                        validation_mae_bps=validation["mae_bps"], validation_baseline_mae_bps=validation["baseline_mae_bps"],
                        test_mae_bps=test["mae_bps"], baseline_mae_bps=test["baseline_mae_bps"],
                        validation_passed=validation["mae_bps"] < validation["baseline_mae_bps"],
                        validation_metrics=validation, test_metrics=test, pipeline_metadata=deepcopy(model.metadata),
                        target="next four completed candle close log return in basis points")
        checkpoint["model"] = model
    elif architecture == "agentic-ai-mtf-hgb-v1":
        from agentic_ai import HORIZON, _cost_bps, feature_matrix, train_agent
        frames = _specialist_frames(frame, config, fetcher, cancel, progress)
        _check_cancel(cancel)
        execution = _execution(config)
        model = train_agent(frames, config["timeframe"], config["specialist_timeframes"], execution, fit_end)
        _check_cancel(cancel)
        matrix = feature_matrix(frames, config["timeframe"], config["specialist_timeframes"])
        target = 10000 * np.log(frame.Close.shift(-HORIZON) / frame.Close)
        positions = np.arange(len(frame))
        cal_start, cal_end = int(fit_end * .68), int(fit_end * .83)
        eligible = ((positions >= cal_start) & (positions + HORIZON < cal_end)
                    & matrix.notna().all(axis=1).to_numpy() & np.isfinite(target.to_numpy()))
        cost = _cost_bps(execution) + 10
        rates = {"long": float((target.loc[eligible] > cost).mean()),
                 "short": float((target.loc[eligible] < -cost).mean())}
        validation = _agent_metrics(model, matrix, frame, fit_end, val_end, rates, execution)
        test = _agent_metrics(model, matrix, frame, val_end, len(frame), rates, execution)
        metadata.update(feature_names=list(matrix), specialist_timeframes=config["specialist_timeframes"],
                        training_rows=fit_end, validation_rows=validation["rows"], test_rows=test["rows"],
                        validation_metrics=validation, test_metrics=test, baseline_rates=rates,
                        validation_passed=all(validation[d + "_brier"] < validation[d + "_baseline_brier"] for d in ("long", "short")),
                        pipeline_metadata=deepcopy(model.metadata), target=model.metadata["target"],
                        context_training_hashes={tf: _hash(source.loc[source.index + pd.Timedelta(seconds=SECONDS[tf]) <= frame.index[fit_end]])
                                                 for tf, source in frames.items() if tf != config["timeframe"]})
        metadata.update({"validation_" + key: value for key, value in validation.items() if key != "rows"})
        metadata.update({"test_" + key: value for key, value in test.items() if key != "rows"})
        checkpoint["model"] = model
    else:
        from spot_research import HORIZONS, MODEL_FEATURES, fit_models
        frames, btc = _spot_frames(frame, config, fetcher, cancel)
        method = "lightgbm" if architecture == "spot-lightgbm-v1" else "hgb"
        # Spot indexes features by completion rather than opening timestamps.
        start = frame.index[0] + pd.Timedelta(hours=1)
        train_end = frame.index[fit_end] + pd.Timedelta(hours=1)
        validation_end = frame.index[val_end] + pd.Timedelta(hours=1)
        end = frame.index[-1] + pd.Timedelta(hours=2)
        _check_cancel(cancel)
        models, quality = fit_models(frames, start, train_end, method)
        _check_cancel(cancel)
        validation = _spot_metrics(frames, train_end, validation_end, method, models)
        test = _spot_metrics(frames, validation_end, end, method, models)
        longest = str(max(HORIZONS))
        metadata.update(feature_names=list(MODEL_FEATURES), horizons=list(HORIZONS), method=method,
                        training_rows=min(value["training_rows"] for value in quality.values()),
                        validation_rows=min(value["rows"] for value in validation.values()),
                        test_rows=min(value["rows"] for value in test.values()),
                        validation_metrics=validation, test_metrics=test,
                        validation_mae_bps=validation[longest]["return_mae_bps"],
                        validation_baseline_mae_bps=validation[longest]["zero_return_mae_bps"],
                        test_mae_bps=test[longest]["return_mae_bps"], baseline_mae_bps=test[longest]["zero_return_mae_bps"],
                        validation_passed=all(value["return_mae_bps"] < value["zero_return_mae_bps"] for value in validation.values()),
                        pipeline_metadata={str(horizon): value for horizon, value in quality.items()},
                        btc_context_training_hash=_hash(btc.loc[btc.index < frame.index[fit_end]]),
                        target="future hourly close return and downside magnitude for 4, 24 and 72 hours")
        checkpoint["models"] = models
    _check_cancel(cancel)
    if progress:
        progress({"stage": "training", "epoch": 1, "epochs": 1, "history": [],
                  "detail": "Fit complete; separate validation and test metrics recorded"})
    _check_cancel(cancel)
    return checkpoint


def _validate_checkpoint(checkpoint):
    if not isinstance(checkpoint, dict) or checkpoint.get("schema_version") != 1:
        raise ValueError("Unsupported tabular checkpoint schema")
    config = validate_tabular_config(checkpoint.get("config"))
    metadata = checkpoint.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("source") != "nobitex":
        raise ValueError("Unsupported tabular checkpoint metadata or source")
    if checkpoint.get("architecture") != config["architecture"]:
        raise ValueError("Checkpoint architecture and config disagree")
    for key in ("architecture", "symbol", "timeframe", "horizon"):
        if metadata.get(key) != config[key]:
            raise ValueError(f"Checkpoint {key} config and metadata disagree")
    if metadata.get("horizon_candles") != config["horizon"] or metadata.get("model_version") != config["architecture"]:
        raise ValueError("Checkpoint model version or horizon disagrees with config")
    if metadata.get("bot_compatible") is not False or metadata.get("device") != "cpu":
        raise ValueError("Invalid tabular checkpoint compatibility metadata")
    execution = {"entry_fee_rate": config["fee_bps"] / 10000,
                 "exit_fee_rate": config["exit_fee_bps"] / 10000,
                 "slippage_bps": config["slippage_bps"]}
    if metadata.get("execution") != execution:
        raise ValueError("Checkpoint execution costs disagree with config")
    architecture = config["architecture"]
    if architecture == "price-ai-hgbr-v1":
        from price_ai import FEATURES, TrainedPriceModel
        model = checkpoint.get("model")
        if not isinstance(model, TrainedPriceModel) or not callable(getattr(model.estimator, "predict", None)):
            raise ValueError("Invalid PriceAI fitted model payload")
        expected_features = list(FEATURES)
    elif architecture == "agentic-ai-mtf-hgb-v1":
        from agentic_ai import TrainedAgent
        from price_ai import FEATURES
        model = checkpoint.get("model")
        selected = config["specialist_timeframes"]
        if metadata.get("specialist_timeframes") != selected or not isinstance(model, TrainedAgent) or set(model.specialists) != set(selected):
            raise ValueError("Invalid AgenticAI specialist model payload")
        components = [getattr(model, "ensemble_" + direction + "_calibrator") for direction in ("long", "short")]
        components.extend(getattr(specialist, direction + "_" + role) for specialist in model.specialists.values()
                          for direction in ("long", "short") for role in ("model", "calibrator"))
        if any(not callable(getattr(component, "predict_proba", None)) for component in components):
            raise ValueError("Invalid AgenticAI probability model payload")
        expected_features = [tf + "_" + feature for tf in selected for feature in FEATURES]
    else:
        from spot_research import HORIZONS, MODEL_FEATURES
        models = checkpoint.get("models")
        if metadata.get("horizons") != list(HORIZONS) or not isinstance(models, dict) or set(models) != set(HORIZONS):
            raise ValueError("Invalid Spot horizon model payload")
        if metadata.get("method") != ("lightgbm" if architecture == "spot-lightgbm-v1" else "hgb"):
            raise ValueError("Checkpoint Spot method disagrees with architecture")
        if any(not isinstance(pair, (tuple, list)) or len(pair) != 2
               or any(not callable(getattr(estimator, "predict", None)) for estimator in pair) for pair in models.values()):
            raise ValueError("Invalid Spot fitted model payload")
        expected_features = list(MODEL_FEATURES)
    if metadata.get("feature_names") != expected_features:
        raise ValueError("Checkpoint feature names disagree with the model pipeline")
    return checkpoint


def save_checkpoint(checkpoint, path):
    """Atomically publish a generated joblib artifact at the account-owned path."""
    _validate_checkpoint(checkpoint)
    joblib = importlib.import_module("joblib")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".model-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            joblib.dump(checkpoint, stream, compress=3)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_checkpoint(path):
    """Load only private server-generated artifacts: joblib is pickle-based."""
    try:
        checkpoint = importlib.import_module("joblib").load(Path(path))
        return _validate_checkpoint(checkpoint)
    except Exception as exc:
        raise ValueError(f"Invalid tabular checkpoint: {exc}") from exc


def _target_times(timestamp, timeframe, horizon):
    step = pd.Timedelta(seconds=SECONDS[timeframe])
    target_close = (timestamp + (horizon + 1) * step).isoformat()
    return {"horizon": horizon, "horizon_candles": horizon,
            "target_candle_time": (timestamp + horizon * step).isoformat(),
            "target_close_time": target_close, "target_time": target_close}


def _price(last_price, return_bps):
    try:
        value = math.exp(math.log(last_price) + return_bps / 10000)
    except OverflowError as exc:
        raise ValueError("Model produced an out-of-range price forecast") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Model produced an invalid price forecast")
    return value


def predict_tabular(checkpoint, *, fetcher):
    """Forecast the latest completed candle using the artifact's own pipeline."""
    _validate_checkpoint(checkpoint)
    config = checkpoint["config"]
    architecture, timeframe = config["architecture"], config["timeframe"]
    count = 200 if architecture.startswith("spot-") else 128
    frame = _frame(fetcher(config["symbol"], timeframe, count), timeframe)
    timestamp = frame.index[-1]
    result = {"architecture": architecture, "symbol": config["symbol"], "timeframe": timeframe,
              "last_price": float(frame.Close.iloc[-1]), "candle_time": timestamp.isoformat(),
              "observed_close_time": (timestamp + pd.Timedelta(seconds=SECONDS[timeframe])).isoformat(),
              "source": "nobitex", "bot_compatible": False,
              "validation_passed": checkpoint["metadata"].get("validation_passed", False)}
    if architecture == "price-ai-hgbr-v1":
        from price_ai import FEATURES, _features
        features = _features(frame).iloc[[-1]][list(FEATURES)]
        if features.isna().any().any():
            raise ValueError("Insufficient completed history for PriceAI prediction")
        estimate = float(checkpoint["model"].estimator.predict(features)[0])
        result.update(_target_times(timestamp, timeframe, 4), forecast_return_bps=estimate,
                      predicted_price=_price(result["last_price"], estimate))
    elif architecture == "agentic-ai-mtf-hgb-v1":
        from agentic_ai import decide, feature_matrix, probability_evidence
        frames = _specialist_frames(frame, config, fetcher)
        matrix = feature_matrix(frames, timeframe, config["specialist_timeframes"])
        evidence = probability_evidence(checkpoint["model"], matrix.iloc[-1])
        result.update(_target_times(timestamp, timeframe, 4), evidence=evidence,
                      long_probability=evidence["agent"]["long_probability"],
                      short_probability=evidence["agent"]["short_probability"], decision=decide(evidence))
    else:
        from spot_research import _best_forecasts, forecasts_for
        frames, _ = _spot_frames(frame, config, fetcher)
        start = timestamp + pd.Timedelta(hours=1)
        raw = forecasts_for(frames, start, start + pd.Timedelta(hours=1),
                            checkpoint["metadata"]["method"], checkpoint["models"])
        asset = config["symbol"][:-4].upper()
        values = raw.get(start.isoformat(), {}).get(asset, {})
        if set(values) != {4, 24, 72}:
            raise ValueError("Insufficient completed history or BTC context for Spot prediction")
        forecasts = {}
        for horizon, prediction in values.items():
            estimate, downside = prediction["return"], prediction["downside"]
            if not math.isfinite(estimate) or not math.isfinite(downside) or estimate <= -1:
                raise ValueError("Spot model produced an invalid return or downside forecast")
            forecasts[str(horizon)] = {**prediction, "forecast_return_bps": estimate * 10000,
                                       "downside_bps": downside * 10000, "horizon_hours": horizon,
                                       "predicted_price": result["last_price"] * (1 + estimate),
                                       **_target_times(timestamp, timeframe, horizon)}
        best = _best_forecasts({asset: values})[asset]
        chosen = forecasts[str(best["horizon_hours"])]
        result.update(forecasts=forecasts, by_symbol={config["symbol"]: forecasts},
                      forecast_return_bps=chosen["forecast_return_bps"], predicted_price=chosen["predicted_price"],
                      downside_bps=chosen["downside_bps"], **_target_times(timestamp, timeframe, best["horizon_hours"]))
    return result
