"""Temporal, numerical and serialization checks for candle path forecasts."""
import builtins
import copy
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

import transformer_model as model


CONFIG = {"symbol": "btcusdt", "timeframe": "60", "lookback": 100,
          "horizon": 10, "epochs": 2, "batch_size": 32,
          "learning_rate": .001, "device": "cpu", "seed": 42}


def candles(count=240):
    t = np.arange(count)
    close = 100 * np.exp(.00015 * t + .015 * np.sin(t / 13))
    return pd.DataFrame({"Open": close * .999, "High": close * 1.004,
                         "Low": close * .995, "Close": close,
                         "Volume": 100 + 10 * np.sin(t / 7)},
                        index=pd.date_range("2026-01-01", periods=count, freq="h"))


@pytest.fixture(scope="module")
def torch_runtime():
    torch = pytest.importorskip("torch")
    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    yield torch
    torch.set_num_threads(previous)


@pytest.fixture(scope="module")
def trained(torch_runtime):
    return model.train_transformer(candles(), CONFIG)


def test_import_leaves_optional_torch_unloaded():
    result = subprocess.run([sys.executable, "-c",
        "import sys; import transformer_model; assert 'torch' not in sys.modules"],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("failure", [
    ModuleNotFoundError("No module named 'torch'", name="torch"),
    OSError("Cannot load torch.dll"),
])
def test_optional_dependency_errors_are_actionable(monkeypatch, failure):
    original = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == "torch":
            raise failure
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    with pytest.raises(RuntimeError, match="PyTorch") as error:
        model.train_transformer(candles(), CONFIG)
    assert str(failure) in str(error.value) or "not installed" in str(error.value)


@pytest.mark.parametrize("case", ["duplicates", "unsorted", "nan", "zero", "negative_volume",
                                 "invalid_ohlc", "incomplete", "irregular_intervals"])
def test_bad_candles_rejected_before_optional_dependency(monkeypatch, case):
    df = candles()
    if case == "duplicates":
        df = pd.concat([df, df.iloc[[-1]]])
    elif case == "unsorted":
        df = df.iloc[::-1]
    elif case == "nan":
        df.iloc[10, 3] = np.nan
    elif case == "zero":
        df.iloc[10, 3] = 0
    elif case == "negative_volume":
        df.iloc[10, 4] = -1
    elif case == "invalid_ohlc":
        df.iloc[10, 1] = df.iloc[10, 2] / 2
    elif case == "incomplete":
        df.index = pd.date_range(pd.Timestamp.now(tz="UTC").floor("h"), periods=len(df), freq="h")
    else:
        df.index += pd.to_timedelta(np.arange(len(df)), unit="m")
    monkeypatch.setattr(model, "_torch", lambda: pytest.fail("Invalid input loaded torch"))
    with pytest.raises(ValueError):
        model.train_transformer(df, CONFIG)


@pytest.mark.parametrize("key,value", [("lookback", 99), ("horizon", 9), ("epochs", True),
                                       ("batch_size", 0), ("learning_rate", float("nan")),
                                       ("device", "gpu"), ("timeframe", "5"), ("seed", -1)])
def test_invalid_configuration_rejected(key, value):
    with pytest.raises(ValueError):
        model.train_transformer(candles(), {**CONFIG, key: value})


def test_features_round_trip_valid_candles_and_flat_zero_volume():
    df = candles()
    df.iloc[20, :4] = 110
    df.iloc[20, 4] = 0
    values = df.to_numpy()
    encoded = model._features(values)
    assert (encoded[1:, 2:] >= 0).all()
    decoded = model._candles_from_features(encoded[1:][None], values[:1, 3])[0]
    np.testing.assert_allclose(decoded, values[1:], rtol=1e-12, atol=1e-10)


def test_split_purges_whole_target_paths_and_scaler_is_training_only(trained):
    df = candles()
    fit_end, val_end = int(len(df) * .7), int(len(df) * .85)
    splits = model._split_indices(len(df), 100, 10)
    for name, lower, upper in (("fit", 0, fit_end), ("validation", fit_end, val_end),
                               ("test", val_end, len(df))):
        origins = splits[name]
        assert (origins + 1 >= lower).all()
        assert (origins + 10 < upper).all()
    metadata = trained["metadata"]
    assert metadata["architecture"] == "transformer-ohlcv-v1"
    assert metadata["training_rows"] == len(splits["fit"])
    assert metadata["validation_rows"] == len(splits["validation"])
    assert metadata["test_rows"] == len(splits["test"])
    expected = model._features(df.to_numpy())[1:fit_end]
    np.testing.assert_allclose(trained["scaler"]["mean"], expected.mean(axis=0))
    np.testing.assert_allclose(trained["scaler"]["scale"], np.maximum(expected.std(axis=0), 1e-8))
    assert metadata["evaluation_mode"] == "recursive"
    assert metadata["selection_metric"] == "validation_close_path_mae_bps"
    for split in ("validation", "test"):
        assert len(metadata["per_step_metrics"][split]) == 10
        assert [row["step"] for row in metadata["per_step_metrics"][split]] == list(range(1, 11))
        assert all(0 <= row["direction_accuracy"] <= 1 for row in metadata["per_step_metrics"][split])


def test_test_partition_cannot_change_training_or_selection(torch_runtime):
    df = candles()
    changed = df.copy()
    changed.iloc[int(len(df) * .85):, :4] *= 1.5
    first = model.train_transformer(df, {**CONFIG, "epochs": 1})
    second = model.train_transformer(changed, {**CONFIG, "epochs": 1})
    assert first["scaler"] == second["scaler"]
    assert all(torch_runtime.equal(first["state_dict"][key], second["state_dict"][key])
               for key in first["state_dict"])
    for key in ("validation_mae_bps", "validation_baseline_mae_bps", "best_epoch"):
        assert first["metadata"][key] == second["metadata"][key]
    assert first["metadata"]["test_mae_bps"] != second["metadata"]["test_mae_bps"]


def test_teacher_forcing_cannot_see_future_decoder_tokens(torch_runtime):
    network = model._network(torch_runtime).eval()
    source = torch_runtime.randn(1, 100, 5)
    decoder = torch_runtime.randn(1, 10, 5)
    changed = decoder.clone()
    changed[:, 5:] += 100
    with torch_runtime.no_grad():
        before = network(source, decoder)
        after = network(source, changed)
    torch_runtime.testing.assert_close(before[:, :5], after[:, :5], rtol=0, atol=0)


def test_recursive_rollout_uses_only_its_own_constrained_outputs(torch_runtime):
    class Recorder:
        def __init__(self):
            self.inputs = []
        def encode(self, source):
            return source
        def decode(self, tokens, memory):
            self.inputs.append(tokens.clone())
            result = torch_runtime.zeros_like(tokens)
            result[:, -1, 0] = len(self.inputs) / 100
            result[:, -1, 2:] = -10
            return result
    recorder = Recorder()
    source = torch_runtime.zeros(2, 100, 5)
    source[:, -1] = .25
    output = model._recursive_rollout(torch_runtime, recorder, source,
                                      np.zeros(5), np.ones(5), 10)
    assert output.shape == (2, 10, 5)
    assert len(recorder.inputs) == 10
    for step, inputs in enumerate(recorder.inputs):
        assert inputs.shape == (2, step + 1, 5)
        torch_runtime.testing.assert_close(inputs[:, 0], source[:, -1])
        if step:
            torch_runtime.testing.assert_close(inputs[:, 1:], output[:, :step])
    assert (output[:, :, 2:] >= 0).all()


def test_cpu_train_roundtrip_and_prediction_contract(tmp_path, trained, torch_runtime):
    path = tmp_path / "model.pt"
    model.save_checkpoint(trained, path)
    loaded = model.load_checkpoint(path)
    assert loaded.metadata == trained["metadata"]
    assert all(tensor.device.type == "cpu" for tensor in trained["state_dict"].values())
    assert len(loaded.network.encoder.layers) == len(loaded.network.decoder.layers) == 2
    df = candles()
    forecast = model.predict_transformer(loaded, df)
    tail = model.predict_transformer(loaded, df.iloc[-101:])
    assert forecast == tail
    assert len(forecast) == 10
    for step, row in enumerate(forecast, 1):
        assert set(row) == {"time", "open", "high", "low", "close", "volume"}
        assert pd.Timestamp(row["time"]) == df.index[-1].tz_localize("UTC") + pd.Timedelta(hours=step)
        assert all(np.isfinite(row[key]) for key in ("open", "high", "low", "close", "volume"))
        assert 0 < row["low"] <= min(row["open"], row["close"])
        assert row["high"] >= max(row["open"], row["close"])
        assert row["volume"] >= 0
    with pytest.raises(ValueError, match="101"):
        model.predict_transformer(loaded, df.iloc[-100:])
    # Equivalent timestamp resolutions and timezone representations have equal forecasts.
    zoned = df.copy()
    zoned.index = zoned.index.tz_localize("UTC").tz_convert("Asia/Tehran").as_unit("s")
    assert model.predict_transformer(loaded, zoned) == forecast


def test_nobitex_daily_calendar_train_and_prediction(tmp_path, torch_runtime):
    df = candles()
    df.index = pd.date_range("2025-01-01", periods=len(df), freq="D", tz="Asia/Tehran")
    checkpoint = model.train_transformer(df, {**CONFIG, "timeframe": "D", "epochs": 1})
    path = tmp_path / "daily-model.pt"
    model.save_checkpoint(checkpoint, path)
    loaded = model.load_checkpoint(path)
    forecast = model.predict_transformer(loaded, df.iloc[-101:])
    utc = df.copy()
    utc.index = utc.index.tz_convert("UTC")
    assert model.predict_transformer(loaded, utc.iloc[-101:]) == forecast
    assert len(forecast) == 10
    for step, candle in enumerate(forecast, 1):
        opened = pd.Timestamp(candle["time"])
        assert opened == df.index[-1] + pd.Timedelta(days=step)
        assert opened.hour == 20 and opened.minute == 30
        assert opened.tz_convert("Asia/Tehran").hour == 0


def test_extreme_features_always_decode_finite_valid_ohlcv():
    raw = np.array([[[100, -100, -100, 100, 100]] * 10], dtype=float)
    predicted = model._candles_from_features(raw, np.array([1e300]))
    assert np.isfinite(predicted).all()
    assert (predicted[:, :, :4] > 0).all()
    assert (predicted[:, :, 1] >= predicted[:, :, [0, 3]].max(axis=2)).all()
    assert (predicted[:, :, 2] <= predicted[:, :, [0, 3]].min(axis=2)).all()
    assert (predicted[:, :, 4] >= 0).all()


def test_gap_windows_include_previous_close_and_forecast_checks_only_tail(tmp_path, trained):
    df = candles(400).drop(candles(400).index[150])
    eligible = model._window_indices(df.index.tz_localize("UTC"), "60", 100, 10)
    assert all(not (origin - 100 < 150 <= origin + 10) for origin in eligible)
    np.testing.assert_array_equal(model._window_indices(df.index.tz_localize("UTC").as_unit("s"), "60"), eligible)
    model.save_checkpoint(trained, tmp_path / "model.pt")
    loaded = model.load_checkpoint(tmp_path / "model.pt")
    assert len(model.predict_transformer(loaded, df)) == 10
    with pytest.raises(ValueError, match="gap-free"):
        model.predict_transformer(loaded, df.iloc[:200])


def test_cancel_before_dependency_loading_and_after_progress(monkeypatch, torch_runtime):
    original = model._torch
    monkeypatch.setattr(model, "_torch", lambda: pytest.fail("Cancelled run loaded torch"))
    with pytest.raises(model.TrainingCancelled):
        model.train_transformer(candles(), CONFIG, cancel=lambda: True)
    monkeypatch.setattr(model, "_torch", original)
    updates = []
    with pytest.raises(model.TrainingCancelled):
        model.train_transformer(candles(), CONFIG, progress=updates.append, cancel=lambda: bool(updates))
    assert len(updates) == 1
    assert updates[0]["history"][0]["train_loss"] >= 0


def test_cuda_request_never_silently_falls_back(torch_runtime, monkeypatch):
    monkeypatch.setattr(torch_runtime.cuda, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="CUDA is unavailable"):
        model.train_transformer(candles(), {**CONFIG, "device": "cuda"})


def test_actual_cuda_training_roundtrips_to_cpu_inference(tmp_path, torch_runtime):
    if not torch_runtime.cuda.is_available():
        pytest.skip("No CUDA GPU available")
    checkpoint = model.train_transformer(candles(), {**CONFIG, "device": "cuda", "epochs": 1})
    assert checkpoint["metadata"]["device"] == "cuda"
    assert checkpoint["metadata"]["device_name"]
    assert np.isfinite(checkpoint["metadata"]["test_mae_bps"])
    assert all(tensor.device.type == "cpu" for tensor in checkpoint["state_dict"].values())
    path = tmp_path / "cuda-model.pt"
    model.save_checkpoint(checkpoint, path)
    loaded = model.load_checkpoint(path)
    assert next(loaded.network.parameters()).device.type == "cpu"
    assert len(model.predict_transformer(loaded, candles().iloc[-101:])) == 10


@pytest.mark.parametrize("kind", ["garbage", "schema", "architecture", "missing_scaler",
                                 "negative_scale", "mismatched_timeframe", "nan_weights"])
def test_corrupt_checkpoints_are_clear_value_errors(tmp_path, trained, kind):
    path = tmp_path / "bad.pt"
    checkpoint = copy.deepcopy(trained)
    if kind == "garbage":
        path.write_bytes(b"not a torch checkpoint")
    else:
        if kind == "schema":
            checkpoint["schema_version"] = 999
        elif kind == "architecture":
            checkpoint["metadata"]["architecture"] = "other"
        elif kind == "missing_scaler":
            checkpoint.pop("scaler")
        elif kind == "negative_scale":
            checkpoint["scaler"]["scale"][0] = -1
        elif kind == "mismatched_timeframe":
            checkpoint["config"]["timeframe"] = "240"
        else:
            next(iter(checkpoint["state_dict"].values())).view(-1)[0] = float("nan")
        model.save_checkpoint(checkpoint, path)
    with pytest.raises(ValueError, match="Invalid Transformer checkpoint"):
        model.load_checkpoint(path)


def test_failed_atomic_publication_keeps_previous_checkpoint(tmp_path, trained, monkeypatch):
    path = tmp_path / "model.pt"
    model.save_checkpoint(trained, path)
    previous = path.read_bytes()
    def failed_replace(*args):
        raise OSError("replacement unavailable")
    monkeypatch.setattr(model.os, "replace", failed_replace)
    with pytest.raises(OSError, match="replacement unavailable"):
        model.save_checkpoint(trained, path)
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]
