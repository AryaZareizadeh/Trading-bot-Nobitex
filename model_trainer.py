"""Per-account model registry and cancellable background model training jobs."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import math
from pathlib import Path
import re
import threading
import uuid

import numpy as np

from config import Config
from data import DataFetcher
from research_scheduler import TRAINING
from trainer_catalog import (ALEXNET, TRANSFORMER, PRICE_AI, AGENTIC_AI, SPOT_HGB,
                             SPOT_LIGHTGBM, NEURAL, DEFAULTS, TIMEFRAMES,
                             definition, model_catalog, artifact_suffix)


ARCHITECTURE = ALEXNET
# All accounts share one GPU and PyTorch's process-wide training RNG. Serialize
# training, including CPU runs; waiting jobs can still be cancelled immediately.
_TRAINING_SLOT = threading.Lock()
_ID = re.compile(r"^[0-9a-f]{32}$")
_TERMINAL = {"done", "failed", "cancelled", "interrupted"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def validate_config(raw):
    family = definition(raw.get('architecture', ALEXNET))
    config = deepcopy(family['defaults'])
    config.update(raw)
    config['architecture'] = family['id']
    symbols = config["symbols"]
    timeframes = config["timeframes"]
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 6:
        raise ValueError("Select between one and six USDT cryptocurrency markets")
    normalized = []
    for symbol in symbols:
        if not isinstance(symbol, str):
            raise ValueError("Invalid cryptocurrency market")
        symbol = symbol.lower().strip().replace("/", "")
        if not re.fullmatch(r"[a-z0-9]{2,20}usdt", symbol):
            raise ValueError("Use USDT markets such as btcusdt")
        normalized.append(symbol)
    if not isinstance(timeframes, list) or not 1 <= len(timeframes) <= 4:
        raise ValueError("Select one or more supported timeframes")
    if any(tf not in family['supported_timeframes'] for tf in timeframes):
        raise ValueError(f"{family['label']} does not support the selected timeframe")
    config["symbols"] = list(dict.fromkeys(normalized))
    config["timeframes"] = list(dict.fromkeys(timeframes))
    if len(config["symbols"]) * len(config["timeframes"]) > 12:
        raise ValueError("Select at most 12 coin/timeframe combinations per run")
    for key, low, high in [("candles", 500, 20000), ("lookback", 64, 256),
                           ("horizon", 1, 72 if family['id'] in (SPOT_HGB, SPOT_LIGHTGBM) else 24),
                           ("epochs", 1, 100), ("batch_size", 8, 256)]:
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")
    rate = config["learning_rate"]
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or not 1e-5 <= rate <= 0.01:
        raise ValueError("learning_rate must be between 0.00001 and 0.01")
    if config["device"] not in ("cuda", "cpu", "auto"):
        raise ValueError("device must be cuda, cpu or auto")
    if family['id'] == TRANSFORMER and (config['lookback'] != 100 or config['horizon'] != 10):
        raise ValueError('Transformer requires 100 lookback candles and 10 forecast candles')
    if family['id'] not in NEURAL:
        config.update(device='cpu', epochs=1)
    minimum = {PRICE_AI: 650, AGENTIC_AI: 1500, SPOT_HGB: 800, SPOT_LIGHTGBM: 800}.get(family['id'], 500)
    if config['candles'] < minimum:
        raise ValueError(f"{family['label']} requires at least {minimum} historical candles")
    if family['id'] in (PRICE_AI, AGENTIC_AI) and config['horizon'] != 4:
        raise ValueError(f"{family['label']} predicts a four-candle horizon")
    if family['id'] == AGENTIC_AI:
        if len(config['timeframes']) != 1:
            raise ValueError('Agentic AI requires one primary timeframe per run')
        primary = config['timeframes'][0]
        if 'specialist_timeframes' not in raw:
            config['specialist_timeframes'] = ['60', '240'] if primary == '60' else ['240', 'D']
        from agentic_ai import validate_timeframes
        config['specialist_timeframes'] = validate_timeframes(primary, config['specialist_timeframes'])
        for key in ('fee_bps', 'exit_fee_bps', 'slippage_bps'):
            value = config[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value < 10000:
                raise ValueError(f'{key} must be positive and below 10000 basis points')
    if family['id'] in (SPOT_HGB, SPOT_LIGHTGBM):
        from spot_data import APPROVED_ASSETS
        if any(symbol[:-4].upper() not in APPROVED_ASSETS for symbol in config['symbols']):
            raise ValueError('Spot model markets must belong to the supported portfolio asset list')
        config['horizon'] = 72
    if int(config["candles"] * .7) - config["lookback"] - config["horizon"] < 100:
        raise ValueError("Increase history for the selected lookback and horizon")
    return config


class ModelTrainerManager:
    def __init__(self, store, *, fetcher=None, trainer=None, runtime=None):
        self.store = store
        self.model_dir = store.path.parent / "models"
        self._jobs = {}
        self._tasks = {}
        self._cancel = {}
        self._lock = threading.RLock()
        self._fetcher = fetcher
        self._trainer = trainer
        self._runtime = runtime

    def load(self):
        with self._lock:
            for key, job in self.store.list("model_training_jobs").items():
                if job["status"] not in _TERMINAL:
                    job.update(status="interrupted", stage="interrupted", finished_at=_now(),
                               error="Training stopped when the server restarted. Start a new run.")
                    self.store.put("model_training_jobs", key, job)
                self._jobs[key] = job

    def get_config(self):
        if self._runtime:
            runtime = self._runtime()
        else:
            from alexnet_model import runtime_info
            runtime = runtime_info()
        return {"architecture": ARCHITECTURE, "runtime": runtime, "timeframes": TIMEFRAMES,
                "defaults": deepcopy(DEFAULTS), "models": model_catalog(runtime),
                "max_pairs": 12, "source": "nobitex"}

    def list_jobs(self):
        with self._lock:
            return deepcopy(sorted(self._jobs.values(), key=lambda item: item["created_at"], reverse=True))

    def get_job(self, job_id):
        with self._lock:
            return deepcopy(self._jobs.get(job_id))

    def _update(self, job_id, **values):
        with self._lock:
            self._jobs[job_id].update(values)
            self.store.put("model_training_jobs", job_id, self._jobs[job_id])

    def list_models(self):
        # The registry belongs to this account's store; paths never come from clients.
        records = self.store.list("trained_models").values()
        return sorted((record for record in records if record.get("status") == "ready"),
                      key=lambda item: item["created_at"], reverse=True)

    def get_model(self, model_id):
        if not isinstance(model_id, str) or not _ID.fullmatch(model_id):
            return None
        record = self.store.get("trained_models", model_id)
        if not record or record.get("status") != "ready":
            return None
        return record

    def model_path(self, model_id):
        if self.get_model(model_id) is None:
            raise ValueError("Saved model not found for this account")
        record = self.get_model(model_id)
        path = self.model_dir / f"{model_id}{artifact_suffix(record.get('architecture', ALEXNET))}"
        if not path.is_file():
            raise ValueError("Saved model checkpoint is missing. Train a replacement model.")
        return path

    def start_job(self, raw):
        config = validate_config(raw)
        with self._lock:
            if any(job["status"] not in _TERMINAL for job in self._jobs.values()):
                raise ValueError("A training run is already active for this account")
            finished = sorted((j for j in self._jobs.values() if j["status"] in _TERMINAL),
                              key=lambda item: item["created_at"])
            while len(self._jobs) >= 20 and finished:
                old_id = finished.pop(0)["id"]
                self.store.delete("model_training_jobs", old_id)
                self._jobs.pop(old_id, None)
            job_id = uuid.uuid4().hex
            job = dict(id=job_id, status="pending", stage="queued", created_at=_now(),
                       config=config, pair_count=len(config["symbols"]) * len(config["timeframes"]),
                       pair_index=0, current_pair=None, epoch=0, epochs=config["epochs"],
                       progress=0, history=[], model_ids=[], error=None)
            self.store.put("model_training_jobs", job_id, job)
            self._jobs[job_id] = job
            self._cancel[job_id] = threading.Event()
            self._tasks[job_id] = asyncio.create_task(self._run(job_id, config))
        return job_id

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            event = self._cancel.get(job_id)
            if job["status"] not in _TERMINAL and event is not None:
                event.set()
                self._update(job_id, stage="cancelling")
            return deepcopy(job)

    def _fetch(self, symbol, timeframe, candles, cancel=None):
        if self._fetcher:
            return self._fetcher(symbol, timeframe, candles)
        cfg = Config()
        cfg.symbol, cfg.interval, cfg.limit, cfg.exchange = symbol, timeframe, candles, "nobitex"
        return DataFetcher(cfg, cancel=cancel).fetch()

    async def _run(self, job_id, config):
        try:
            await TRAINING.run(str(self.store.path.resolve()), self._work, job_id, config,
                               cancel=self._cancel[job_id])
        except Exception as exc:
            cancelled = self._cancel[job_id].is_set()
            self._update(job_id, status="cancelled" if cancelled else "failed", stage="cancelled" if cancelled else "failed",
                         error=None if cancelled else str(exc), finished_at=_now())
        finally:
            with self._lock:
                self._cancel.pop(job_id, None)
                self._tasks.pop(job_id, None)

    def _work(self, job_id, config):
        cancel = self._cancel[job_id]
        acquired = False
        try:
            while not cancel.is_set():
                acquired = _TRAINING_SLOT.acquire(timeout=.25)
                if acquired:
                    break
            if cancel.is_set():
                self._update(job_id, status="cancelled", stage="cancelled", finished_at=_now())
                return
            runtime = self.get_config()["runtime"]
            architecture = config['architecture']
            family = next(item for item in model_catalog(runtime) if item['id'] == architecture)
            if not family['available']:
                raise ValueError(family['error'])
            if architecture in NEURAL and config["device"] == "cuda" and not runtime.get("cuda_available"):
                raise ValueError("CUDA is unavailable in this server. Install CUDA-enabled PyTorch and a compatible NVIDIA driver, or select CPU.")
            self._update(job_id, status="running", started_at=_now())
            pair_count = len(config["symbols"]) * len(config["timeframes"])
            model_ids = []
            for pair_index, (symbol, timeframe) in enumerate((s, t) for s in config["symbols"] for t in config["timeframes"]):
                if cancel.is_set():
                    break
                self._update(job_id, stage="fetching", pair_index=pair_index + 1,
                             current_pair={"symbol": symbol, "timeframe": timeframe}, epoch=0, history=[])
                df = self._fetch(symbol, timeframe, config["candles"], cancel=cancel.is_set)
                if cancel.is_set():
                    break
                self._update(job_id, stage="training", candle_count=len(df))
                def progress(update):
                    update = dict(update)
                    epoch = int(update.get("epoch", 0))
                    update["progress"] = round(100 * (pair_index + epoch / config["epochs"]) / pair_count, 2)
                    self._update(job_id, **update)
                if self._trainer:
                    checkpoint = self._trainer(df, {**config, "symbol": symbol, "timeframe": timeframe},
                                               progress=progress, cancel=cancel.is_set)
                elif architecture == ALEXNET:
                    from alexnet_model import train_alexnet
                    checkpoint = train_alexnet(df, {**config, "symbol": symbol, "timeframe": timeframe},
                                              progress=progress, cancel=cancel.is_set)
                elif architecture == TRANSFORMER:
                    from transformer_model import train_transformer
                    checkpoint = train_transformer(df, {**config, 'symbol': symbol, 'timeframe': timeframe},
                                                   progress=progress, cancel=cancel.is_set)
                else:
                    from trainer_adapters import train_tabular
                    checkpoint = train_tabular(df, {**config, 'symbol': symbol, 'timeframe': timeframe},
                        fetcher=lambda s, t, n: self._fetch(s, t, n, cancel=cancel.is_set),
                        progress=progress, cancel=cancel.is_set)
                if cancel.is_set():
                    break
                model_id = uuid.uuid4().hex
                metadata = {**checkpoint["metadata"], "id": model_id, "status": "ready",
                            "symbol": symbol, "timeframe": timeframe, "architecture": architecture,
                            "lookback": config["lookback"], "horizon": config["horizon"],
                            "bot_compatible": architecture == ALEXNET,
                            "created_at": _now(), "source": "nobitex"}
                metadata.setdefault('horizon_candles', config['horizon'])
                checkpoint["metadata"] = metadata
                if architecture == ALEXNET:
                    from alexnet_model import save_checkpoint
                elif architecture == TRANSFORMER:
                    from transformer_model import save_checkpoint
                else:
                    from trainer_adapters import save_checkpoint
                self.model_dir.mkdir(parents=True, exist_ok=True)
                artifact = self.model_dir / f"{model_id}{artifact_suffix(architecture)}"
                save_checkpoint(checkpoint, artifact)
                if cancel.is_set():
                    # This UUID file was just generated by this worker and has
                    # never been published in the registry.
                    artifact.unlink(missing_ok=True)
                    break
                self.store.put("trained_models", model_id, metadata)
                model_ids.append(model_id)
                self._update(job_id, model_ids=model_ids[:], progress=round(100 * (pair_index + 1) / pair_count, 2))
            self._update(job_id, status="cancelled" if cancel.is_set() else "done",
                         stage="cancelled" if cancel.is_set() else "done", finished_at=_now())
        finally:
            if acquired:
                _TRAINING_SLOT.release()

    def predict(self, model_id):
        record = self.get_model(model_id)
        if record is None:
            raise ValueError("Saved model not found for this account")
        architecture = record.get('architecture', ALEXNET)
        if architecture == TRANSFORMER:
            from transformer_model import load_checkpoint, predict_transformer
            model = load_checkpoint(self.model_path(model_id))
            self._check_metadata(model.metadata, record)
            df = self._fetch(record['symbol'], record['timeframe'], record['lookback'] + 2)
            forecast = predict_transformer(model, df)
            observed = self._candle_rows(df.tail(record['lookback']))
            last_price = float(df['Close'].iloc[-1])
            predicted = float(forecast[-1]['close'])
            duration = DataFetcher._RESOLUTION_SECONDS[record['timeframe']]
            import pandas as pd
            return dict(model_id=model_id, architecture=architecture, symbol=record['symbol'],
                        timeframe=record['timeframe'], horizon=record['horizon'], source='nobitex',
                        observed_candles=observed, forecast_candles=forecast,
                        last_price=last_price, predicted_price=predicted,
                        forecast_return_bps=10000 * math.log(predicted / last_price),
                        observed_close_time=(pd.Timestamp(observed[-1]['time']) + pd.Timedelta(seconds=duration)).isoformat(),
                        target_time=(pd.Timestamp(forecast[-1]['time']) + pd.Timedelta(seconds=duration)).isoformat())
        if architecture != ALEXNET:
            from trainer_adapters import load_checkpoint, predict_tabular
            checkpoint = load_checkpoint(self.model_path(model_id))
            self._check_metadata(checkpoint['metadata'], record)
            return {'model_id': model_id, **predict_tabular(checkpoint, fetcher=self._fetch)}
        from alexnet_model import load_checkpoint, predict_alexnet
        model = load_checkpoint(self.model_path(model_id))
        self._check_metadata(model.metadata, record)
        df = self._fetch(record["symbol"], record["timeframe"], record["lookback"] + 2)
        predictions = predict_alexnet(model, df)
        if len(df) == 0 or not np.isfinite(predictions[-1]):
            raise ValueError("Insufficient consecutive completed candles for this prediction")
        bps = float(predictions[-1])
        last_price = float(df["Close"].iloc[-1])
        predicted_price = last_price * math.exp(bps / 10000)
        if not math.isfinite(predicted_price):
            raise ValueError("Model produced an invalid price forecast")
        duration = DataFetcher._RESOLUTION_SECONDS[record["timeframe"]]
        import pandas as pd
        candle = pd.Timestamp(df.index[-1])
        candle = candle.tz_localize("UTC") if candle.tzinfo is None else candle.tz_convert("UTC")
        return {"model_id": model_id, "symbol": record["symbol"], "timeframe": record["timeframe"],
                "horizon": record["horizon"], "candle_time": candle.isoformat(),
                "observed_close_time": (candle + pd.Timedelta(seconds=duration)).isoformat(),
                "target_candle_time": (candle + pd.Timedelta(seconds=duration * record["horizon"])).isoformat(),
                "target_time": (candle + pd.Timedelta(seconds=duration * (record["horizon"] + 1))).isoformat(),
                "last_price": last_price, "predicted_price": predicted_price,
                "forecast_return_bps": bps, "quote_currency": "USDT", "source": "nobitex"}

    @staticmethod
    def _check_metadata(metadata, record):
        for key in ('id', 'symbol', 'timeframe', 'architecture', 'source', 'lookback', 'horizon'):
            if metadata.get(key) != record.get(key):
                raise ValueError('Saved model metadata does not match its registry')
        if metadata.get('horizon_candles') != record.get('horizon'):
            raise ValueError('Saved model horizon does not match its registry')

    @staticmethod
    def _candle_rows(df):
        import pandas as pd
        rows = []
        for stamp, values in df.iterrows():
            stamp = pd.Timestamp(stamp)
            stamp = stamp.tz_localize('UTC') if stamp.tzinfo is None else stamp.tz_convert('UTC')
            rows.append({'time': stamp.isoformat(), **{key.lower(): float(values[key])
                         for key in ('Open', 'High', 'Low', 'Close', 'Volume')}})
        return rows

    async def shutdown(self):
        for cancel in list(self._cancel.values()):
            cancel.set()
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
