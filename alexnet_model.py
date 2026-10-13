"""AlexNet-inspired 1D OHLCV regression with causal, held-out evaluation.

PyTorch is optional and imported only when the trainer or a saved model is used.
The five convolutional layers retain AlexNet's pattern with a compact regression
head suitable for a 4 GB GPU. This is not the original image classifier.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
import math
import os
from pathlib import Path
import sys
import tempfile

import numpy as np
import pandas as pd


ARCHITECTURE = "alexnet1d-v1"
SECONDS = {"15": 900, "60": 3600, "240": 14400, "D": 86400}
COLUMNS = ("Open", "High", "Low", "Close", "Volume")
FEATURES = ("open_log_ratio", "high_log_ratio", "low_log_ratio", "close_log_return", "log_volume")


class TrainingCancelled(RuntimeError):
    """A cooperative cancellation requested before a checkpoint was published."""


def _torch():
    try:
        import torch
    except (ImportError, OSError) as exc:
        if isinstance(exc, ModuleNotFoundError) and exc.name == "torch":
            message = ("PyTorch is not installed in this server's Python environment. "
                       "Install the optional model training requirements in that environment.")
        else:
            message = (f"PyTorch failed to load: {type(exc).__name__}: {exc}. "
                       "Repair the training dependencies in this server's Python environment.")
        raise RuntimeError(message) from exc
    return torch


def runtime_info() -> dict:
    result = dict(torch_available=False, cuda_available=False, device_name=None,
                  torch_version=None, cuda_version=None, error=None,
                  torch_installed=None, python_executable=sys.executable,
                  runtime_environment="docker" if Path("/.dockerenv").is_file() else "python")
    try:
        result["torch_installed"] = importlib.util.find_spec("torch") is not None
    except (ImportError, ValueError):
        # A custom importer or an already loaded module may have no usable spec.
        # Importing the package is still the authoritative availability check.
        pass
    try:
        torch = _torch()
        result.update(torch_installed=True, torch_available=True,
                      torch_version=str(torch.__version__), cuda_version=torch.version.cuda,
                      device_name="CPU")
    except Exception as exc:
        result["error"] = str(exc)[:300]
        return result
    try:
        if torch.cuda.is_available():
            device_name = torch.cuda.get_device_name(0)
            result.update(cuda_available=True, device_name=device_name)
    except Exception as exc:
        result["error"] = f"CUDA detection failed: {exc}"[:300]
    return result


def _network(torch):
    nn = torch.nn

    class AlexNet1D(nn.Module):
        def __init__(self):
            super().__init__()
            self.features = nn.Sequential(
                nn.Conv1d(5, 32, kernel_size=11, stride=4, padding=5), nn.ReLU(),
                nn.MaxPool1d(kernel_size=3, stride=2),
                nn.Conv1d(32, 64, kernel_size=5, padding=2), nn.ReLU(),
                nn.MaxPool1d(kernel_size=3, stride=2),
                nn.Conv1d(64, 96, kernel_size=3, padding=1), nn.ReLU(),
                nn.Conv1d(96, 96, kernel_size=3, padding=1), nn.ReLU(),
                nn.Conv1d(96, 64, kernel_size=3, padding=1), nn.ReLU(),
                nn.MaxPool1d(kernel_size=3, stride=2), nn.AdaptiveAvgPool1d(2),
            )
            self.regressor = nn.Sequential(nn.Flatten(), nn.Dropout(.2),
                                           nn.Linear(128, 64), nn.ReLU(), nn.Linear(64, 1))

        def forward(self, values):
            return self.regressor(self.features(values)).squeeze(-1)

    return AlexNet1D()


def _config(raw) -> dict:
    config = {"symbol": "", "timeframe": "60", "lookback": 96, "horizon": 4,
              "epochs": 10, "batch_size": 32, "learning_rate": .001,
              "device": "auto", "seed": 42, **dict(raw)}
    for key, lower, upper in (("lookback", 64, 256), ("horizon", 1, 24),
                              ("epochs", 1, 500), ("batch_size", 1, 512),
                              ("seed", 0, 4294967295)):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or not lower <= value <= upper:
            raise ValueError(f"{key} must be an integer between {lower} and {upper}")
        config[key] = int(value)
    if config["timeframe"] not in SECONDS:
        raise ValueError("AlexNet supports 15m, 1h, 4h and 1d candles")
    if config["device"] not in {"cuda", "cpu", "auto"}:
        raise ValueError("device must be cuda, cpu or auto")
    rate = float(config["learning_rate"])
    if not math.isfinite(rate) or not 0 < rate <= .1:
        raise ValueError("learning_rate must be positive and at most 0.1")
    config["learning_rate"] = rate
    if not isinstance(config["symbol"], str):
        raise ValueError("symbol must be a string")
    config["symbol"] = config["symbol"].lower()
    return config


def _validate_frame(df, timeframe):
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise ValueError("Candle history is empty")
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.hasnans:
        raise ValueError("Candles require valid datetime timestamps")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError("Candle timestamps must be sorted and unique")
    if any(column not in df for column in COLUMNS):
        raise ValueError("Candles require Open, High, Low, Close and Volume")
    values = df[list(COLUMNS)].to_numpy(dtype=np.float64)
    if not np.isfinite(values).all():
        raise ValueError("Candles contain non-finite OHLCV values")
    if (values[:, :4] <= 0).any() or (values[:, 4] < 0).any():
        raise ValueError("Candles contain nonpositive prices or negative volume")
    if (values[:, 1] < values[:, [0, 2, 3]].max(axis=1)).any() or (values[:, 2] > values[:, [0, 1, 3]].min(axis=1)).any():
        raise ValueError("Candles contain inconsistent OHLCV values")
    timestamps = df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
    if (timestamps + pd.Timedelta(seconds=SECONDS[timeframe]) > pd.Timestamp.now(tz="UTC")).any():
        raise ValueError("Candle history includes an incomplete candle")
    return values, timestamps


def _features(values):
    previous = np.r_[values[0, 0], values[:-1, 3]]
    return np.column_stack((np.log(values[:, :4]) - np.log(previous[:, None]),
                            np.log1p(values[:, 4])))


def _window_indices(timestamps, timeframe, lookback, horizon=0):
    """Keep only contiguous input windows and (when training) future labels."""
    size = len(timestamps)
    # A gap at row j invalidates any window whose start is before j <= end.
    differences = np.diff(timestamps.as_unit("ns").asi8)
    gap_count = np.r_[0, np.cumsum(differences != SECONDS[timeframe] * 1_000_000_000)]
    # A full window needs the preceding close as well as lookback feature rows.
    # Exclude the synthetic feature row at index zero, including in tail fetches.
    indices = np.arange(lookback, size - horizon, dtype=np.int64)
    # The first feature row also uses its preceding close. Include that one
    # causal dependency when a window starts just after a missing candle.
    starts = np.maximum(indices - lookback, 0)
    return indices[gap_count[indices + horizon] == gap_count[starts]]


def _iso(timestamp):
    return timestamp.isoformat()


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise TrainingCancelled("Model training cancelled")


def train_alexnet(df: pd.DataFrame, config: dict, *, progress=None, cancel=None) -> dict:
    """Select weights using validation only; report a later untouched test block.

    Labels across either chronological boundary are purged. Normalization is
    fitted only on the first 70% of candle history. Training examples may shuffle
    inside that block, but validation and test candles never enter model fitting.
    """
    config = _config(config)
    values, timestamps = _validate_frame(df, config["timeframe"])
    _check_cancel(cancel)
    n, lookback, horizon = len(df), config["lookback"], config["horizon"]
    fit_end, val_end = int(n * .70), int(n * .85)
    eligible = _window_indices(timestamps, config["timeframe"], lookback, horizon)
    fit = eligible[eligible + horizon < fit_end]
    validation = eligible[(eligible >= fit_end) & (eligible + horizon < val_end)]
    test = eligible[eligible >= val_end]
    if len(fit) < 32 or min(len(validation), len(test)) < 8:
        raise ValueError("History needs at least 32 training and 8 validation/test contiguous examples after purging; increase candles")
    features = _features(values)
    mean, scale = features[1:fit_end].mean(axis=0), features[1:fit_end].std(axis=0)
    scale = np.maximum(scale, 1e-8)
    normalized = ((features - mean) / scale).astype(np.float32)
    targets = np.full(n, np.nan, dtype=np.float64)
    targets[:-horizon] = (np.log(values[horizon:, 3]) - np.log(values[:-horizon, 3])) * 10000
    target_mean, target_scale = float(targets[fit].mean()), max(float(targets[fit].std()), 1.0)
    target_normalized = ((targets - target_mean) / target_scale).astype(np.float32)
    torch = _torch()
    requested = config["device"]
    if requested == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable. Install a CUDA-enabled PyTorch build and NVIDIA driver, or select CPU.")
    device = "cuda" if requested != "cpu" and torch.cuda.is_available() else "cpu"
    torch.manual_seed(config["seed"])
    if device == "cuda":
        torch.cuda.manual_seed_all(config["seed"])

    class Windows(torch.utils.data.Dataset):
        def __init__(self, indices):
            self.indices = indices

        def __len__(self):
            return len(self.indices)

        def __getitem__(self, item):
            i = int(self.indices[item])
            return np.ascontiguousarray(normalized[i - lookback + 1:i + 1].T), target_normalized[i]

    loaders = {name: torch.utils.data.DataLoader(Windows(indices), batch_size=config["batch_size"],
                shuffle=name == "fit", num_workers=0, pin_memory=device == "cuda")
               for name, indices in (("fit", fit), ("validation", validation), ("test", test))}
    network = _network(torch).to(device)
    optimizer = torch.optim.Adam(network.parameters(), lr=config["learning_rate"])
    loss_fn = torch.nn.MSELoss()

    def forecasts(loader):
        network.eval()
        batches = []
        with torch.no_grad():
            for x, _ in loader:
                _check_cancel(cancel)
                batches.append(network(x.to(device)).detach().cpu().numpy() * target_scale + target_mean)
        result = np.concatenate(batches).astype(np.float64)
        if not np.isfinite(result).all():
            raise RuntimeError("Training produced non-finite forecasts; reduce the learning rate")
        return result

    history, best_state, best_mae, best_epoch = [], None, float("inf"), None
    try:
        for epoch in range(1, config["epochs"] + 1):
            _check_cancel(cancel)
            network.train()
            loss_sum = 0.0
            for x, y in loaders["fit"]:
                _check_cancel(cancel)
                optimizer.zero_grad(set_to_none=True)
                loss = loss_fn(network(x.to(device)), y.to(device))
                if not torch.isfinite(loss):
                    raise RuntimeError("Training produced a non-finite loss; reduce the learning rate")
                loss.backward()
                torch.nn.utils.clip_grad_norm_(network.parameters(), max_norm=5)
                optimizer.step()
                loss_sum += float(loss.detach().cpu()) * len(x)
            val_predictions = forecasts(loaders["validation"])
            val_mae = float(np.mean(np.abs(val_predictions - targets[validation])))
            record = {"epoch": epoch, "train_loss": loss_sum / len(fit),
                      "val_loss": float(np.mean(((val_predictions - targets[validation]) / target_scale) ** 2)),
                      "validation_mae_bps": val_mae}
            history.append(record)
            if val_mae < best_mae:
                best_mae, best_epoch = val_mae, epoch
                best_state = {key: tensor.detach().cpu().clone() for key, tensor in network.state_dict().items()}
            if progress is not None:
                progress({"stage": "training", "epoch": epoch, "epochs": config["epochs"],
                          **record, "history": [dict(row) for row in history]})
        _check_cancel(cancel)
        network.load_state_dict(best_state)
        test_predictions = forecasts(loaders["test"])
        test_mae = float(np.mean(np.abs(test_predictions - targets[test])))
        baseline = float(np.mean(np.abs(targets[test])))
        val_baseline = float(np.mean(np.abs(targets[validation])))
        metadata = {
            "architecture": ARCHITECTURE, "model_version": ARCHITECTURE,
            "symbol": config["symbol"].lower(), "timeframe": config["timeframe"], "source": "nobitex",
            "lookback": lookback, "horizon": horizon, "horizon_candles": horizon,
            "device": device, "device_name": torch.cuda.get_device_name(0) if device == "cuda" else "CPU",
            "torch_version": str(torch.__version__), "cuda_version": torch.version.cuda,
            "feature_names": list(FEATURES), "target": "future close log return in basis points",
            "created_at": datetime.now(timezone.utc).isoformat(), "best_epoch": best_epoch,
            "epochs": config["epochs"], "training_rows": len(fit), "validation_rows": len(validation),
            "test_rows": len(test), "skipped_gap_windows": n - horizon - lookback - len(eligible),
            "training_window_start": _iso(timestamps[0]), "training_window_end": _iso(timestamps[fit_end - 1]),
            "validation_window_start": _iso(timestamps[fit_end]), "validation_window_end": _iso(timestamps[val_end - 1]),
            "test_window_start": _iso(timestamps[val_end]), "test_window_end": _iso(timestamps[-1]),
            "validation_mae_bps": best_mae, "validation_baseline_mae_bps": val_baseline,
            "test_mae_bps": test_mae, "baseline_mae_bps": baseline,
            "validation_passed": bool(best_mae < val_baseline), "history": history,
            "data_hash": hashlib.sha256(pd.util.hash_pandas_object(df[list(COLUMNS)], index=True).values.tobytes()).hexdigest(),
        }
        # A bounded preview only: metrics above use the entire untouched test set.
        def predicted_price(i, prediction):
            try:
                price = math.exp(math.log(float(values[i, 3])) + float(prediction) / 10000)
            except OverflowError as exc:
                raise RuntimeError("Model produced an out-of-range held-out price forecast") from exc
            if not math.isfinite(price) or price <= 0:
                raise RuntimeError("Model produced an invalid held-out price forecast")
            return price

        heldout = [{"candle_time": _iso(timestamps[i]), "forecast_return_bps": float(prediction),
                    "actual_return_bps": float(targets[i]), "price": float(values[i, 3]),
                    "predicted_price": predicted_price(i, prediction),
                    "actual_price": float(values[i + horizon, 3])}
                   for i, prediction in zip(test[-100:], test_predictions[-100:])]
        return {"schema_version": 1, "state_dict": best_state, "config": config, "metadata": metadata,
                "scaler": {"mean": mean.tolist(), "scale": scale.tolist(),
                           "target_mean": target_mean, "target_scale": target_scale},
                "heldout_samples": heldout}
    finally:
        # Drop active CUDA tensors after training, including a cancelled run.
        del loaders, optimizer, network
        if device == "cuda":
            torch.cuda.empty_cache()


def save_checkpoint(checkpoint: dict, path) -> None:
    """Publish a generated checkpoint atomically; caller owns its private path."""
    torch = _torch()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".model-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            torch.save(checkpoint, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass
class LoadedAlexNet:
    network: object
    config: dict
    metadata: dict
    scaler: dict


def load_checkpoint(path) -> LoadedAlexNet:
    torch = _torch()
    try:
        package = torch.load(Path(path), map_location="cpu", weights_only=True)
        if not isinstance(package, dict) or package.get("schema_version") != 1:
            raise ValueError("Unsupported AlexNet checkpoint schema")
        metadata = package.get("metadata", {})
        if metadata.get("architecture") != ARCHITECTURE or metadata.get("source") != "nobitex":
            raise ValueError("Unsupported AlexNet architecture or source")
        config = _config(package["config"])
        for key in ("symbol", "timeframe", "lookback", "horizon"):
            if metadata.get(key) != config[key]:
                raise ValueError(f"Checkpoint {key} config and metadata disagree")
        if metadata.get("horizon_candles") != config["horizon"]:
            raise ValueError("Checkpoint horizon_candles config and metadata disagree")
        scaler = package["scaler"]
        for name in ("mean", "scale"):
            values = np.asarray(scaler[name], dtype=float)
            if values.shape != (5,) or not np.isfinite(values).all() or (name == "scale" and (values <= 0).any()):
                raise ValueError("Invalid AlexNet feature scaler")
        if not math.isfinite(float(scaler["target_mean"])) or not math.isfinite(float(scaler["target_scale"])) or scaler["target_scale"] <= 0:
            raise ValueError("Invalid AlexNet target scaler")
        network = _network(torch)
        network.load_state_dict(package["state_dict"], strict=True)
        if not all(torch.isfinite(value).all() for value in network.state_dict().values()):
            raise ValueError("Checkpoint contains non-finite weights")
        network.eval()
        return LoadedAlexNet(network, config, metadata, scaler)
    except Exception as exc:
        raise ValueError(f"Invalid AlexNet checkpoint: {exc}") from exc


def predict_alexnet(model: LoadedAlexNet, df: pd.DataFrame) -> np.ndarray:
    """Causal forecasts in basis points aligned with each completed input candle.

    NaN marks an unavailable input window, including one spanning a candle gap.
    Inference runs on CPU so training can use the GPU independently.
    """
    config, scaler = model.config, model.scaler
    values, timestamps = _validate_frame(df, config["timeframe"])
    indices = _window_indices(timestamps, config["timeframe"], config["lookback"])
    result = np.full(len(df), np.nan, dtype=np.float64)
    if not len(indices):
        return result
    normalized = ((_features(values) - np.asarray(scaler["mean"])) / np.asarray(scaler["scale"])).astype(np.float32)
    torch = _torch()
    model.network.eval()
    with torch.no_grad():
        for offset in range(0, len(indices), 128):
            batch_indices = indices[offset:offset + 128]
            windows = np.stack([normalized[i - config["lookback"] + 1:i + 1].T for i in batch_indices])
            predictions = model.network(torch.from_numpy(windows)).numpy().astype(np.float64)
            result[batch_indices] = predictions * scaler["target_scale"] + scaler["target_mean"]
    if not np.isfinite(result[indices]).all():
        raise RuntimeError("AlexNet produced non-finite predictions")
    return result
