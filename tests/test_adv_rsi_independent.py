import asyncio
import json
from dataclasses import asdict, fields
from types import SimpleNamespace

import pandas as pd
import pytest


def candles(n=800):
    return pd.DataFrame({"Open": 100., "High": 100., "Low": 100., "Close": 100., "Volume": 1.},
                        index=pd.date_range("2025-01-01", periods=n, freq="h"))


@pytest.mark.parametrize("name,removed", [("AdvRSILong", "overbought"), ("AdvRSIShort", "oversold")])
def test_directional_strategy_has_ten_searchable_parameters(name, removed):
    from strategy import PARAMS_CLASS
    from optimizer import PARAM_BOUNDS
    assert name in PARAMS_CLASS
    names = {f.name for f in fields(PARAMS_CLASS[name])}
    assert len(names) == 10
    assert removed not in names
    assert names == set(PARAM_BOUNDS[name])


@pytest.mark.parametrize("name,k_values,rsi,ma,expected", [
    ("AdvRSILong", [10, 10, 90, 90, 10, 30], 45, 40, [None, None, "long", None, None, "long"]),
    ("AdvRSIShort", [90, 90, 10, 10, 90, 70], 55, 60, [None, None, "short", None, None, "short"]),
])
def test_directional_entry_arms_once_and_handles_jump_across_bands(name, k_values, rsi, ma, expected):
    from bot import STRATEGY_MAP
    from strategy import PARAMS_CLASS
    from config import Config
    assert name in STRATEGY_MAP
    strategy = STRATEGY_MAP[name](Config(), PARAMS_CLASS[name](entry_wait_candles=0))
    strategy._init_entry_state()
    k = pd.Series(k_values)
    r, m = pd.Series([rsi]*len(k)), pd.Series([ma]*len(k))
    assert [strategy._entry_signal(i, k, k, r, m, k-k) for i in range(len(k))] == expected


@pytest.mark.parametrize("name,direction,k_values,rsi,ma", [
    ("AdvRSILong", "Long", [10, 30, 90, 70, 10, 30], 45, 40),
    ("AdvRSIShort", "Short", [90, 70, 10, 30, 90, 70], 55, 60),
])
def test_research_and_strategy_restrict_entries_and_seed_positions(monkeypatch, name, direction, k_values, rsi, ma):
    from indicators import Indicators
    from execution_model import evaluate_research
    from bot import STRATEGY_MAP
    from strategy import PARAMS_CLASS
    from config import Config
    assert name in STRATEGY_MAP
    k = pd.Series(k_values, index=candles(6).index, dtype=float)
    monkeypatch.setattr(Indicators, "stoch_rsi", lambda *args: SimpleNamespace(k=k, d=k))
    monkeypatch.setattr(Indicators, "rsi_with_ma", lambda *args: (k*0+rsi, k*0+ma))
    params = PARAMS_CLASS[name](entry_wait_candles=0)
    result = evaluate_research(candles(6), name, 100, 2, params,
                               {"fill_timing": "legacy_close", "margin_fraction": .25}, start_idx=0)
    positions = result["trades"] + result["open_positions"]
    assert positions
    assert all(p.get("Position", p.get("direction")) == direction for p in positions)
    strategy = STRATEGY_MAP[name](Config(), params)
    strategy.run(candles(6), Indicators(candles(6)))
    assert all(p["direction"] == direction for p in strategy.open_positions)
    other = "Short" if direction == "Long" else "Long"
    with pytest.raises(ValueError, match="direction"):
        strategy.run(candles(6), Indicators(candles(6)), seed_positions=[
            {"direction": other, "entry_price": 100, "entry_time": str(k.index[0])}])


def test_pair_fetches_once_searches_sequentially_and_persists_results(monkeypatch, tmp_path):
    import optimizer
    from storage import Store
    calls, searches = [], []
    monkeypatch.setattr(optimizer, "_fetch_df", lambda *args: calls.append(args) or candles(args[2] + optimizer.WARMUP_CANDLES))
    def search(job, df):
        searches.append((job.strategy, len(df), job.evaluation_start, job.credit))
        job.best_params = asdict(optimizer.PARAMS_CLASS[job.strategy]())
        job.best_params["rsi_length"] = 14 if job.strategy == "AdvRSILong" else 38
        job.best_summary = optimizer.evaluate(df, job.strategy, optimizer.PARAMS_CLASS[job.strategy](**job.best_params),
                                              job.credit, job.leverage, execution=job.execution, start_idx=job.evaluation_start)
        job.best_pnl = job.best_summary["total_pnl"]
    monkeypatch.setattr(optimizer.OptimizerJob, "_run_de", search)
    store = Store(tmp_path / "pair.sqlite3")
    manager = optimizer.OptimizerManager(store)
    assert "AdvRSIIndependent" in {s["value"] for s in manager.get_config()["available_strategies"]}
    async def run():
        job = manager.start_job("btcusdt", "AdvRSIIndependent", "60", 500, 999, 2,
                                train_pct=70, long_credit=300, short_credit=700,
                                custom_bounds={"short_rsi_length": [6, 250]}, random_seed=42)
        await job._task
        return job.to_dict()
    result = asyncio.run(run())
    assert result["status"] == "done", result["error"]
    assert len(calls) == 1
    assert [s[0] for s in searches] == ["AdvRSILong", "AdvRSIShort"]
    assert searches[0][1:3] == searches[1][1:3]
    assert [s[3] for s in searches] == [300, 700]
    assert result["credit"] == 1000
    assert result["sides"]["long"]["best_params"]["rsi_length"] == 14
    assert result["sides"]["short"]["best_params"]["rsi_length"] == 38
    assert result["train_candle_count"] == 350
    assert result["test_candle_count"] == 150
    assert result["sides"]["long"]["train_candle_count"] == result["sides"]["short"]["train_candle_count"]
    assert result["sides"]["long"]["test_candle_count"] == result["sides"]["short"]["test_candle_count"]
    assert result["train_candle_count"] == result["sides"]["long"]["train_candle_count"]
    assert result["test_candle_count"] == result["sides"]["short"]["test_candle_count"]
    assert result["best_summary"]["total_pnl"] == result["combined_pnl"]
    recovered = optimizer.OptimizerManager(store).get_job(result["id"]).to_dict()
    assert recovered["sides"] == json.loads(json.dumps(result["sides"]))


def test_pair_control_applies_to_both_sides():
    import optimizer
    assert hasattr(optimizer, "IndependentOptimizerJob")
    job = optimizer.IndependentOptimizerJob("btcusdt", "60", 500, 100, 100, 2)
    assert job.pause()
    assert all(side._pause_requested for side in job.sides.values())
    assert job.resume()
    assert not any(side._pause_requested for side in job.sides.values())
    job.cancel()
    assert all(side._cancel for side in job.sides.values())


def test_pair_pause_releases_active_worker_and_queued_side():
    import optimizer
    import threading
    import time
    job = optimizer.IndependentOptimizerJob("btcusdt", "60", 500, 100, 100, 2)
    job.status = "running"
    job.active_side = "long"
    job.sides["long"].status = "running"
    assert job.pause()
    worker = threading.Thread(target=job.sides["long"]._wait_if_paused)
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while job.status != "paused" and time.monotonic() < deadline:
            time.sleep(.01)
        assert job.status == "paused"
        assert job.resume()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert not any(side._pause_requested for side in job.sides.values())
    finally:
        job.cancel()
        worker.join(timeout=2)


def test_cancelled_pair_does_not_start_short_search(monkeypatch):
    import optimizer
    job = optimizer.IndependentOptimizerJob("btcusdt", "60", 500, 100, 100, 2)
    monkeypatch.setattr(optimizer, "_fetch_df", lambda *args: candles(args[2] + optimizer.WARMUP_CANDLES))
    searches = []
    def cancel_during_long(child, df):
        searches.append(child.strategy)
        job.cancel()
    monkeypatch.setattr(optimizer.OptimizerJob, "_run_de", cancel_during_long)
    asyncio.run(job.run())
    assert job.status == "cancelled"
    assert searches == ["AdvRSILong"]
    assert job.sides["short"].status == "cancelled"


@pytest.mark.parametrize("field", ["long_credit", "short_credit"])
def test_invalid_directional_credit_rejected(field):
    from server import OptimizerRunRequest
    from pydantic import ValidationError
    for amount in (0, -100, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            OptimizerRunRequest(symbol="btcusdt", strategy="AdvRSIIndependent", timeframe="60", **{field: amount})


def test_changing_short_credit_preserves_long_credit_and_saved_preferences(tmp_path):
    import optimizer
    from storage import Store
    first = optimizer.IndependentOptimizerJob("btcusdt", "60", 500, 120, 300, 2)
    second = optimizer.IndependentOptimizerJob("btcusdt", "60", 500, 120, 700, 2)
    assert first.sides["long"].credit == second.sides["long"].credit == 120
    assert first.credit == 420
    assert second.credit == 820
    store = Store(tmp_path / "prefs.sqlite3")
    manager = optimizer.OptimizerManager(store)
    manager.save_prefs("btcusdt", "AdvRSIIndependent", "60", 500, 999, 2, long_credit=120, short_credit=700)
    recovered = optimizer.OptimizerManager(store).get_config()
    assert recovered["long_credit"] == 120
    assert recovered["short_credit"] == 700


def test_pair_api_and_atomic_creation_restore_individual_settings(monkeypatch, tmp_path):
    import optimizer
    import server
    from fastapi.testclient import TestClient
    from bot_manager import BotManager
    from storage import Store
    monkeypatch.setattr(server, "current_max_hold_days", lambda: 60)
    monkeypatch.setattr(optimizer, "_fetch_df", lambda *args: candles(args[2] + optimizer.WARMUP_CANDLES))
    def search(job, df):
        job.best_params = asdict(optimizer.PARAMS_CLASS[job.strategy]())
        job.best_params["rsi_length"] = 14 if job.strategy == "AdvRSILong" else 38
        job.best_summary = {"total_pnl": 0, "win_rate": 0, "trade_count": 0}
        job.best_pnl = 0
    monkeypatch.setattr(optimizer.OptimizerJob, "_run_de", search)
    manager = BotManager(Store(tmp_path / "api.sqlite3"), tmp_path)
    with TestClient(server.create_app(manager=manager, offline=True, security=False)) as client:
        payload = dict(symbol="btcusdt", strategy="AdvRSIIndependent", timeframe="60", candles=500,
                       long_credit=300, short_credit=700, leverage=2,
                       execution={"entry_fee_rate": .001})
        response = client.post("/api/optimizer/run", json=payload)
        assert response.status_code == 200, response.text
        job_id = response.json()["job_id"]
        import time
        for _ in range(100):
            result = client.get(f"/api/optimizer/run/{job_id}").json()
            if result["status"] in ("done", "error"):
                break
            time.sleep(.02)
        assert result["status"] == "done", result["error"]
        response = client.post(f"/api/optimizer/run/{job_id}/bots", json={})
        assert response.status_code == 201, response.text
        assert len(response.json()["bots"]) == 2
        configs = [b.cfg for b in manager.get_all()]
        assert [cfg.credit for cfg in configs] == [300, 700]
        assert [cfg.strategy_name for cfg in configs] == ["AdvRSILong", "AdvRSIShort"]
        assert [cfg.custom_params["rsi_length"] for cfg in configs] == [14, 38]
        assert all(cfg.mode == "simulation" and cfg.execution["entry_fee_rate"] == .001 for cfg in configs)
        assert client.post(f"/api/optimizer/run/{job_id}/bots", json={"sides": ["long", "long"]}).status_code == 400
        assert len(manager.get_all()) == 2
    restored = BotManager(manager.store, tmp_path)
    restored.load()
    assert {b.cfg.strategy_name: b.cfg.custom_params for b in restored.get_all()} == {
        cfg.strategy_name: cfg.custom_params for cfg in configs}


def test_atomic_bot_creation_does_not_leave_first_bot_when_second_invalid(tmp_path):
    from bot import BotConfig
    from bot_manager import BotManager
    from storage import Store
    manager = BotManager(Store(tmp_path / "atomic.sqlite3"), tmp_path)
    good = BotConfig("btcusdt", "AdvRSILong", "60", 30)
    bad = BotConfig("btcusdt", "unknown", "60", 70)
    with pytest.raises(ValueError):
        manager.create_bots([good, bad])
    assert manager.get_all() == []
    assert manager.store.list("bots") == {}


def test_long_bot_rejects_adopting_short_position(monkeypatch, tmp_path):
    import server
    from bot import BotConfig
    from bot_manager import BotManager
    from fastapi.testclient import TestClient
    from storage import Store
    monkeypatch.setattr(server, "current_max_hold_days", lambda: 60)
    manager = BotManager(Store(tmp_path / "adoption.sqlite3"), tmp_path)
    bot = manager.create_bot(BotConfig("btcusdt", "AdvRSILong", "60", 100, mode="real"))
    fake_client = SimpleNamespace(position=lambda position_id: {
        "id": position_id, "status": "Open", "srcCurrency": "btc", "dstCurrency": "usdt",
        "side": "sell", "entryPrice": "100", "openedAt": "2025-01-01T00:00:00Z"})
    with TestClient(server.create_app(manager=manager, offline=True, security=False)) as client:
        bot = manager.get_bot(bot.id)
        bot._exchange_client = fake_client
        result = client.post(f"/api/bots/{bot.id}/adopt-position", json={"position_id": 123})
        assert result.status_code == 400
        assert "direction" in result.json()["error"]
        assert bot._exchange_position_id is None
        assert manager.store.owner(bot._account_key(), 123) is None
