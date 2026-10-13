"""A compact causal encoder/decoder forecasting ten complete OHLCV candles.

The decoder learns with shifted teacher forcing. Model selection, held-out
metrics and inference always generate the entire path recursively from the last
completed candle. PyTorch stays optional until training or model loading.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

from alexnet_model import TrainingCancelled, _torch


ARCHITECTURE = "transformer-ohlcv-v1"
SECONDS = {"15": 900, "60": 3600, "240": 14400, "D": 86400}
COLUMNS = ("Open", "High", "Low", "Close", "Volume")
FEATURES = ("open_gap_log_return", "body_log_return", "upper_log_wick",
            "lower_log_wick", "log_volume")
# Bounded recursive tokens prevent runaway price/volume exponentiation. These
# bounds are also applied before feedback, so the decoder consumes its emitted
# candle representation, including nonnegative wicks and volume.
FEATURE_MIN = np.array([-1., -1., 0., 0., 0.])
FEATURE_MAX = np.array([1., 1., 1., 1., 40.])


def _config(raw) -> dict:
    config = {"symbol": "", "timeframe": "60", "lookback": 100, "horizon": 10,
              "epochs": 10, "batch_size": 32, "learning_rate": .001,
              "device": "auto", "seed": 42, **dict(raw)}
    for key, lower, upper in (("lookback", 100, 100), ("horizon", 10, 10),
                              ("epochs", 1, 500), ("batch_size", 1, 512),
                              ("seed", 0, 4294967295)):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or not lower <= value <= upper:
            raise ValueError(f"{key} must be an integer between {lower} and {upper}")
        config[key] = int(value)
    if config["timeframe"] not in SECONDS:
        raise ValueError("Transformer supports 15m, 1h, 4h and 1d candles")
    if config["device"] not in {"cuda", "cpu", "auto"}:
        raise ValueError("device must be cuda, cpu or auto")
    if isinstance(config["learning_rate"], bool):
        raise ValueError("learning_rate must be positive and at most 0.1")
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
    # Exchange calendars need not start at UTC midnight. Nobitex daily candles
    # open at Tehran midnight; continuity is checked from actual intervals.
    if (timestamps + pd.Timedelta(seconds=SECONDS[timeframe]) > pd.Timestamp.now(tz="UTC")).any():
        raise ValueError("Candle history includes an incomplete candle")
    return values, timestamps


def _features(values):
    """Lossless valid-candle representation; row zero needs a prior close."""
    logs = np.log(values[:, :4])
    previous = np.r_[logs[0, 0], logs[:-1, 3]]
    return np.column_stack((logs[:, 0] - previous, logs[:, 3] - logs[:, 0],
                            logs[:, 1] - np.maximum(logs[:, 0], logs[:, 3]),
                            np.minimum(logs[:, 0], logs[:, 3]) - logs[:, 2],
                            np.log1p(values[:, 4])))


def _window_indices(timestamps, timeframe, lookback=100, horizon=10):
    gaps = np.r_[0, np.cumsum(np.diff(timestamps.as_unit("ns").asi8) != SECONDS[timeframe] * 1_000_000_000)]
    origins = np.arange(lookback, len(timestamps) - horizon, dtype=np.int64)
    # Source candles begin at i-lookback+1; their first feature needs i-lookback.
    return origins[gaps[origins + horizon] == gaps[origins - lookback]]


def _split_indices(size, lookback=100, horizon=10, eligible=None):
    fit_end, val_end = int(size * .70), int(size * .85)
    origins = np.arange(lookback, size - horizon, dtype=np.int64) if eligible is None else eligible
    return {"fit": origins[origins + horizon < fit_end],
            "validation": origins[(origins + 1 >= fit_end) & (origins + horizon < val_end)],
            "test": origins[(origins + 1 >= val_end) & (origins + horizon < size)]}


def _network(torch):
    nn = torch.nn

    class CandleTransformer(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_projection = nn.Linear(5, 64)
            position = torch.arange(100, dtype=torch.float32).unsqueeze(1)
            divisor = torch.exp(torch.arange(0, 64, 2, dtype=torch.float32) * (-math.log(10000.) / 64))
            encoding = torch.zeros(100, 64)
            encoding[:, 0::2] = torch.sin(position * divisor)
            encoding[:, 1::2] = torch.cos(position * divisor)
            self.register_buffer("positions", encoding.unsqueeze(0))
            encoder_layer = nn.TransformerEncoderLayer(64, 4, dim_feedforward=128,
                dropout=.1, activation="gelu", batch_first=True, norm_first=True)
            decoder_layer = nn.TransformerDecoderLayer(64, 4, dim_feedforward=128,
                dropout=.1, activation="gelu", batch_first=True, norm_first=True)
            self.encoder = nn.TransformerEncoder(encoder_layer, 2, norm=nn.LayerNorm(64),
                                                  enable_nested_tensor=False)
            self.decoder = nn.TransformerDecoder(decoder_layer, 2, norm=nn.LayerNorm(64))
            self.output_projection = nn.Linear(64, 5)

        def embed(self, tokens):
            return self.input_projection(tokens) + self.positions[:, :tokens.size(1)]

        def encode(self, source):
            return self.encoder(self.embed(source))

        def decode(self, tokens, memory):
            count = tokens.size(1)
            mask = torch.triu(torch.ones(count, count, dtype=torch.bool, device=tokens.device), diagonal=1)
            return self.output_projection(self.decoder(self.embed(tokens), memory, tgt_mask=mask))

        def forward(self, source, tokens):
            return self.decode(tokens, self.encode(source))

    return CandleTransformer()


def _recursive_rollout(torch, network, source, mean, scale, horizon=10):
    """Generate without target inputs; return constrained unscaled features."""
    mean = torch.as_tensor(mean, dtype=source.dtype, device=source.device)
    scale = torch.as_tensor(scale, dtype=source.dtype, device=source.device)
    lower = torch.as_tensor(FEATURE_MIN, dtype=source.dtype, device=source.device)
    upper = torch.as_tensor(FEATURE_MAX, dtype=source.dtype, device=source.device)
    memory, tokens = network.encode(source), source[:, -1:, :]
    predictions = []
    for _ in range(horizon):
        raw = network.decode(tokens, memory)[:, -1, :] * scale + mean
        if not bool(torch.isfinite(raw).all()):
            raise RuntimeError("Transformer produced non-finite recursive predictions")
        raw = torch.minimum(torch.maximum(raw, lower), upper)
        predictions.append(raw)
        tokens = torch.cat((tokens, ((raw - mean) / scale).unsqueeze(1)), dim=1)
    return torch.stack(predictions, dim=1)


def _candles_from_features(features, last_close):
    raw = np.asarray(features, dtype=np.float64)
    if raw.ndim != 3 or raw.shape[2] != 5 or not np.isfinite(raw).all():
        raise RuntimeError("Transformer produced invalid OHLCV features")
    raw = np.clip(raw, FEATURE_MIN, FEATURE_MAX)
    previous = np.log(np.asarray(last_close, dtype=np.float64))
    if previous.shape != (len(raw),) or not np.isfinite(previous).all():
        raise ValueError("Forecast needs positive finite previous closes")
    result = np.empty_like(raw)
    for step in range(raw.shape[1]):
        opening = np.clip(previous + raw[:, step, 0], -700, 700)
        closing = np.clip(opening + raw[:, step, 1], -700, 700)
        high = np.clip(np.maximum(opening, closing) + raw[:, step, 2], -700, 700)
        low = np.clip(np.minimum(opening, closing) - raw[:, step, 3], -700, 700)
        result[:, step, :4] = np.exp(np.column_stack((opening, high, low, closing)))
        result[:, step, 4] = np.expm1(raw[:, step, 4])
        previous = closing
    return result


def _metrics(predicted, actual, anchors):
    errors = np.abs(np.log(predicted[:, :, :4]) - np.log(actual[:, :, :4])) * 10000
    volume_error = np.abs(predicted[:, :, 4] - actual[:, :, 4])
    volume_log_error = np.abs(np.log1p(predicted[:, :, 4]) - np.log1p(actual[:, :, 4]))
    correct = np.sign(predicted[:, :, 3] - anchors[:, None, 3]) == np.sign(actual[:, :, 3] - anchors[:, None, 3])
    steps = [{"step": step + 1, **{f"{column.lower()}_mae_bps": float(errors[:, step, position].mean())
              for position, column in enumerate(COLUMNS[:4])},
              "volume_mae": float(volume_error[:, step].mean()),
              "volume_log_mae": float(volume_log_error[:, step].mean()),
              "direction_accuracy": float(correct[:, step].mean())}
             for step in range(predicted.shape[1])]
    return {"close_mae_bps": float(errors[:, :, 3].mean()),
            "ohlc_mae_bps": float(errors.mean()), "volume_mae": float(volume_error.mean()),
            "volume_log_mae": float(volume_log_error.mean()),
            "direction_accuracy": float(correct.mean()), "per_step": steps}


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise TrainingCancelled("Model training cancelled")


def train_transformer(df: pd.DataFrame, config: dict, *, progress=None, cancel=None) -> dict:
    """Fit chronological 70/15/15 splits, purging complete ten-candle labels."""
    config = _config(config)
    values, timestamps = _validate_frame(df, config["timeframe"])
    _check_cancel(cancel)
    size, lookback, horizon = len(values), config["lookback"], config["horizon"]
    fit_end, val_end = int(size * .70), int(size * .85)
    eligible = _window_indices(timestamps, config["timeframe"], lookback, horizon)
    splits = _split_indices(size, lookback, horizon, eligible)
    if len(splits["fit"]) < 32 or min(len(splits["validation"]), len(splits["test"])) < 8:
        raise ValueError("History needs at least 32 training and 8 validation/test contiguous examples after purging; increase candles")
    features = _features(values)
    contiguous = np.r_[False, np.diff(timestamps.as_unit("ns").asi8) == SECONDS[config["timeframe"]] * 1_000_000_000]
    fit_features = features[:fit_end][contiguous[:fit_end]]
    mean, scale = fit_features.mean(axis=0), np.maximum(fit_features.std(axis=0), 1e-8)
    normalized = ((features - mean) / scale).astype(np.float32)
    if not np.isfinite(normalized).all():
        raise ValueError("Candle features exceed the supported numerical range")
    torch = _torch()
    requested = config["device"]
    cuda_available = bool(torch.cuda.is_available()) if requested != "cpu" else False
    if requested == "cuda" and not cuda_available:
        raise RuntimeError("CUDA is unavailable. Install a CUDA-enabled PyTorch build and NVIDIA driver, or select CPU.")
    device = "cuda" if cuda_available else "cpu"
    torch.manual_seed(config["seed"])
    if device == "cuda":
        torch.cuda.manual_seed_all(config["seed"])

    class Windows(torch.utils.data.Dataset):
        def __init__(self, indices, training=False):
            self.indices, self.training = indices, training

        def __len__(self):
            return len(self.indices)

        def __getitem__(self, item):
            origin = int(self.indices[item])
            source = np.ascontiguousarray(normalized[origin - lookback + 1:origin + 1])
            if self.training:
                return source, np.ascontiguousarray(normalized[origin + 1:origin + horizon + 1])
            return source, origin

    loaders = {name: torch.utils.data.DataLoader(Windows(indices, name == "fit"),
        batch_size=config["batch_size"], shuffle=name == "fit", num_workers=0,
        pin_memory=device == "cuda") for name, indices in splits.items()}
    network = _network(torch).to(device)
    optimizer = torch.optim.AdamW(network.parameters(), lr=config["learning_rate"])
    loss_fn = torch.nn.SmoothL1Loss()

    def evaluate(name):
        network.eval()
        predictions = []
        with torch.no_grad():
            for source, origins in loaders[name]:
                _check_cancel(cancel)
                raw = _recursive_rollout(torch, network, source.to(device), mean, scale, horizon)
                indices = origins.numpy()
                predictions.append(_candles_from_features(raw.cpu().numpy(), values[indices, 3]))
        predicted = np.concatenate(predictions)
        origins = splits[name]
        actual = values[origins[:, None] + np.arange(1, horizon + 1)]
        anchors = values[origins]
        baseline = np.repeat(anchors[:, None, :], horizon, axis=1)
        # A flat-price baseline retains the latest volume for every step.
        baseline[:, :, :4] = anchors[:, None, 3, None]
        return _metrics(predicted, actual, anchors), _metrics(baseline, actual, anchors)

    history, best_state, best_mae, best_epoch = [], None, float("inf"), None
    try:
        for epoch in range(1, config["epochs"] + 1):
            _check_cancel(cancel)
            network.train()
            loss_sum = 0.
            for source, targets in loaders["fit"]:
                _check_cancel(cancel)
                source, targets = source.to(device), targets.to(device)
                shifted = torch.cat((source[:, -1:, :], targets[:, :-1, :]), dim=1)
                optimizer.zero_grad(set_to_none=True)
                loss = loss_fn(network(source, shifted), targets)
                if not bool(torch.isfinite(loss)):
                    raise RuntimeError("Training produced a non-finite loss; reduce the learning rate")
                loss.backward()
                torch.nn.utils.clip_grad_norm_(network.parameters(), 1.)
                optimizer.step()
                loss_sum += float(loss.detach().cpu()) * len(source)
            validation, _ = evaluate("validation")
            val_mae = validation["close_mae_bps"]
            record = {"epoch": epoch, "train_loss": loss_sum / len(splits["fit"]),
                      "validation_mae_bps": val_mae, "validation_close_path_mae_bps": val_mae,
                      "validation_volume_log_mae": validation["volume_log_mae"]}
            history.append(record)
            if val_mae < best_mae:
                best_mae, best_epoch = val_mae, epoch
                best_state = {key: tensor.detach().cpu().clone() for key, tensor in network.state_dict().items()}
            if progress is not None:
                progress({"stage": "training", "epochs": config["epochs"], **record,
                          "history": [dict(row) for row in history]})
        _check_cancel(cancel)
        network.load_state_dict(best_state)
        validation, validation_baseline = evaluate("validation")
        test, test_baseline = evaluate("test")
        _check_cancel(cancel)
        metadata = {
            "architecture": ARCHITECTURE, "model_version": ARCHITECTURE,
            "symbol": config["symbol"], "timeframe": config["timeframe"], "source": "nobitex",
            "lookback": lookback, "horizon": horizon, "horizon_candles": horizon,
            "device": device, "device_name": torch.cuda.get_device_name(0) if device == "cuda" else "CPU",
            "torch_version": str(torch.__version__), "cuda_version": torch.version.cuda,
            "feature_names": list(FEATURES), "target": "next 10 complete OHLCV candles",
            "loss_name": "Huber (standardized OHLCV)", "bot_compatible": False,
            "evaluation_mode": "recursive", "selection_metric": "validation_close_path_mae_bps",
            "direction_definition": "close relative to final completed input close",
            "created_at": datetime.now(timezone.utc).isoformat(), "best_epoch": best_epoch,
            "epochs": config["epochs"], "training_rows": len(splits["fit"]),
            "validation_rows": len(splits["validation"]), "test_rows": len(splits["test"]),
            "skipped_gap_windows": size - horizon - lookback - len(eligible),
            "training_window_start": timestamps[0].isoformat(), "training_window_end": timestamps[fit_end - 1].isoformat(),
            "validation_window_start": timestamps[fit_end].isoformat(), "validation_window_end": timestamps[val_end - 1].isoformat(),
            "test_window_start": timestamps[val_end].isoformat(), "test_window_end": timestamps[-1].isoformat(),
            "validation_mae_bps": validation["close_mae_bps"],
            "validation_baseline_mae_bps": validation_baseline["close_mae_bps"],
            "test_mae_bps": test["close_mae_bps"], "baseline_mae_bps": test_baseline["close_mae_bps"],
            "validation_passed": bool(validation["close_mae_bps"] < validation_baseline["close_mae_bps"]),
            "per_step_metrics": {"validation": validation["per_step"], "test": test["per_step"]},
            "metrics": {"validation": validation, "test": test},
            "baseline": {"name": "flat close and last volume", "validation": validation_baseline, "test": test_baseline},
            "network": {"d_model": 64, "heads": 4, "encoder_layers": 2, "decoder_layers": 2, "dropout": .1},
            "feature_bounds": {"min": FEATURE_MIN.tolist(), "max": FEATURE_MAX.tolist()},
            "history": history,
            "data_hash": hashlib.sha256(pd.util.hash_pandas_object(df[list(COLUMNS)], index=True).values.tobytes()).hexdigest(),
        }
        return {"schema_version": 1, "state_dict": best_state, "config": config,
                "metadata": metadata, "scaler": {"mean": mean.tolist(), "scale": scale.tolist()}}
    finally:
        del loaders, optimizer, network
        if device == "cuda":
            torch.cuda.empty_cache()


def save_checkpoint(checkpoint: dict, path) -> None:
    """Publish atomically, preserving an existing checkpoint on failure."""
    torch = _torch()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".transformer-", suffix=".tmp", dir=path.parent)
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
class LoadedTransformer:
    network: object
    config: dict
    metadata: dict
    scaler: dict


def load_checkpoint(path) -> LoadedTransformer:
    torch = _torch()
    try:
        package = torch.load(Path(path), map_location="cpu", weights_only=True)
        if not isinstance(package, dict) or package.get("schema_version") != 1:
            raise ValueError("Unsupported Transformer checkpoint schema")
        metadata = package["metadata"]
        if metadata.get("architecture") != ARCHITECTURE or metadata.get("source") != "nobitex":
            raise ValueError("Unsupported Transformer architecture or source")
        config = _config(package["config"])
        for key in ("symbol", "timeframe", "lookback", "horizon"):
            if metadata.get(key) != config[key]:
                raise ValueError(f"Checkpoint {key} config and metadata disagree")
        if metadata.get("horizon_candles") != config["horizon"]:
            raise ValueError("Checkpoint horizon_candles config and metadata disagree")
        scaler = package["scaler"]
        for name in ("mean", "scale"):
            values = np.asarray(scaler[name], dtype=np.float64)
            if values.shape != (5,) or not np.isfinite(values).all() or (name == "scale" and (values <= 0).any()):
                raise ValueError("Invalid Transformer feature scaler")
        network = _network(torch)
        network.load_state_dict(package["state_dict"], strict=True)
        if not all(bool(torch.isfinite(value).all()) for value in network.state_dict().values()):
            raise ValueError("Checkpoint contains non-finite weights")
        network.eval()
        return LoadedTransformer(network, config, metadata, scaler)
    except Exception as exc:
        raise ValueError(f"Invalid Transformer checkpoint: {exc}") from exc


def predict_transformer(model: LoadedTransformer, df: pd.DataFrame) -> list:
    """Forecast ten UTC candle opens from 100 completed candles + prior close."""
    config, scaler = model.config, model.scaler
    values, timestamps = _validate_frame(df, config["timeframe"])
    required = config["lookback"] + 1
    if len(values) < required:
        raise ValueError(f"Transformer forecast needs at least {required} completed candles (100 inputs plus previous close)")
    values, timestamps = values[-required:], timestamps[-required:]
    if not len(_window_indices(timestamps, config["timeframe"], config["lookback"], 0)):
        raise ValueError("Transformer forecast needs a gap-free 101-candle tail; no missing candles are filled")
    mean, scale = np.asarray(scaler["mean"]), np.asarray(scaler["scale"])
    source = ((_features(values)[1:] - mean) / scale).astype(np.float32)
    if not np.isfinite(source).all():
        raise ValueError("Candle features exceed the supported numerical range")
    torch = _torch()
    model.network.eval()
    with torch.no_grad():
        raw = _recursive_rollout(torch, model.network, torch.from_numpy(source[None]), mean, scale, config["horizon"])
    predicted = _candles_from_features(raw.numpy(), values[-1:, 3])[0]
    interval = pd.Timedelta(seconds=SECONDS[config["timeframe"]])
    return [{"time": (timestamps[-1] + interval * (step + 1)).isoformat(),
             **{column.lower(): float(row[position]) for position, column in enumerate(COLUMNS)}}
            for step, row in enumerate(predicted)]
