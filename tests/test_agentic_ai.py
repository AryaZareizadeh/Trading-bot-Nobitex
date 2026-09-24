import numpy as np
import pandas as pd
import pytest
import asyncio

from agentic_ai import decide, feature_matrix, train_agent, validate_timeframes
from bot import Bot, BotConfig


EXECUTION = {"entry_fee_rate": .001, "exit_fee_rate": .001, "slippage_bps": 5}


def candles(count=2600, freq="15min"):
    rng = np.random.default_rng(19)
    returns = rng.normal(0, .006, count)
    close = 100 * np.exp(np.cumsum(returns))
    return pd.DataFrame({"Open": close, "High": close * 1.008,
        "Low": close * .992, "Close": close,
        "Volume": 100 + rng.random(count) * 20},
        index=pd.date_range("2025-01-01", periods=count, freq=freq))


def higher(frame, rule="1h"):
    return frame.resample(rule).agg({"Open": "first", "High": "max", "Low": "min",
                                      "Close": "last", "Volume": "sum"})


def test_only_completed_higher_candles_are_visible():
    base = candles(300, "1h")
    four_hours = higher(base, "4h")
    original = feature_matrix({"60": base, "240": four_hours}, "60", ["60", "240"])
    changed = four_hours.copy()
    candle = changed.index[45]
    changed.loc[candle, ["Open", "High", "Low", "Close"]] *= 2
    future = feature_matrix({"60": base, "240": changed}, "60", ["60", "240"])
    # The changed 4h bar opens at 00:00 and is not available until 04:00.
    before = candle + pd.Timedelta(hours=2)
    after = candle + pd.Timedelta(hours=4)
    pd.testing.assert_series_equal(original.loc[before], future.loc[before])
    assert not original.loc[after].equals(future.loc[after])


def test_training_ignores_later_prices_and_reports_disjoint_validation():
    base = candles()
    hour = higher(base)
    boundary = 1800
    original = train_agent({"15": base, "60": hour}, "15", ["15", "60"], EXECUTION, boundary)
    changed = base.copy()
    changed.iloc[boundary:, :4] *= 3
    later = train_agent({"15": changed, "60": hour}, "15", ["15", "60"], EXECUTION, boundary)
    assert original.metadata == later.metadata
    assert original.metadata["ensemble_validation"]["validation_rows"] > 100


def test_decision_agent_abstains_enters_holds_and_exits():
    strong = {"60": {"long_probability": .75, "short_probability": .10},
              "240": {"long_probability": .71, "short_probability": .12},
              "agent": {"long_probability": .73, "short_probability": .11}}
    assert decide(strong)["action"] == "enter_long"
    assert decide(strong)["confidence"] == .73
    assert decide(strong, "long")["action"] == "hold"
    assert decide(strong, "long", -.11)["action"] == "exit"
    weak = {"60": {"long_probability": .52, "short_probability": .37},
            "240": {"long_probability": .48, "short_probability": .40},
            "agent": {"long_probability": .50, "short_probability": .39}}
    assert decide(weak)["action"] == "abstain"
    assert decide(weak)["confidence"] is None


def test_live_bot_waits_for_new_completed_primary_candle(monkeypatch):
    base = candles(300, "1h")
    bot = Bot(BotConfig("btcusdt", "AgenticAI", "60", 100, leverage=1,
                        mode="real", execution=EXECUTION, ai_timeframes=["60", "240"]))
    bot.df = base
    bot._ai_frames = {"60": base, "240": higher(base, "4h")}
    bot._ai_model = object()
    bot.ai_model_metadata = {"validation_passed": True}
    bot._live_from_ts = base.index[-1] + pd.Timedelta(hours=1)
    bot._run_strategy()
    assert bot._agent_signal is None
    monkeypatch.setattr("bot.probability_evidence", lambda *_: {
        "60": {"long_probability": .75, "short_probability": .10},
        "240": {"long_probability": .72, "short_probability": .11},
        "agent": {"long_probability": .74, "short_probability": .10}})
    bot._live_from_ts = base.index[-1]
    bot._run_strategy()
    assert bot._agent_signal == "long"
    assert bot.ai_decisions[-1]["confidence"] == .74


def test_invalid_timeframes_and_costs_fail_closed():
    with pytest.raises(ValueError, match="two or three"):
        validate_timeframes("60", ["60"])
    with pytest.raises(ValueError, match="at most 24"):
        validate_timeframes("15", ["15", "D"])
    with pytest.raises(ValueError, match="positive fee"):
        Bot(BotConfig("btcusdt", "AgenticAI", "60", 100, mode="real",
                      ai_timeframes=["60", "240"]))
    with pytest.raises(ValueError, match="limited to 5x"):
        Bot(BotConfig("btcusdt", "AgenticAI", "60", 100, leverage=6,
                      mode="real", execution=EXECUTION, ai_timeframes=["60", "240"]))


def test_ai_timeframes_persist_and_bulk_start_skips_agent(tmp_path):
    from tests.test_dashboard_flow import client_for, login

    client, manager = client_for(tmp_path)
    with client:
        headers = login(client)
        ai = client.post("/api/bots", headers=headers, json={
            "symbol": "btcusdt", "strategy_name": "AgenticAI", "timeframe": "60",
            "ai_timeframes": ["60", "240", "D"], "credit": 100,
            "leverage": 2, "mode": "real", "execution": EXECUTION})
        assert ai.status_code == 201, ai.text
        ai_id = ai.json()["id"]
        assert ai.json()["ai_timeframes"] == ["60", "240", "D"]
        changed = client.patch(f"/api/bots/{ai_id}", headers=headers,
                               json={"ai_timeframes": ["60", "240"]})
        assert changed.status_code == 200, changed.text
        assert changed.json()["ai_timeframes"] == ["60", "240"]
        standard = client.post("/api/bots", headers=headers, json={
            "symbol": "ethusdt", "strategy_name": "AdvRSI", "timeframe": "60",
            "credit": 100})
        assert standard.status_code == 201
        started = []
        for bot in manager.get_all():
            async def record(bot_id=bot.id):
                started.append(bot_id)
            bot.start = record
        response = client.post("/api/bots/actions/start-all", headers=headers)
        assert response.status_code == 200
        assert started == [standard.json()["id"]]
    next_client, next_manager = client_for(tmp_path)
    with next_client:
        login(next_client)
        assert next_manager.get_bot(ai_id).cfg.ai_timeframes == ["60", "240"]


def test_agent_does_not_submit_order_before_a_new_valid_decision(tmp_path):
    from storage import Store

    class Exchange:
        def positions(self, status):
            assert status == "active"
            return []

        def __getattr__(self, name):
            raise AssertionError(f"Unexpected exchange call: {name}")

    bot = Bot(BotConfig("btcusdt", "AgenticAI", "60", 100, leverage=1,
                        mode="real", execution=EXECUTION, ai_timeframes=["60", "240"]))
    bot._store = Store(tmp_path / "orders.sqlite3")
    bot._exchange_client = Exchange()
    asyncio.run(bot._sync_exchange())
    assert bot._store.list("orders") == {}


def test_simulation_snapshot_preserves_higher_timeframe_candles():
    base = candles(300, "1h")
    cfg = BotConfig("btcusdt", "AgenticAI", "60", 100, leverage=1,
                    execution=EXECUTION, ai_timeframes=["60", "240"])
    bot = Bot(cfg)
    bot.df = base
    bot._ai_frames = {"60": base, "240": higher(base, "4h")}
    saved = bot.snapshot()
    restored = Bot(cfg)
    restored.restore(saved)
    pd.testing.assert_frame_equal(restored._ai_frames["240"], bot._ai_frames["240"],
                                  check_names=False, check_freq=False)
