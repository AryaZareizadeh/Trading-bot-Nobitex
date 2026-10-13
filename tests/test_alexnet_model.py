"""Numerical and temporal acceptance checks for the optional CNN trainer."""
import importlib
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from alexnet_model import (ARCHITECTURE, TrainingCancelled, _features,
                           _window_indices, load_checkpoint, predict_alexnet,
                           runtime_info, save_checkpoint, train_alexnet)


CONFIG = {"symbol": "btcusdt", "timeframe": "60", "lookback": 64, "horizon": 4,
          "epochs": 2, "batch_size": 32, "device": "cpu", "seed": 42}


def candles(count=480):
    t = np.arange(count)
    close = 100 * np.exp(.00015 * t + .03 * np.sin(t / 13))
    return pd.DataFrame({"Open": close * .999, "High": close * 1.004,
                         "Low": close * .995, "Close": close,
                         "Volume": 100 + 10 * np.sin(t / 7)},
                        index=pd.date_range("2026-01-01", periods=count, freq="h"))


@pytest.fixture
def torch_runtime():
    torch = pytest.importorskip("torch")
    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    yield torch
    torch.set_num_threads(previous)


@pytest.fixture
def trained(torch_runtime):
    return train_alexnet(candles(), CONFIG)


def test_module_import_does_not_import_optional_torch():
    result = subprocess.run([sys.executable, "-c",
        "import sys; import alexnet_model; assert 'torch' not in sys.modules"],
        check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_runtime_reports_optional_dependency_failure(monkeypatch):
    module = importlib.import_module("alexnet_model")
    def unavailable():
        raise RuntimeError("PyTorch unavailable")
    monkeypatch.setattr(module, "_torch", unavailable)
    info = runtime_info()
    assert info["torch_available"] is False
    assert info["cuda_available"] is False
    assert "PyTorch" in info["error"]


@pytest.mark.parametrize("in_docker,environment", [(False, "python"), (True, "docker")])
def test_missing_torch_reports_the_server_environment(monkeypatch, in_docker, environment):
    import builtins
    module = importlib.import_module("alexnet_model")
    original_import = builtins.__import__
    def missing_import(name, *args, **kwargs):
        if name == "torch":
            raise ModuleNotFoundError("No module named 'torch'", name="torch")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", missing_import)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    original_is_file = module.Path.is_file
    monkeypatch.setattr(module.Path, "is_file", lambda path: in_docker if path.name == ".dockerenv" else original_is_file(path))
    info = runtime_info()
    assert info["torch_installed"] is False
    assert info["torch_available"] is False
    assert info["runtime_environment"] == environment
    assert info["python_executable"] == sys.executable
    assert "not installed" in info["error"]
    assert "server" in info["error"]


@pytest.mark.parametrize("failure", [
    OSError("[WinError 126] The specified module could not be found: torch.dll"),
    ImportError("numpy.core.multiarray failed to import"),
    ModuleNotFoundError("No module named 'typing_extensions'", name="typing_extensions"),
])
def test_installed_torch_load_failure_keeps_the_original_cause(monkeypatch, failure):
    import builtins
    original_import = builtins.__import__
    def failed_import(name, *args, **kwargs):
        if name == "torch":
            raise failure
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", failed_import)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    info = runtime_info()
    assert str(failure) in info["error"]
    assert info["torch_installed"] is True
    assert info["torch_available"] is False
    assert "not installed" not in info["error"]


@pytest.mark.parametrize("failing_probe", ["availability", "device_name"])
def test_cuda_probe_failure_preserves_cpu_and_never_advertises_cuda(monkeypatch, failing_probe):
    module = importlib.import_module("alexnet_model")
    def failed_probe(*args):
        raise RuntimeError("CUDA driver initialization failed")
    cuda = SimpleNamespace(is_available=failed_probe if failing_probe == "availability" else lambda: True,
                           get_device_name=failed_probe)
    torch = SimpleNamespace(__version__="2.8.0+cu129", version=SimpleNamespace(cuda="12.9"), cuda=cuda)
    monkeypatch.setattr(module, "_torch", lambda: torch)
    info = runtime_info()
    assert info["cuda_available"] is False
    assert info["device_name"] == "CPU"
    assert info["torch_installed"] is True
    assert info["torch_available"] is True
    assert "CUDA driver initialization failed" in info["error"]


def test_failed_package_discovery_does_not_hide_a_working_runtime(monkeypatch):
    module = importlib.import_module("alexnet_model")
    def failed_discovery(name):
        raise ValueError("torch.__spec__ is None")
    torch = SimpleNamespace(__version__="2.8.0", version=SimpleNamespace(cuda=None),
                            cuda=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setattr(importlib.util, "find_spec", failed_discovery)
    monkeypatch.setattr(module, "_torch", lambda: torch)
    info = runtime_info()
    assert info["torch_installed"] is True
    assert info["torch_available"] is True
    assert info["cuda_available"] is False
    assert info["device_name"] == "CPU"
    assert info["error"] is None


def test_unknown_package_discovery_is_not_reported_as_missing(monkeypatch):
    module = importlib.import_module("alexnet_model")
    def failed_discovery(name):
        raise ValueError("torch.__spec__ is None")
    def unavailable():
        raise RuntimeError("PyTorch failed to load")
    monkeypatch.setattr(importlib.util, "find_spec", failed_discovery)
    monkeypatch.setattr(module, "_torch", unavailable)
    info = runtime_info()
    assert info["torch_installed"] is None
    assert info["torch_available"] is False
    assert "failed to load" in info["error"]


def test_training_purges_split_labels_and_normalizes_training_only(trained):
    df = candles()
    metadata = trained["metadata"]
    fit_end, val_end, horizon = int(len(df) * .7), int(len(df) * .85), CONFIG["horizon"]
    assert metadata["architecture"] == ARCHITECTURE
    assert metadata["horizon"] == metadata["horizon_candles"] == horizon
    assert metadata["training_rows"] == fit_end - horizon - CONFIG["lookback"]
    assert metadata["validation_rows"] == val_end - fit_end - horizon
    assert metadata["test_rows"] == len(df) - val_end - horizon
    assert pd.Timestamp(metadata["training_window_end"]).tz_convert(None) == df.index[fit_end - 1]
    assert pd.Timestamp(metadata["validation_window_end"]).tz_convert(None) == df.index[val_end - 1]
    assert pd.Timestamp(metadata["test_window_start"]).tz_convert(None) == df.index[val_end]
    features = _features(df.to_numpy())
    np.testing.assert_allclose(trained["scaler"]["mean"], features[1:fit_end].mean(axis=0))
    np.testing.assert_allclose(trained["scaler"]["scale"], features[1:fit_end].std(axis=0))
    assert metadata["validation_passed"] == (metadata["validation_mae_bps"] < metadata["validation_baseline_mae_bps"])
    assert all(pd.Timestamp(row["candle_time"]).tz_convert(None) >= df.index[val_end]
               for row in trained["heldout_samples"])


def test_heldout_test_prices_cannot_change_weights_or_validation(torch_runtime):
    original = candles()
    changed = original.copy()
    changed.iloc[int(len(changed) * .85):, :4] *= 3
    first = train_alexnet(original, CONFIG)
    second = train_alexnet(changed, CONFIG)
    assert first["scaler"] == second["scaler"]
    for key in first["state_dict"]:
        assert torch_runtime.equal(first["state_dict"][key], second["state_dict"][key])
    for key in ("validation_mae_bps", "validation_baseline_mae_bps", "validation_passed", "best_epoch"):
        assert first["metadata"][key] == second["metadata"][key]
    assert first["metadata"]["test_mae_bps"] != second["metadata"]["test_mae_bps"]


def test_checkpoint_round_trip_and_predictions_are_causal(tmp_path, trained, torch_runtime):
    path = tmp_path / "model.pt"
    save_checkpoint(trained, path)
    loaded = load_checkpoint(path)
    assert loaded.metadata == trained["metadata"]
    assert all(tensor.device.type == "cpu" for tensor in trained["state_dict"].values())
    assert sum(isinstance(layer, torch_runtime.nn.Conv1d) for layer in loaded.network.modules()) == 5
    df = candles()
    before = predict_alexnet(loaded, df)
    assert len(before) == len(df)
    assert np.isnan(before[:CONFIG["lookback"]]).all()
    assert np.isfinite(before[CONFIG["lookback"]:]).all()
    tail = predict_alexnet(loaded, df.iloc[-CONFIG["lookback"] - 1:])
    assert tail[-1] == pytest.approx(before[-1], abs=.001)
    changed = df.copy()
    changed.iloc[400:, :4] *= 2
    after = predict_alexnet(loaded, changed)
    np.testing.assert_array_equal(before[:400], after[:400])
    sample = trained["heldout_samples"][-1]
    position = df.index.get_loc(pd.Timestamp(sample["candle_time"]).tz_convert(None))
    assert before[position] == pytest.approx(sample["forecast_return_bps"], abs=.001)
    assert sample["predicted_price"] == pytest.approx(sample["price"] * np.exp(sample["forecast_return_bps"] / 10000))


def test_missing_periods_are_skipped_and_never_filled(tmp_path, trained):
    df = candles().drop(candles().index[150])
    timestamps = df.index.tz_localize("UTC")
    eligible = _window_indices(timestamps, "60", 64, 4)
    # Includes the preceding close needed by the first feature in each window.
    assert all(not (i - 64 < 150 <= i + 4) for i in eligible)
    save_checkpoint(trained, tmp_path / "model.pt")
    prediction = predict_alexnet(load_checkpoint(tmp_path / "model.pt"), df)
    assert np.isnan(prediction[150:214]).all()
    assert np.isfinite(prediction[214:]).all()


def test_timestamp_storage_resolution_does_not_change_gap_detection():
    timestamps = candles().index.tz_localize("UTC")
    expected = _window_indices(timestamps, "60", 64, 4)
    np.testing.assert_array_equal(_window_indices(timestamps.as_unit("s"), "60", 64, 4), expected)


@pytest.mark.parametrize("case", ["duplicates", "unsorted", "nan", "zero", "negative_volume", "invalid_ohlc", "incomplete"])
def test_invalid_candles_rejected_before_torch(case):
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
    else:
        df.index = pd.date_range(pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=len(df) - 1), periods=len(df), freq="h")
    with pytest.raises(ValueError):
        train_alexnet(df, CONFIG)


def test_validation_and_test_unchanged_price_baselines_are_exact(trained):
    df = candles()
    horizon = CONFIG["horizon"]
    targets = np.log(df.Close.shift(-horizon) / df.Close) * 10000
    fit_end, val_end = int(len(df) * .7), int(len(df) * .85)
    assert trained["metadata"]["validation_baseline_mae_bps"] == pytest.approx(targets.iloc[fit_end:val_end - horizon].abs().mean())
    assert trained["metadata"]["baseline_mae_bps"] == pytest.approx(targets.iloc[val_end:-horizon].abs().mean())


def test_cancel_before_start_and_between_epochs(torch_runtime):
    with pytest.raises(TrainingCancelled):
        train_alexnet(candles(), CONFIG, cancel=lambda: True)
    updates = []
    with pytest.raises(TrainingCancelled):
        train_alexnet(candles(), CONFIG, progress=updates.append, cancel=lambda: bool(updates))
    assert len(updates) == 1
    assert updates[0]["epoch"] == 1
    assert updates[0]["history"][0]["train_loss"] >= 0


def test_cuda_request_never_silently_falls_back(torch_runtime, monkeypatch):
    monkeypatch.setattr(torch_runtime.cuda, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="CUDA is unavailable"):
        train_alexnet(candles(), {**CONFIG, "device": "cuda"})


@pytest.mark.parametrize("kind", ["garbage", "missing_scaler", "mismatched_timeframe", "nan_weights"])
def test_corrupt_checkpoint_is_a_clear_value_error(tmp_path, trained, torch_runtime, kind):
    path = tmp_path / "bad.pt"
    if kind == "garbage":
        path.write_bytes(b"not a torch checkpoint")
    else:
        if kind == "missing_scaler":
            trained.pop("scaler")
        elif kind == "mismatched_timeframe":
            trained["config"]["timeframe"] = "240"
        else:
            next(iter(trained["state_dict"].values())).view(-1)[0] = float("nan")
        save_checkpoint(trained, path)
    with pytest.raises(ValueError, match="Invalid AlexNet checkpoint"):
        load_checkpoint(path)


def test_actual_cuda_training_when_available(torch_runtime):
    if not torch_runtime.cuda.is_available():
        pytest.skip("No CUDA GPU available")
    checkpoint = train_alexnet(candles(), {**CONFIG, "device": "cuda", "epochs": 1})
    assert checkpoint["metadata"]["device"] == "cuda"
    assert checkpoint["metadata"]["device_name"]
    assert np.isfinite(checkpoint["metadata"]["test_mae_bps"])
    assert all(tensor.device.type == "cpu" for tensor in checkpoint["state_dict"].values())
