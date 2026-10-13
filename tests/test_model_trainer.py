"""Trainer lifecycle, private registry and API integration without a GPU/network."""
import asyncio
from copy import deepcopy
import threading
import time

import pytest

from model_trainer import DEFAULTS, ModelTrainerManager, validate_config
from storage import Store


RUNTIME = dict(torch_available=True, cuda_available=True, device_name="test GPU",
               torch_version="test", cuda_version="test", error=None)


@pytest.fixture
def trainer(tmp_path, monkeypatch):
    import alexnet_model
    monkeypatch.setattr(alexnet_model, "save_checkpoint", lambda checkpoint, path: path.write_bytes(b"test weights"))
    def fit(df, config, *, progress, cancel):
        progress({"epoch": 1, "history": [{"epoch": 1, "train_loss": .1, "val_loss": .2}]})
        return {"metadata": {"device": config["device"], "validation_passed": True,
                             "validation_mae_bps": 1, "test_mae_bps": 2,
                             "baseline_mae_bps": 3}, "state_dict": {}}
    return ModelTrainerManager(Store(tmp_path / "trader.sqlite3"),
                               fetcher=lambda symbol, timeframe, count: list(range(count)),
                               trainer=fit, runtime=lambda: deepcopy(RUNTIME))


async def finished(manager, job_id):
    task = manager._tasks[job_id]
    await asyncio.wait_for(task, timeout=5)
    return manager.get_job(job_id)


@pytest.mark.parametrize("changes", [
    {"symbols": []}, {"symbols": ["../../etcusdt"]}, {"symbols": ["btcirt"]},
    {"timeframes": ["bogus"]}, {"device": "bad"}, {"learning_rate": float("nan")},
    {"epochs": 0}, {"horizon": 25}, {"lookback": 63}, {"batch_size": True},
    {"symbols": ["btcusdt", "ethusdt", "zecusdt", "adausdt"], "timeframes": ["15", "60", "240", "D"]},
])
def test_invalid_training_inputs_rejected(changes):
    with pytest.raises(ValueError):
        validate_config({**DEFAULTS, **changes})


def test_symbols_normalized_deduplicated_without_changing_defaults():
    config = validate_config({"symbols": ["BTC/USDT", "btcusdt"], "timeframes": ["60", "60"]})
    assert config["symbols"] == ["btcusdt"]
    assert config["timeframes"] == ["60"]
    config["symbols"].append("ethusdt")
    assert DEFAULTS["symbols"] == ["btcusdt"]


def test_cartesian_training_saves_private_models_and_persists_history(trainer):
    async def run():
        job_id = trainer.start_job({"symbols": ["btcusdt", "ethusdt"], "timeframes": ["60", "240"], "epochs": 1})
        job = await finished(trainer, job_id)
        assert job["status"] == "done", job
        assert job["progress"] == 100
        assert len(job["model_ids"]) == 4
        assert len(trainer.list_models()) == 4
        for model in trainer.list_models():
            assert model["source"] == "nobitex"
            assert model["architecture"] == "alexnet1d-v1"
            assert trainer.model_path(model["id"]).is_file()
        copy = ModelTrainerManager(trainer.store)
        copy.load()
        assert copy.get_job(job_id) == job
        assert copy.list_models() == trainer.list_models()
    asyncio.run(run())


def test_cancel_active_batch_and_prevent_concurrent_account_runs(trainer):
    started = threading.Event()
    def fit(df, config, *, progress, cancel):
        started.set()
        while not cancel():
            time.sleep(.005)
        raise RuntimeError("batch cancelled")
    trainer._trainer = fit
    async def run():
        job_id = trainer.start_job({})
        assert await asyncio.to_thread(started.wait, 2)
        with pytest.raises(ValueError, match="already active"):
            trainer.start_job({})
        assert trainer.cancel(job_id)["stage"] == "cancelling"
        job = await finished(trainer, job_id)
        assert job["status"] == "cancelled"
        assert job["model_ids"] == []
        assert job["error"] is None
    asyncio.run(run())


def test_cancel_queued_job_releases_no_foreign_slot(trainer):
    from model_trainer import _TRAINING_SLOT
    async def run():
        assert _TRAINING_SLOT.acquire(timeout=1)
        try:
            job_id = trainer.start_job({})
            trainer.cancel(job_id)
            job = await finished(trainer, job_id)
            assert job["status"] == "cancelled"
            assert _TRAINING_SLOT.locked()
        finally:
            _TRAINING_SLOT.release()
    asyncio.run(run())


@pytest.mark.parametrize("torch_available,cuda_available,error", [
    (False, False, "Install PyTorch"), (True, False, "CUDA is unavailable"),
])
def test_missing_runtime_gives_actionable_error(trainer, torch_available, cuda_available, error):
    trainer._runtime = lambda: {**RUNTIME, "torch_available": torch_available, "cuda_available": cuda_available,
                                "error": None}
    async def run():
        job = await finished(trainer, trainer.start_job({}))
        assert job["status"] == "failed"
        assert error in job["error"]
        assert trainer.list_models() == []
    asyncio.run(run())


def test_completed_pair_retained_if_later_pair_fails(trainer):
    def fetch(symbol, timeframe, count):
        if symbol == "ethusdt":
            raise ValueError("History unavailable")
        return list(range(count))
    trainer._fetcher = fetch
    async def run():
        job = await finished(trainer, trainer.start_job({"symbols": ["btcusdt", "ethusdt"]}))
        assert job["status"] == "failed"
        assert job["error"] == "History unavailable"
        assert len(job["model_ids"]) == len(trainer.list_models()) == 1
    asyncio.run(run())


def test_cancel_during_save_does_not_publish_checkpoint(trainer, monkeypatch):
    import alexnet_model
    def save(checkpoint, path):
        path.write_bytes(b'new weights')
        trainer.cancel(next(iter(trainer._jobs)))
    monkeypatch.setattr(alexnet_model, 'save_checkpoint', save)
    async def run():
        job = await finished(trainer, trainer.start_job({}))
        assert job['status'] == 'cancelled'
        assert job['model_ids'] == []
        assert trainer.list_models() == []
        assert list(trainer.model_dir.glob('*.pt')) == []
    asyncio.run(run())


def test_restart_marks_inflight_runs_interrupted(trainer):
    trainer.store.put("model_training_jobs", "a" * 32,
                      dict(id="a" * 32, status="running", stage="training", created_at="2026-01-01"))
    trainer.load()
    assert trainer.get_job("a" * 32)["status"] == "interrupted"
    assert trainer.store.get("model_training_jobs", "a" * 32)["status"] == "interrupted"


def test_models_and_job_ids_cannot_cross_account_boundary(trainer, tmp_path):
    model_id = "a" * 32
    trainer.store.put("trained_models", model_id, dict(id=model_id, status="ready", created_at="2026-01-01"))
    other = ModelTrainerManager(Store(tmp_path / "other" / "trader.sqlite3"))
    assert other.get_model(model_id) is None
    assert other.get_job(model_id) is None
    assert trainer.get_model("../" + model_id) is None
    with pytest.raises(ValueError, match="missing"):
        trainer.model_path(model_id)


def test_api_trainer_validation_jobs_and_model_scope(tmp_path):
    from bot_manager import BotManager
    from fastapi.testclient import TestClient
    from server import create_app
    app = create_app(manager=BotManager(store=Store(tmp_path / "trader.sqlite3"), legacy_dir=tmp_path),
                     offline=True, security=False)
    app.state.model_trainer._runtime = lambda: {**RUNTIME, "torch_available": False}
    with TestClient(app) as client:
        assert client.get("/api/model-trainer/config").json()["runtime"]["device_name"] == "test GPU"
        assert client.get("/api/model-trainer/models").json() == {"models": []}
        assert client.post("/api/model-trainer/run", json={"symbols": ["btcusdt"], "timeframes": ["1"]}).status_code == 400
        assert client.post("/api/model-trainer/run", json={"symbols": [], "timeframes": ["60"]}).status_code == 422
        response = client.post("/api/model-trainer/run", json={"symbols": ["btcusdt"], "timeframes": ["60"]})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        assert client.get(f"/api/model-trainer/run/{job_id}").json()["id"] == job_id
        assert client.get("/api/model-trainer/jobs").json()["jobs"][0]["id"] == job_id
        assert client.post(f"/api/model-trainer/run/{job_id}/cancel").status_code == 200
        assert client.get("/api/model-trainer/run/unknown").status_code == 404
        assert client.post("/api/model-trainer/run/unknown/cancel").status_code == 404
        assert client.post(f"/api/model-trainer/models/{'f' * 32}/predict").status_code == 404


def test_history_cancellation_prevents_network_call():
    from config import Config
    from data import DataFetcher
    with pytest.raises(RuntimeError, match="cancelled"):
        DataFetcher(Config(), cancel=lambda: True).fetch()


def test_prediction_reports_utc_future_candle_close_and_checks_horizon(trainer, monkeypatch):
    import alexnet_model
    import numpy as np
    import pandas as pd
    from types import SimpleNamespace
    model_id = "a" * 32
    record = dict(id=model_id, status="ready", created_at="2026-01-01", source="nobitex",
                  symbol="btcusdt", timeframe="60", architecture="alexnet1d-v1",
                  lookback=96, horizon=4, horizon_candles=4)
    trainer.store.put("trained_models", model_id, record)
    trainer.model_dir.mkdir()
    (trainer.model_dir / f"{model_id}.pt").write_bytes(b"test weights")
    metadata = dict(record)
    monkeypatch.setattr(alexnet_model, "load_checkpoint", lambda path: SimpleNamespace(metadata=metadata))
    monkeypatch.setattr(alexnet_model, "predict_alexnet", lambda model, df: np.array([100.0]))
    trainer._fetcher = lambda *args: pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2026-01-01T00:00:00"]))
    prediction = trainer.predict(model_id)
    assert prediction["predicted_price"] == pytest.approx(101.0050167)
    assert prediction["observed_close_time"] == "2026-01-01T01:00:00+00:00"
    assert prediction["target_time"] == "2026-01-01T05:00:00+00:00"
    metadata["horizon_candles"] = 5
    with pytest.raises(ValueError, match="horizon"):
        trainer.predict(model_id)


def test_prediction_reports_utc_future_candle_close_and_checks_horizon(trainer, monkeypatch):
    import alexnet_model
    import numpy as np
    import pandas as pd
    from types import SimpleNamespace
    model_id = "a" * 32
    record = dict(id=model_id, status="ready", created_at="2026-01-01", source="nobitex",
                  symbol="btcusdt", timeframe="60", architecture="alexnet1d-v1",
                  lookback=96, horizon=4, horizon_candles=4)
    trainer.store.put("trained_models", model_id, record)
    trainer.model_dir.mkdir()
    (trainer.model_dir / f"{model_id}.pt").write_bytes(b"test weights")
    metadata = dict(record)
    monkeypatch.setattr(alexnet_model, "load_checkpoint", lambda path: SimpleNamespace(metadata=metadata))
    monkeypatch.setattr(alexnet_model, "predict_alexnet", lambda model, df: np.array([100.0]))
    trainer._fetcher = lambda *args: pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2026-01-01T00:00:00"]))
    prediction = trainer.predict(model_id)
    assert prediction["predicted_price"] == pytest.approx(101.0050167)
    assert prediction["observed_close_time"] == "2026-01-01T01:00:00+00:00"
    assert prediction["target_time"] == "2026-01-01T05:00:00+00:00"
    metadata["horizon_candles"] = 5
    with pytest.raises(ValueError, match="horizon"):
        trainer.predict(model_id)
