"""Live reconciliation must progress while waiting for the next candle."""
import asyncio

import pandas as pd
import pytest

from bot import BotStatus
from exchange_execution import OrderExecutor
from tests.test_recovery import Exchange, make_bot


def pending_bot(tmp_path, monkeypatch, *, timeframe="60", response="filled"):
    bot, exchange = make_bot(tmp_path), Exchange()
    bot.cfg.timeframe = timeframe
    bot.df = pd.DataFrame({name: [100.] for name in ("Open", "High", "Low", "Close", "Volume")},
                          index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]))
    bot._exchange_client = exchange
    bot.status = BotStatus.RUNNING
    executor = OrderExecutor(bot, exchange)
    intent = executor._new("open", "btc", "usdt", "buy", {"amount": "1", "price": "100"}, [])
    asyncio.run(executor.submit(intent))
    observed = {"waits": [], "strategy_runs": 0, "candle_fetches": 0}
    initial_status = exchange.order_status
    status_calls = 0

    def status(order_id):
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1 or response == "unavailable":
            raise TimeoutError("Temporary exchange outage")
        order = initial_status(order_id)
        if response == "pending":
            return dict(order, status="Active", matchedAmount="0")
        return order

    exchange.order_status = status
    now = [pd.Timestamp("2026-01-01T01:00:10").timestamp()]
    monkeypatch.setattr("bot.time.time", lambda: now[0])

    async def refresh():
        pass

    def strategy():
        observed["strategy_runs"] += 1

    def fetch_latest(_):
        observed["candle_fetches"] += 1
        return bot.df.copy()

    async def wait(awaitable, timeout):
        awaitable.close()
        observed["waits"].append(timeout)
        if len(observed["waits"]) == 1:
            now[0] += timeout
            raise asyncio.TimeoutError
        bot._stop_event.set()
        return None

    monkeypatch.setattr(bot, "_refresh_data", refresh)
    monkeypatch.setattr(bot, "_run_strategy", strategy)
    monkeypatch.setattr("bot.DataFetcher.fetch_latest", fetch_latest)
    monkeypatch.setattr("bot.asyncio.wait_for", wait)
    return bot, exchange, observed


@pytest.mark.parametrize("timeframe", ["60", "D", "M"])
def test_known_fill_recovers_before_next_candle_without_replaying_signal(tmp_path, monkeypatch, timeframe):
    bot, exchange, observed = pending_bot(tmp_path, monkeypatch, timeframe=timeframe)
    asyncio.run(bot._run_loop())
    assert observed["waits"][0] <= 30
    assert bot._exchange_position_id == 42
    assert not bot.unresolved_intents()
    assert bot.error is None
    assert bot._store.get("runtime", bot.id)["error"] is None
    assert observed["strategy_runs"] == 1
    assert observed["candle_fetches"] == 0
    assert len(exchange.sent) == 1


@pytest.mark.parametrize("response", ["pending", "unavailable"])
def test_unresolved_order_keeps_retrying_without_stopping_or_submitting_again(tmp_path, monkeypatch, response):
    bot, exchange, observed = pending_bot(tmp_path, monkeypatch, response=response)
    asyncio.run(bot._run_loop())
    assert observed["waits"] and all(wait <= 30 for wait in observed["waits"])
    assert bot.status == BotStatus.STOPPED  # The test requests a normal stop on its second wait.
    assert bot.unresolved_intents()
    assert bot.error
    assert len(exchange.sent) == 1
    assert observed["strategy_runs"] == 1
    assert observed["candle_fetches"] == 0


def test_open_uses_current_quote_for_price_and_amount(tmp_path, monkeypatch):
    bot, exchange = make_bot(tmp_path), Exchange()
    bot.df = pd.DataFrame({"Close": [100.]}, index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]))
    monkeypatch.setattr(bot, "_fetch_delegation_limit", lambda *args: None)
    monkeypatch.setattr(exchange, "live_price", lambda symbol: 250., raising=False)
    asyncio.run(bot._place_single_order(exchange, "btc", "usdt", "buy", 1))
    assert exchange.sent[0]["price"] == "250.00"
    assert exchange.sent[0]["amount"] == "0.400000"


@pytest.mark.parametrize("quote", [0., -1., float("nan"), float("inf")])
def test_invalid_open_quote_never_creates_or_submits_order(tmp_path, monkeypatch, quote):
    bot, exchange = make_bot(tmp_path), Exchange()
    bot.df = pd.DataFrame({"Close": [100.]}, index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]))
    monkeypatch.setattr(bot, "_fetch_delegation_limit", lambda *args: None)
    monkeypatch.setattr(exchange, "live_price", lambda symbol: quote, raising=False)
    with pytest.raises(ValueError, match="price|quote"):
        asyncio.run(bot._place_single_order(exchange, "btc", "usdt", "buy", 1))
    assert not exchange.sent
    assert not bot._store.list("orders")


def test_failed_open_quote_never_falls_back_to_old_candle(tmp_path, monkeypatch):
    bot, exchange = make_bot(tmp_path), Exchange()
    bot.df = pd.DataFrame({"Close": [100.]}, index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]))
    monkeypatch.setattr(bot, "_fetch_delegation_limit", lambda *args: None)

    def quote(symbol):
        raise TimeoutError("Quote unavailable")

    monkeypatch.setattr(exchange, "live_price", quote, raising=False)
    with pytest.raises(TimeoutError, match="Quote unavailable"):
        asyncio.run(bot._place_single_order(exchange, "btc", "usdt", "buy", 1))
    assert not exchange.sent
    assert not bot._store.list("orders")
