import pandas as pd
import pytest

from bot import Bot, BotConfig, BotStatus
from tests.fakes import synthetic_candles


@pytest.mark.parametrize('timeframe,opened,next_close', [
    ('60', '2026-10-04T08:30:00Z', '2026-10-04T10:30:00Z'),
    ('D', '2026-10-02T20:30:00Z', '2026-10-04T20:30:00Z'),
    ('M', '2026-08-31T20:30:00Z', '2026-10-31T20:30:00Z'),
])
def test_countdown_uses_exchange_boundaries(monkeypatch, timeframe, opened, next_close):
    bot = Bot(BotConfig('btcusdt', 'AdvRSI', timeframe, 100))
    bot.df = synthetic_candles(2)
    bot.df.index = pd.DatetimeIndex([pd.Timestamp(opened) - pd.Timedelta(days=32), pd.Timestamp(opened)])
    bot.status = BotStatus.RUNNING
    monkeypatch.setattr('bot.time.time', lambda: pd.Timestamp('2026-10-04T09:00:00Z').timestamp())
    state = bot.to_state_dict()
    assert state['next_candle_close'] == pd.Timestamp(next_close).timestamp()
    assert state['last_candle_open'] == pd.Timestamp(opened).timestamp()


def test_chart_revision_changes_only_when_candles_change():
    bot = Bot(BotConfig('btcusdt', 'AdvRSI', '60', 100))
    bot.df = synthetic_candles(100)
    first = bot.to_state_dict()['chart_revision']
    bot.current_price = 999
    assert bot.to_state_dict()['chart_revision'] == first
    assert bot.get_chart_data()['chart_revision'] == first
    bot.df = synthetic_candles(101)
    assert bot.to_state_dict()['chart_revision'] != first


def test_chart_revision_changes_after_candle_strategy_finishes():
    from datetime import datetime, timezone
    bot = Bot(BotConfig('btcusdt', 'AdvRSI', '60', 100))
    bot.df = synthetic_candles(100)
    first = bot.to_state_dict()['chart_revision']
    bot._last_processed_ts = bot.df.index[-1]
    bot.last_updated = datetime.now(timezone.utc)
    assert bot.to_state_dict()['chart_revision'] != first


def test_overlapping_delivery_keeps_new_candles():
    bot = Bot(BotConfig('btcusdt', 'AdvRSI', '60', 100))
    candles = synthetic_candles(105)
    bot.df = candles.iloc[:100].copy()
    assert bot._validate_and_append(candles.iloc[98:])
    assert list(bot.df.index) == list(candles.index)


def test_idle_bot_has_no_countdown():
    bot = Bot(BotConfig('btcusdt', 'AdvRSI', '60', 100))
    assert bot.to_state_dict()['next_candle_close'] is None


def test_running_bot_waits_for_delayed_candle_then_processes_two_closes(monkeypatch):
    import asyncio
    candles = synthetic_candles(102)
    candles.index = pd.date_range('2026-10-04T08:30:00Z', periods=102, freq='min')
    bot = Bot(BotConfig('btcusdt', 'AdvRSI', '1', 100))
    bot.status = BotStatus.RUNNING
    bot.df = candles.iloc[:100].copy()
    clock = [candles.index[99].timestamp() + 60]
    waits, evaluations, delivered = [], [], [99, 99, 100, 101]

    async def nothing():
        pass

    async def wait(coroutine, timeout):
        coroutine.close()
        waits.append(timeout)
        clock[0] += timeout
        raise asyncio.TimeoutError

    def latest(fetcher):
        index = delivered.pop(0)
        if not delivered:
            bot._stop_event.set()
        return candles.iloc[index:index + 1].copy()

    def strategy():
        evaluations.append(bot.df.index[-1])
        bot.current_price = float(bot.df.Close.iloc[-1])

    monkeypatch.setattr(bot, '_refresh_data', nothing)
    monkeypatch.setattr(bot, '_sync_exchange', nothing)
    monkeypatch.setattr(bot, '_run_strategy', strategy)
    monkeypatch.setattr('bot.time.time', lambda: clock[0])
    monkeypatch.setattr('bot.asyncio.wait_for', wait)
    monkeypatch.setattr('data.DataFetcher.fetch_latest', latest)
    monkeypatch.setattr('position_limits.current_max_hold_days', lambda: 30)
    asyncio.run(bot._run_loop())
    assert evaluations == list(candles.index[99:102])
    assert waits[2] > waits[1]  # Missing publication backs off instead of a tight loop.
    assert bot.df.index[-1] == candles.index[101]


def test_rest_and_websocket_share_quotes_without_http(tmp_path):
    import time
    from fastapi.testclient import TestClient
    from bot_manager import BotManager
    from market_quotes import MarketQuotes
    from server import create_app
    from storage import Store
    from tests.test_market_quotes import Response

    calls = []
    def get(*args, **kwargs):
        calls.append(1)
        return Response()

    quotes = MarketQuotes(tmp_path / 'quotes.sqlite3', get=get)
    quotes.refresh_due()
    manager = BotManager(Store(tmp_path / 'bots.sqlite3'), tmp_path)
    with TestClient(create_app(manager=manager, offline=True, security=False, market_quotes=quotes)) as client:
        bot = manager.create_bot(BotConfig('btcusdt', 'AdvRSI', '60', 100))
        bot.df = synthetic_candles(100)
        bot.current_price = 90
        state = client.get('/api/bots').json()[0]
        detail = client.get(f'/api/bots/{bot.id}').json()
        assert state['current_price'] == detail['current_price'] == 90
        assert state['live_price'] == detail['live_price'] == 101.5
        with client.websocket_connect('/ws') as socket:
            payload = socket.receive_json()
            assert payload['bots'][0]['live_price'] == 101.5
            assert abs(payload['server_time'] - time.time()) < 5
        assert calls == [1]


def test_unavailable_public_quote_cache_does_not_break_bot_updates(tmp_path, monkeypatch):
    import sqlite3
    from fastapi.testclient import TestClient
    from bot_manager import BotManager
    from market_quotes import MarketQuotes
    from server import create_app
    from storage import Store

    quotes = MarketQuotes(tmp_path / 'quotes.sqlite3')
    def unavailable():
        raise sqlite3.OperationalError('disk unavailable')
    monkeypatch.setattr(quotes, 'snapshot', unavailable)
    manager = BotManager(Store(tmp_path / 'bots.sqlite3'), tmp_path)
    with TestClient(create_app(manager=manager, offline=True, security=False, market_quotes=quotes)) as client:
        manager.create_bot(BotConfig('btcusdt', 'AdvRSI', '60', 100))
        assert client.get('/api/bots').json()[0]['live_price'] is None
        with client.websocket_connect('/ws') as socket:
            assert socket.receive_json()['bots'][0]['live_price_stale'] is True
