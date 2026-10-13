"""Saved-model bot integration without a GPU or exchange network dependency."""
import asyncio
import sys
import threading
import uuid
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from bot import Bot, BotConfig, BotStatus
from bot_manager import BotManager
from storage import Store


EXECUTION = {"entry_fee_rate": 0.001, "exit_fee_rate": 0.001,
             "slippage_bps": 5, "margin_fraction": 0.25}


def candles(count=900):
    t = np.arange(count)
    close = 100 * np.exp(0.00015 * t + 0.01 * np.sin(t / 13))
    return pd.DataFrame({"Open": close * 0.999, "High": close * 1.004,
                         "Low": close * 0.995, "Close": close,
                         "Volume": 100 + np.sin(t / 7)},
                        index=pd.date_range("2026-01-01", periods=count, freq="h"))


@pytest.fixture
def saved_model(tmp_path, monkeypatch):
    frame = candles()
    model_id = uuid.uuid4().hex
    metadata = {"id": model_id, "model_version": "alexnet1d-v1", "architecture": "alexnet1d-v1",
                "symbol": "btcusdt", "timeframe": "60", "source": "nobitex",
                "lookback": 96, "horizon_candles": 4, "validation_passed": True,
                "training_window_end": str(frame.index[599]),
                "validation_window_end": str(frame.index[649]),
                "test_window_start": str(frame.index[650])}
    store = Store(tmp_path / "trader.sqlite3")
    record = {**metadata, "id": model_id, "status": "ready", "horizon": 4}
    store.put("trained_models", model_id, record)
    directory = store.path.parent / "models"
    directory.mkdir()
    path = directory / f"{model_id}.pt"
    path.write_bytes(b"fake checkpoint loaded by the test model")
    loads, predictions = [], []
    model = SimpleNamespace(metadata=metadata)

    def load_checkpoint(file):
        loads.append(file)
        return model

    def predict_alexnet(loaded, data):
        assert loaded is model
        predictions.append(data.copy())
        values = np.full(len(data), 200.0)
        values[:metadata["lookback"] - 1] = np.nan
        return values

    monkeypatch.setitem(sys.modules, "alexnet_model", SimpleNamespace(
        load_checkpoint=load_checkpoint, predict_alexnet=predict_alexnet))
    manager = BotManager(store, legacy_dir=tmp_path)
    cfg = BotConfig("btcusdt", "AlexNet", "60", 100, leverage=1,
                    ai_model_id=model_id, execution=EXECUTION)
    return SimpleNamespace(frame=frame, store=store, record=record, manager=manager,
                           cfg=cfg, model=model, path=path, loads=loads,
                           predictions=predictions)


def test_alexnet_requires_explicit_saved_model_simulation_and_costs(saved_model):
    cfg = saved_model.cfg
    with pytest.raises(ValueError, match="simulation mode only"):
        Bot(replace(cfg, mode="real"))
    with pytest.raises(ValueError, match="saved model ID"):
        Bot(replace(cfg, ai_model_id=None))
    with pytest.raises(ValueError, match="saved model ID"):
        Bot(replace(cfg, ai_model_id="../outside"))
    with pytest.raises(ValueError, match="positive entry fee"):
        Bot(replace(cfg, execution={}))
    with pytest.raises(ValueError, match="next-open fills"):
        Bot(replace(cfg, execution={**EXECUTION, "fill_timing": "legacy_close"}))
    with pytest.raises(ValueError, match="15-minute"):
        Bot(replace(cfg, timeframe="1"))


@pytest.mark.parametrize("change, message", [
    ({"symbol": "ethusdt"}, "match the bot"),
    ({"timeframe": "240"}, "match the bot"),
    ({"status": "failed"}, "unavailable"),
    ({"validation_passed": False}, "chronological baseline"),
    ({"source": "external"}, "Nobitex candles"),
    ({"architecture": "another-model"}, "architecture is invalid"),
    ({"id": "f" * 32}, "registry identity"),
])
def test_manager_rejects_unsuitable_checkpoints_atomically(saved_model, change, message):
    env = saved_model
    env.store.put("trained_models", env.cfg.ai_model_id, {**env.record, **change})
    with pytest.raises(ValueError, match=message):
        env.manager.create_bot(env.cfg)
    assert env.manager.get_all() == []
    assert env.store.list("bots") == {}


def test_manager_rejects_missing_model_and_failed_update_preserves_bot(saved_model):
    env = saved_model
    bot = env.manager.create_bot(env.cfg)
    with pytest.raises(ValueError, match="match the bot"):
        env.manager.update_bot(bot, replace(env.cfg, symbol="ethusdt"))
    assert bot.cfg.symbol == "btcusdt"
    assert env.store.get("bots", bot.id)["symbol"] == "btcusdt"
    env.path.unlink()
    with pytest.raises(ValueError, match="checkpoint is missing"):
        env.manager.create_bot(replace(env.cfg, id="newbot"))


def test_live_simulation_uses_fixed_checkpoint_and_survives_restart(saved_model):
    env = saved_model
    bot = env.manager.create_bot(env.cfg)
    bot.df = env.frame.iloc[:800].copy()
    bot._live_from_ts = env.frame.index[700]
    bot._run_strategy()
    assert bot._ai_model is env.model
    assert len(env.loads) == 1
    assert len(bot.ai_decisions) == 100
    assert bot.ai_decisions[0]["candle_time"] == str(env.frame.index[700])
    assert bot.ai_decisions[-1]["horizon_candles"] == 4
    assert bot.ai_decisions[-1]["predicted_price"] == pytest.approx(env.frame.Close.iloc[799] * np.exp(0.02))
    assert bot.to_state_dict()["ai_model_id"] == env.cfg.ai_model_id
    assert bot.to_detail_dict()["ai_decisions"]
    snapshot = bot.snapshot()

    restored_manager = BotManager(env.store, legacy_dir=env.store.path.parent)
    restored_manager.load()
    restored = restored_manager.get_bot(bot.id)
    assert restored.cfg.ai_model_id == env.cfg.ai_model_id
    restored.restore(snapshot)
    assert restored.status == BotStatus.IDLE
    assert restored._ai_model is None
    bot.df = restored.df = env.frame.iloc[:801].copy()
    bot._run_strategy()
    restored._run_strategy()
    assert len(env.loads) == 2
    assert restored.ai_model_metadata == bot.ai_model_metadata
    assert restored.ai_decisions == bot.ai_decisions
    assert restored.completed_trades == bot.completed_trades
    assert restored.open_positions == bot.open_positions


def test_live_simulation_refuses_replay_before_an_explicit_start(saved_model):
    env = saved_model
    bot = env.manager.create_bot(env.cfg)
    bot.df = env.frame
    with pytest.raises(RuntimeError, match="explicit start boundary"):
        bot._run_strategy()
    assert env.loads == []
    assert env.predictions == []


def test_backtest_uses_untouched_test_window_and_next_open_costs(saved_model, monkeypatch):
    env = saved_model
    bot = env.manager.create_bot(env.cfg)
    monkeypatch.setattr("bot.DataFetcher.fetch", lambda _: env.frame.copy())
    monkeypatch.setattr("position_limits.current_max_hold_days", lambda: 30)
    result = asyncio.run(bot.run_backtest())
    assert len(env.loads) == 1
    assert len(result["decisions"]) == 250
    assert result["candle_count"] == 250
    assert result["metadata"]["strategy"] == "AlexNet"
    assert result["metadata"]["window_start"] == str(env.frame.index[650])
    assert result["metadata"]["execution"]["fill_timing"] == "next_open"
    assert result["fees"] > 0
    assert result["trades"][0]["_start_ts"] == int(env.frame.index[651].timestamp())
    assert result["model"]["model_id"] == env.cfg.ai_model_id


def test_backtest_refuses_training_and_validation_only_history(saved_model, monkeypatch):
    env = saved_model
    bot = env.manager.create_bot(env.cfg)
    monkeypatch.setattr("bot.DataFetcher.fetch", lambda _: env.frame.iloc[:650].copy())
    monkeypatch.setattr("position_limits.current_max_hold_days", lambda: 30)
    with pytest.raises(ValueError, match="no held-out candles"):
        asyncio.run(bot.run_backtest())
    assert env.predictions == []


def test_cnn_forecasts_do_not_depend_on_price_ai_feature_readiness(saved_model):
    env = saved_model
    bot = env.manager.create_bot(env.cfg)
    frame = env.frame.copy()
    frame["Volume"] = 0
    result = bot._evaluate_alexnet(frame, 650)
    assert result["decisions"][0]["action"] == "enter_long"
    assert result["summary"]["trade_count"] > 0


def test_checkpoint_market_metadata_is_checked_before_start(saved_model):
    env = saved_model
    bot = env.manager.create_bot(env.cfg)
    env.model.metadata["symbol"] = "ethusdt"
    with pytest.raises(ValueError, match="registered market"):
        asyncio.run(bot.start())
    assert bot.status == BotStatus.IDLE
    assert bot._task is None


@pytest.mark.parametrize("key,value", [("id", "f" * 32), ("architecture", "different-cnn")])
def test_checkpoint_identity_and_architecture_are_checked_before_start(saved_model, key, value):
    env = saved_model
    bot = env.manager.create_bot(env.cfg)
    env.model.metadata[key] = value
    with pytest.raises(ValueError, match="registered identity and architecture"):
        asyncio.run(bot.start())
    assert bot.status == BotStatus.IDLE
    assert bot._task is None


@pytest.mark.parametrize("second_action", ["start", "stop"])
def test_awaited_model_loading_serializes_start_and_stop(saved_model, monkeypatch, second_action):
    env = saved_model
    bot = env.manager.create_bot(env.cfg)
    loading, release = threading.Event(), threading.Event()
    loads, runs = [], []

    def slow_preflight():
        loads.append(True)
        loading.set()
        assert release.wait(3)

    async def run_loop():
        runs.append(True)
        await bot._stop_event.wait()

    monkeypatch.setattr(bot, "_ensure_alexnet_model", slow_preflight)
    monkeypatch.setattr(bot, "_run_loop", run_loop)

    async def exercise():
        first = asyncio.create_task(bot.start())
        assert await asyncio.to_thread(loading.wait, 2)
        second = asyncio.create_task(getattr(bot, second_action)())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 3)
        await asyncio.sleep(0)
        assert len(loads) == 1
        if second_action == "start":
            assert bot.status == BotStatus.RUNNING
            assert len(runs) == 1
            await bot.stop()
        else:
            assert bot.status == BotStatus.STOPPED
            assert bot._stop_event.is_set()
        assert bot._task.done()

    asyncio.run(exercise())


def test_missing_checkpoint_allows_registry_reload_but_prevents_start(saved_model):
    env = saved_model
    bot = env.manager.create_bot(env.cfg)
    env.path.unlink()
    manager = BotManager(env.store, legacy_dir=env.store.path.parent)
    manager.load()
    restored = manager.get_bot(bot.id)
    assert restored.status == BotStatus.IDLE
    with pytest.raises(ValueError, match="checkpoint is missing"):
        asyncio.run(restored.start())
    assert restored._task is None


def test_alexnet_discards_a_candle_that_has_not_closed(saved_model):
    env = saved_model
    bot = env.manager.create_bot(env.cfg)
    frame = env.frame.iloc[:100].copy()
    open_time = pd.Timestamp.now(tz="UTC").floor("h").tz_localize(None)
    frame.index = pd.date_range(end=open_time, periods=100, freq="h")
    closed = bot._alexnet_closed_frame(frame)
    assert len(closed) == 99
    assert closed.index[-1] == open_time - pd.Timedelta(hours=1)


def test_api_create_edit_duplicate_and_restart_preserve_saved_model_id(saved_model):
    from tests.test_dashboard_flow import client_for, login

    env = saved_model
    client, manager = client_for(env.store.path.parent)
    request = {"symbol": "BTC/USDT", "strategy_name": "AlexNet", "timeframe": "60",
               "credit": 100, "leverage": 1, "ai_model_id": env.cfg.ai_model_id,
               "execution": EXECUTION}
    with client:
        assert client.get("/api/model-trainer/models").status_code == 401
        headers = login(client)
        response = client.post("/api/bots", headers=headers, json=request)
        assert response.status_code == 201, response.text
        created = response.json()
        bot_id = created["id"]
        assert created["symbol"] == "BTCUSDT"
        assert created["ai_model_id"] == env.cfg.ai_model_id
        assert created["status"] == "idle"
        edited = client.patch(f"/api/bots/{bot_id}", headers=headers,
                              json={"name": "Saved CNN", "credit": 250})
        assert edited.status_code == 200, edited.text
        assert edited.json()["ai_model_id"] == env.cfg.ai_model_id
        assert manager.get_bot(bot_id).cfg.ai_model_id == env.cfg.ai_model_id
        duplicate = client.post(f"/api/bots/{bot_id}/duplicate", headers=headers)
        assert duplicate.status_code == 201, duplicate.text
        assert duplicate.json()["ai_model_id"] == env.cfg.ai_model_id
        assert duplicate.json()["status"] == "idle"
        assert duplicate.json()["id"] != bot_id
        ids = {bot_id, duplicate.json()["id"]}
        assert env.loads == []

    restored_client, restored_manager = client_for(env.store.path.parent)
    with restored_client:
        login(restored_client)
        restored = restored_client.get("/api/bots").json()
        assert {item["id"] for item in restored} == ids
        assert all(item["ai_model_id"] == env.cfg.ai_model_id and item["status"] == "idle" for item in restored)
        assert all(bot.cfg.ai_model_id == env.cfg.ai_model_id for bot in restored_manager.get_all())


def test_api_invalid_model_references_are_rejected_without_reconfiguring(saved_model):
    from tests.test_dashboard_flow import client_for, login

    env = saved_model
    client, manager = client_for(env.store.path.parent)
    request = {"symbol": "btcusdt", "strategy_name": "AlexNet", "timeframe": "60",
               "credit": 100, "ai_model_id": env.cfg.ai_model_id, "execution": EXECUTION}
    with client:
        headers = login(client)
        for changes in ({"ai_model_id": "../outside"}, {"ai_model_id": "f" * 32},
                        {"symbol": "ethusdt"}, {"timeframe": "240"}, {"mode": "real"}):
            response = client.post("/api/bots", headers=headers, json={**request, **changes})
            assert response.status_code == 400, response.text
        assert manager.get_all() == []
        response = client.post("/api/bots", headers=headers, json=request)
        assert response.status_code == 201, response.text
        bot_id = response.json()["id"]
        for changes in ({"ai_model_id": "f" * 32}, {"symbol": "ethusdt"},
                        {"timeframe": "240"}, {"mode": "real"}):
            response = client.patch(f"/api/bots/{bot_id}", headers=headers, json=changes)
            assert response.status_code == 400, response.text
            assert manager.get_bot(bot_id).cfg.ai_model_id == env.cfg.ai_model_id
            assert manager.get_bot(bot_id).cfg.symbol == "btcusdt"
            assert manager.get_bot(bot_id).cfg.timeframe == "60"
            assert manager.get_bot(bot_id).cfg.mode == "simulation"
        switched = client.patch(f"/api/bots/{bot_id}", headers=headers,
                                json={"strategy_name": "PriceAI"})
        assert switched.status_code == 200, switched.text
        assert manager.get_bot(bot_id).cfg.ai_model_id is None


def test_api_foreign_account_cannot_select_or_predict_a_saved_model(saved_model, tmp_path):
    from tests.test_dashboard_flow import client_for, login

    env = saved_model
    client, manager = client_for(tmp_path / "another-account")
    with client:
        headers = login(client)
        assert client.get("/api/model-trainer/models").json() == {"models": []}
        response = client.post("/api/bots", headers=headers,
                               json={"symbol": "btcusdt", "strategy_name": "AlexNet", "timeframe": "60",
                                     "credit": 100, "ai_model_id": env.cfg.ai_model_id, "execution": EXECUTION})
        assert response.status_code == 400, response.text
        assert manager.get_all() == []
        response = client.post(f"/api/model-trainer/models/{env.cfg.ai_model_id}/predict", headers=headers)
        assert response.status_code == 404
        assert env.loads == []


def test_api_missing_checkpoint_start_and_duplicate_return_actionable_error(saved_model):
    from tests.test_dashboard_flow import client_for, login

    env = saved_model
    client, manager = client_for(env.store.path.parent)
    with client:
        headers = login(client)
        created = client.post("/api/bots", headers=headers,
                              json={"symbol": "btcusdt", "strategy_name": "AlexNet", "timeframe": "60",
                                    "credit": 100, "ai_model_id": env.cfg.ai_model_id, "execution": EXECUTION})
        assert created.status_code == 201, created.text
        bot_id = created.json()["id"]
        env.path.unlink()
        response = client.post(f"/api/bots/{bot_id}/start", headers=headers)
        assert response.status_code == 400, response.text
        assert "checkpoint is missing" in response.json()["error"]
        assert manager.get_bot(bot_id).status == BotStatus.IDLE
        response = client.post(f"/api/bots/{bot_id}/duplicate", headers=headers)
        assert response.status_code == 400, response.text
        assert "checkpoint is missing" in response.json()["error"]
        assert len(manager.get_all()) == 1

