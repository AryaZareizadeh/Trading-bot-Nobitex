"""CPU trainer artifacts use existing causal pipelines and survive serialization."""
from copy import deepcopy
import importlib
import math
import threading

import numpy as np
import pandas as pd
import pytest

import trainer_adapters as adapters


def candles(count=1200, frequency="h", seed=19):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, .006, count)))
    return pd.DataFrame({"Open": close, "High": close * 1.008,
                         "Low": close * .992, "Close": close,
                         "Volume": 100 + rng.random(count) * 20},
                        index=pd.date_range("2025-01-01", periods=count, freq=frequency, tz="UTC"))


def config(architecture="price-ai-hgbr-v1", **updates):
    return {"architecture": architecture, "symbol": "btcusdt", "timeframe": "60",
            "candles": 1200, **updates}


def forbidden_fetch(*args):
    raise AssertionError(f"Unexpected history fetch: {args}")


@pytest.fixture(scope="module")
def price_package():
    frame = candles()
    updates = []
    checkpoint = adapters.train_tabular(frame, config(), fetcher=forbidden_fetch, progress=updates.append)
    return frame, checkpoint, updates


def test_price_ai_actual_fit_save_load_predict(price_package, tmp_path):
    frame, checkpoint, updates = price_package
    metadata = checkpoint["metadata"]
    assert metadata["architecture"] == "price-ai-hgbr-v1"
    assert metadata["bot_compatible"] is False
    assert metadata["device"] == "cpu"
    assert metadata["training_rows"] >= 400
    assert metadata["test_rows"] > 100
    assert metadata["test_mae_bps"] >= 0
    assert metadata["baseline_mae_bps"] >= 0
    assert metadata["training_window_end"] < metadata["validation_window_start"]
    assert metadata["validation_window_end"] < metadata["test_window_start"]
    assert updates[-1]["epoch"] == updates[-1]["epochs"] == 1
    path = tmp_path / "model.joblib"
    adapters.save_checkpoint(checkpoint, path)
    loaded = adapters.load_checkpoint(path)
    prediction = adapters.predict_tabular(loaded, fetcher=lambda *args: frame.tail(args[2]))
    assert prediction["horizon"] == 4
    assert prediction["last_price"] == float(frame.Close.iloc[-1])
    assert prediction["predicted_price"] == pytest.approx(
        prediction["last_price"] * math.exp(prediction["forecast_return_bps"] / 10000))
    assert pd.Timestamp(prediction["target_close_time"]) - pd.Timestamp(prediction["observed_close_time"]) == pd.Timedelta(hours=4)
    assert list(tmp_path.glob(".model-*")) == []


def test_final_test_prices_cannot_change_price_model_training(price_package):
    frame, first, _ = price_package
    changed = frame.copy()
    changed.iloc[int(len(frame) * .85):, :4] *= 3
    second = adapters.train_tabular(changed, config(), fetcher=forbidden_fetch)
    from price_ai import _features
    row = _features(frame).iloc[[-1]]
    assert first["model"].estimator.predict(row).tolist() == second["model"].estimator.predict(row).tolist()
    assert first["metadata"]["training_data_hash"] == second["metadata"]["training_data_hash"]
    assert first["metadata"]["validation_mae_bps"] == second["metadata"]["validation_mae_bps"]
    assert first["metadata"]["test_mae_bps"] != second["metadata"]["test_mae_bps"]


@pytest.mark.parametrize("change,match", [
    ({"timeframe": "15"}, "1-hour and 4-hour"),
    ({"architecture": "unknown"}, "architecture"),
    ({"symbol": "../../btcusdt"}, "USDT"),
    ({"fee_bps": 0}, "positive"),
    ({"slippage_bps": float("nan")}, "positive"),
    ({"architecture": "spot-hgb-v1", "symbol": "dogeusdt"}, "approved"),
    ({"architecture": "spot-hgb-v1", "timeframe": "240"}, "hourly"),
    ({"architecture": "agentic-ai-mtf-hgb-v1", "specialist_timeframes": ["60"]}, "two or three"),
])
def test_invalid_family_config_rejected(change, match):
    with pytest.raises(ValueError, match=match):
        adapters.train_tabular(candles(), config(**change), fetcher=forbidden_fetch)


@pytest.mark.parametrize("alter,match", [
    (lambda frame: frame.drop(frame.index[300]), "gap"),
    (lambda frame: pd.concat([frame.iloc[:5], frame.iloc[4:]]), "unique|order"),
    (lambda frame: frame.assign(Volume=-1), "inconsistent"),
    (lambda frame: frame.assign(High=frame.Close * .5), "inconsistent"),
    (lambda frame: frame.set_axis(pd.date_range("2099-01-01", periods=len(frame), freq="h", tz="UTC")), "incomplete"),
])
def test_corrupt_or_unfinished_candles_rejected(alter, match):
    with pytest.raises(ValueError, match=match):
        adapters.train_tabular(alter(candles()), config(), fetcher=forbidden_fetch)


def test_cancellation_prevents_fitting_or_publishing():
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(adapters.TrainingCancelled):
        adapters.train_tabular(candles(), config(), fetcher=forbidden_fetch, cancel=cancelled.is_set)


def test_missing_dependency_has_actionable_error(monkeypatch):
    original = importlib.import_module
    def missing(name, *args, **kwargs):
        if name == "lightgbm":
            raise ImportError("dependency unavailable")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(adapters.importlib, "import_module", missing)
    with pytest.raises(ValueError, match="Install lightgbm"):
        adapters.train_tabular(candles(), config("spot-lightgbm-v1"), fetcher=forbidden_fetch)


def test_checkpoint_config_metadata_mismatch_rejected(price_package, tmp_path):
    _, checkpoint, _ = price_package
    bad = deepcopy(checkpoint)
    bad["metadata"]["symbol"] = "ethusdt"
    import joblib
    path = tmp_path / "bad.joblib"
    joblib.dump(bad, path)
    with pytest.raises(ValueError, match="symbol"):
        adapters.load_checkpoint(path)


def test_failed_atomic_save_preserves_existing_artifact(price_package, tmp_path, monkeypatch):
    _, checkpoint, _ = price_package
    import joblib
    path = tmp_path / "saved.joblib"
    path.write_bytes(b"existing artifact")
    def failed_dump(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(joblib, "dump", failed_dump)
    with pytest.raises(OSError, match="disk full"):
        adapters.save_checkpoint(checkpoint, path)
    assert path.read_bytes() == b"existing artifact"
    assert list(tmp_path.glob(".model-*")) == []


def test_cancellation_after_fit_returns_no_checkpoint(monkeypatch):
    import price_ai
    original = price_ai.train_price_model
    cancelled = threading.Event()
    def fit(*args, **kwargs):
        result = original(*args, **kwargs)
        cancelled.set()
        return result
    monkeypatch.setattr(price_ai, "train_price_model", fit)
    with pytest.raises(adapters.TrainingCancelled):
        adapters.train_tabular(candles(), config(), fetcher=forbidden_fetch, cancel=cancelled.is_set)


def test_spot_hgb_actual_fit_uses_original_features_and_btc_context(monkeypatch, tmp_path):
    import spot_research
    frame = candles()
    btc = candles(seed=42)
    calls, fits = [], []
    original = spot_research.fit_models
    def capture(frames, train_start, train_end, method):
        fits.append((frames, train_start, train_end, method))
        return original(frames, train_start, train_end, method)
    monkeypatch.setattr(spot_research, "fit_models", capture)
    def fetch(symbol, timeframe, count):
        calls.append((symbol, timeframe, count))
        return (btc if symbol == "btcusdt" else frame).tail(count)
    checkpoint = adapters.train_tabular(frame, config("spot-hgb-v1", symbol="ethusdt"), fetcher=fetch)
    assert fits[0][3] == "hgb"
    assert set(fits[0][0]) == {"ETH"}
    features = fits[0][0]["ETH"]
    assert features["btc_return_24"].iloc[-1] == pytest.approx(btc.Close.iloc[-1] / btc.Close.iloc[-25] - 1)
    assert checkpoint["metadata"]["horizons"] == [4, 24, 72]
    assert set(checkpoint["metadata"]["test_metrics"]) == {"4", "24", "72"}
    assert checkpoint["metadata"]["test_metrics"]["72"]["rows"] > 0
    adapters.save_checkpoint(checkpoint, tmp_path / "spot.joblib")
    prediction = adapters.predict_tabular(adapters.load_checkpoint(tmp_path / "spot.joblib"), fetcher=fetch)
    assert set(prediction["by_symbol"]) == {"ethusdt"}
    assert set(prediction["forecasts"]) == {"4", "24", "72"}
    assert all(math.isfinite(value["return"]) and value["downside"] > 0 for value in prediction["forecasts"].values())
    assert all(symbol == "btcusdt" and timeframe == "60" for symbol, timeframe, _ in calls[:1])


def test_agent_fetches_causal_higher_history_and_predicts_calibrated_probabilities(monkeypatch):
    import agentic_ai
    frame = candles(2200)
    higher = frame.resample("4h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
    original = agentic_ai.train_agent
    boundaries, fetches = [], []
    def capture(frames, primary, selected, execution, train_end=None):
        boundaries.append((train_end, selected, execution))
        return original(frames, primary, selected, execution, train_end)
    monkeypatch.setattr(agentic_ai, "train_agent", capture)
    def fetch(symbol, timeframe, count):
        fetches.append((symbol, timeframe, count))
        return (frame if timeframe == "60" else higher).tail(count)
    checkpoint = adapters.train_tabular(frame, config("agentic-ai-mtf-hgb-v1", candles=2200), fetcher=fetch)
    assert boundaries[0][0] == int(len(frame) * .7)
    assert boundaries[0][1] == ["60", "240"]
    assert boundaries[0][2].entry_fee_rate == .001
    assert fetches[0][1] == "240"
    assert checkpoint["metadata"]["test_long_brier"] >= 0
    assert checkpoint["metadata"]["test_long_baseline_brier"] >= 0
    prediction = adapters.predict_tabular(checkpoint, fetcher=fetch)
    assert set(prediction["evidence"]) == {"60", "240", "agent"}
    assert 0 <= prediction["long_probability"] <= 1
    assert 0 <= prediction["short_probability"] <= 1
    assert prediction["decision"]["action"] in {"enter_long", "enter_short", "abstain"}


def test_four_hour_agent_defaults_to_daily_specialist():
    cfg = adapters.validate_tabular_config(config("agentic-ai-mtf-hgb-v1", timeframe="240", candles=2200))
    assert cfg["specialist_timeframes"] == ["240", "D"]
