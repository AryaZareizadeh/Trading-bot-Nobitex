import numpy as np
import pandas as pd
import pytest

from bot import Bot, BotConfig
from price_ai import (TrainedPriceModel, _features, evaluate_price_model,
                      train_and_evaluate, train_price_model)


def candles(count=900):
    t = np.arange(count)
    close = 100 * np.exp(0.00015 * t + 0.035 * np.sin(t / 13))
    return pd.DataFrame({
        "Open": close * (1 - 0.001),
        "High": close * 1.004,
        "Low": close * 0.995,
        "Close": close,
        "Volume": 100 + 10 * np.sin(t / 7),
    }, index=pd.date_range("2026-01-01", periods=count, freq="h"))


EXECUTION = {"entry_fee_rate": 0.001, "exit_fee_rate": 0.001,
             "slippage_bps": 5, "margin_fraction": 0.25}


def test_training_never_uses_heldout_prices():
    original = candles()
    changed = original.copy()
    changed.iloc[700:, :4] *= 3
    first = train_price_model(original, 700)
    second = train_price_model(changed, 700)
    assert first.metadata == second.metadata
    features = _features(original).iloc[[680]]
    assert first.estimator.predict(features).tolist() == second.estimator.predict(features).tolist()


def test_heldout_ai_replay_has_model_and_decision_reasons():
    result = train_and_evaluate(candles(), 100, 1, EXECUTION, train_pct=70)
    assert result["summary"]["candle_count"] == 270
    assert len(result["decisions"]) == 270
    assert result["metadata"]["model"]["training_window_end"] == str(candles().index[629])
    assert all(d["reason"] and d["model_version"] for d in result["decisions"])
    assert result["metadata"]["execution"]["entry_fee_rate"] == 0.001


def test_policy_can_enter_and_exit_on_maximum_holding_period():
    class Forecast:
        def predict(self, frame):
            return np.full(len(frame), 200.0)

    frame = candles(130)
    model = TrainedPriceModel(Forecast(), {"model_version": "test"})
    result = evaluate_price_model(frame, model, 100, 1, EXECUTION, start_idx=60)
    actions = [d["action"] for d in result["decisions"]]
    assert "enter_long" in actions
    assert "exit_long" in actions
    assert result["summary"]["trade_count"] >= 1
    assert all(t["Start Time"] != t["End Time"] for t in result["trades"])


def test_simulation_model_stays_fixed_as_candles_arrive():
    frame = candles()
    bot = Bot(BotConfig("btcusdt", "PriceAI", "60", 100, leverage=1,
                        execution=EXECUTION))
    bot.df = frame.iloc[:800].copy()
    bot._live_from_ts = frame.index[700]
    bot._run_strategy()
    first_model = bot._ai_model
    assert first_model is not None
    assert bot.ai_model_metadata["training_window_end"] == str(frame.index[699])
    bot.df = frame.iloc[:801].copy()
    bot._run_strategy()
    assert bot._ai_model is first_model
    assert bot.ai_decisions[-1]["candle_time"] == str(frame.index[800])


def test_simulation_restart_retrains_only_on_original_history():
    frame = candles()
    cfg = BotConfig("btcusdt", "PriceAI", "240", 100, leverage=1, execution=EXECUTION)
    bot = Bot(cfg)
    bot.df = frame.iloc[:800].copy()
    bot._live_from_ts = frame.index[700]
    bot._run_strategy()
    bot.df = frame.iloc[:810].copy()
    bot._run_strategy()
    snapshot = bot.snapshot()

    restored = Bot(cfg)
    restored.restore(snapshot)
    restored.df = frame.iloc[:811].copy()
    restored._run_strategy()
    bot.df = frame.iloc[:811].copy()
    bot._run_strategy()

    assert restored.ai_model_metadata == bot.ai_model_metadata
    assert restored.ai_decisions == bot.ai_decisions
    assert restored.completed_trades == bot.completed_trades
    assert restored.open_positions == bot.open_positions


def test_price_ai_cannot_be_created_for_real_trading_or_fast_candles():
    with pytest.raises(ValueError, match="simulation mode only"):
        Bot(BotConfig("btcusdt", "PriceAI", "60", 100, mode="real"))
    with pytest.raises(ValueError, match="1-hour and 4-hour"):
        Bot(BotConfig("btcusdt", "PriceAI", "5", 100, execution=EXECUTION))
    with pytest.raises(ValueError, match="positive entry fee"):
        Bot(BotConfig("btcusdt", "PriceAI", "60", 100))
    with pytest.raises(ValueError, match="net positions and next-open fills"):
        Bot(BotConfig("btcusdt", "PriceAI", "60", 100,
                      execution={**EXECUTION, "fill_timing": "legacy_close"}))


def test_price_ai_api_requires_costs_and_never_accepts_real_mode(tmp_path):
    from tests.test_dashboard_flow import client_for, login

    client, manager = client_for(tmp_path)
    request = {"symbol": "btcusdt", "strategy_name": "PriceAI", "timeframe": "60", "credit": 100}
    with client:
        headers = login(client)
        assert client.post("/api/bots", headers=headers, json=request).status_code == 400
        assert client.post("/api/bots", headers=headers,
                           json={**request, "mode": "real", "execution": EXECUTION}).status_code == 400
        assert client.post("/api/settings/execution", headers=headers, json=EXECUTION).status_code == 200
        created = client.post("/api/bots", headers=headers, json=request)
        assert created.status_code == 201, created.text
        bot_id = created.json()["id"]
        assert created.json()["strategy"] == "PriceAI"
        assert client.patch(f"/api/bots/{bot_id}", headers=headers,
                            json={"mode": "real"}).status_code == 400
        assert manager.get_bot(bot_id).cfg.mode == "simulation"
