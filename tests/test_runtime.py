import asyncio
import pandas as pd
import pytest
from bot import Bot, BotConfig
from tests.fakes import synthetic_candles


def test_history_refresh_preserves_boundary_and_old_candles(monkeypatch):
    bot = Bot(BotConfig("btcusdt", "AdvRSI", "60", 100))
    all_data = synthetic_candles(900)
    bot.df = all_data.iloc[:700].copy()
    bot._live_from_ts = all_data.index[300]
    monkeypatch.setattr("data.DataFetcher.fetch", lambda self: all_data.iloc[650:].copy())
    asyncio.run(bot._refresh_data())
    assert len(bot.df) == 900
    assert bot._live_from_ts == all_data.index[300]


def test_unrecoverable_history_gap_preserves_snapshot(monkeypatch):
    bot = Bot(BotConfig("btcusdt", "AdvRSI", "60", 100))
    all_data = synthetic_candles(900)
    bot.df = all_data.iloc[:500].copy()
    monkeypatch.setattr("data.DataFetcher.fetch", lambda self: all_data.iloc[600:].copy())
    with pytest.raises(RuntimeError, match="gap"):
        asyncio.run(bot._refresh_data())
    assert len(bot.df) == 500


def test_duplicate_candle_does_not_repeat_simulation_trade(monkeypatch):
    bot = Bot(BotConfig("btcusdt", "AdvRSI", "60", 100))
    bot.df = synthetic_candles(900)
    bot._live_from_ts = bot.df.index[300]
    bot._run_strategy()
    def forbidden(*args, **kwargs):
        raise AssertionError("Repeated candle replayed")
    monkeypatch.setattr("execution_model.evaluate_research", forbidden)
    bot._run_strategy()


def test_reconfigure_does_not_lose_open_exchange_position(tmp_path):
    from storage import Store
    from bot_manager import BotManager
    manager = BotManager(Store(tmp_path / "db"), tmp_path)
    bot = manager.create_bot(BotConfig("btcusdt", "AdvRSI", "60", 100, mode="real"))
    bot._exchange_position_id = 42
    with pytest.raises(ValueError, match="owned position"):
        manager.update_bot(bot, BotConfig("ethusdt", "AdvRSI", "60", 100, id=bot.id))
    assert bot.cfg.symbol == "btcusdt"
