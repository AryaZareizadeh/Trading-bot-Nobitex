"""Candle transport failures pause evaluation while live state keeps running."""
import asyncio

import pandas as pd
import pytest
import requests

from bot import Bot, BotConfig, BotStatus
from exchange_execution import OrderExecutor
from tests.test_recovery import Exchange, make_bot


def active_bot(tmp_path, monkeypatch, config=None):
    bot = make_bot(tmp_path)
    if config is not None:
        store = bot._store
        bot = Bot(config)
        bot._store = store
    old = pd.DataFrame({name: [100.] for name in ("Open", "High", "Low", "Close", "Volume")},
                       index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]))
    new = old.copy()
    new.index += pd.Timedelta(hours=1)
    bot.df, bot.status = old.copy(), BotStatus.RUNNING
    bot._exchange_position_id, bot._real_state = 42, "long"
    bot._seed_position = {"direction": "Long", "entry_price": 100., "entry_time": "2026-01-01T00:00:00Z"}
    bot._store.claim_position(bot._account_key(), 42, bot.id)
    clock = [pd.Timestamp("2026-01-01T02:00:10").timestamp()]
    observed = {"waits": [], "snapshots": [], "evaluated": [], "synced": []}
    monkeypatch.setattr("bot.time.time", lambda: clock[0])

    def strategy():
        observed["evaluated"].append(bot.df.index[-1])
        if bot.df.index[-1] == new.index[-1]:
            bot._stop_event.set()

    async def sync():
        observed["synced"].append(bot.df.index[-1])

    monkeypatch.setattr(bot, "_run_strategy", strategy)
    monkeypatch.setattr(bot, "_sync_exchange", sync)
    return bot, old, new, clock, observed


@pytest.mark.parametrize("failure", [requests.ConnectionError, requests.exceptions.SSLError,
                                      requests.exceptions.ProxyError, requests.Timeout,
                                      requests.exceptions.ChunkedEncodingError])
def test_candle_transport_failure_retries_and_evaluates_only_after_recovery(tmp_path, monkeypatch, failure):
    bot, old, new, clock, observed = active_bot(tmp_path, monkeypatch)
    histories, latest_calls = [], []

    def history(_):
        histories.append(1)
        if len(histories) > 1:
            raise failure("Nobitex temporarily unreachable")
        return old.copy()

    def latest(_):
        latest_calls.append(1)
        if len(latest_calls) == 1:
            raise failure("Nobitex temporarily unreachable")
        return new.copy()

    async def wait(coroutine, timeout):
        coroutine.close()
        observed["waits"].append(timeout)
        observed["snapshots"].append(bot._store.get("runtime", bot.id))
        clock[0] += timeout
        raise asyncio.TimeoutError

    monkeypatch.setattr("bot.DataFetcher.fetch", history)
    monkeypatch.setattr("bot.DataFetcher.fetch_latest", latest)
    monkeypatch.setattr("bot.asyncio.wait_for", wait)
    asyncio.run(bot._run_loop())
    assert bot.status == BotStatus.STOPPED  # Strategy requests a normal stop after the recovered candle.
    assert len(observed["waits"]) == 2
    outage = observed["snapshots"][1]
    assert outage["desired_status"] == "running"
    assert outage["_exchange_position_id"] == 42
    assert outage["candles"]["index"] == ["2026-01-01T00:00:00"]
    assert "Candle data unavailable" in outage["error"]
    assert observed["evaluated"] == [old.index[-1], new.index[-1]]
    assert observed["synced"] == [old.index[-1], new.index[-1]]
    assert bot.error is None
    assert any(event["event"] == "warning" for event in bot.activity_log)


def test_continued_outage_backs_off_and_reconciles_orders_before_candle_retries(tmp_path, monkeypatch):
    bot, old, _, clock, observed = active_bot(tmp_path, monkeypatch)
    exchange = Exchange()
    exchange.active = [{"id": 42, "srcCurrency": "btc", "dstCurrency": "usdt", "side": "buy", "status": "Open",
                        "entryPrice": "100", "openedAt": "2026-01-01T00:00:00Z"}]
    exchange.orders[28] = {"id": 28, "status": "Active", "amount": "1", "matchedAmount": "0"}
    bot._exchange_client = exchange
    executor = OrderExecutor(bot, exchange)
    intent = executor._new("close", "btc", "usdt", "long", {"position_id": 42, "amount": "1", "price": "100"}, [42])
    intent.update(order_id=28, state="acknowledged")
    executor.save(intent)
    events, history_count = [], 0

    def history(_):
        nonlocal history_count
        history_count += 1
        if history_count == 1:
            return old.copy()
        events.append("history")
        raise requests.ConnectionError("DNS unavailable")

    def latest(_):
        events.append("latest")
        raise requests.ConnectionError("DNS unavailable")

    def order_status(order_id):
        events.append("reconcile")
        return exchange.orders[order_id]

    async def wait(coroutine, timeout):
        coroutine.close()
        observed["waits"].append(timeout)
        observed["snapshots"].append(bot._store.get("runtime", bot.id))
        if len(observed["waits"]) == 6:
            bot._stop_event.set()
            return
        clock[0] += timeout
        raise asyncio.TimeoutError

    monkeypatch.setattr("bot.DataFetcher.fetch", history)
    monkeypatch.setattr("bot.DataFetcher.fetch_latest", latest)
    monkeypatch.setattr(exchange, "order_status", order_status)
    monkeypatch.setattr("bot.asyncio.wait_for", wait)
    asyncio.run(bot._run_loop())
    assert bot.status == BotStatus.STOPPED
    assert observed["waits"][1:5] == [10, 20, 30, 30]
    assert all(saved["desired_status"] == "running" for saved in observed["snapshots"])
    assert events == ["reconcile", "latest", "history"] * 5
    assert observed["evaluated"] == [old.index[-1]]
    assert observed["synced"] == [old.index[-1]]
    assert bot._exchange_position_id == 42
    assert bot.unresolved_intents()
    assert not exchange.sent


def test_stop_event_interrupts_candle_network_backoff(tmp_path, monkeypatch):
    bot, old, _, clock, observed = active_bot(tmp_path, monkeypatch)
    history_count = 0
    actual_wait_for = asyncio.wait_for

    def history(_):
        nonlocal history_count
        history_count += 1
        if history_count == 1:
            return old.copy()
        raise requests.Timeout("Candle request timed out")

    def latest(_):
        raise requests.Timeout("Candle request timed out")

    async def wait(coroutine, timeout):
        observed["waits"].append(timeout)
        if len(observed["waits"]) == 1:
            coroutine.close()
            clock[0] += timeout
            raise asyncio.TimeoutError
        bot._stop_event.set()
        return await actual_wait_for(coroutine, timeout)

    monkeypatch.setattr("bot.DataFetcher.fetch", history)
    monkeypatch.setattr("bot.DataFetcher.fetch_latest", latest)
    monkeypatch.setattr("bot.asyncio.wait_for", wait)
    asyncio.run(bot._run_loop())
    assert bot.status == BotStatus.STOPPED
    assert observed["waits"] == [5, 10]
    assert history_count == 2
    assert observed["evaluated"] == [old.index[-1]]


@pytest.mark.parametrize("failure", [ValueError("Invalid OHLCV"), RuntimeError("Candle invariant failed")])
def test_invalid_candle_data_still_fails_without_transport_retry(tmp_path, monkeypatch, failure):
    bot, old, _, clock, observed = active_bot(tmp_path, monkeypatch)

    def latest(_):
        raise failure

    async def wait(coroutine, timeout):
        coroutine.close()
        observed["waits"].append(timeout)
        if len(observed["waits"]) == 1:
            clock[0] += timeout
            raise asyncio.TimeoutError
        bot._stop_event.set()

    monkeypatch.setattr("bot.DataFetcher.fetch", lambda _: old.copy())
    monkeypatch.setattr("bot.DataFetcher.fetch_latest", latest)
    monkeypatch.setattr("bot.asyncio.wait_for", wait)
    asyncio.run(bot._run_loop())
    assert bot.status == BotStatus.ERROR
    assert bot.error == str(failure)
    assert observed["waits"] == [5]


def test_warmup_transport_failures_keep_existing_ten_attempt_limit(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    bot.status = BotStatus.RUNNING
    histories = []

    def history(_):
        histories.append(1)
        raise requests.ConnectionError("No initial history")

    async def sleep(_):
        pass

    monkeypatch.setattr("bot.DataFetcher.fetch", history)
    monkeypatch.setattr("bot.asyncio.sleep", sleep)
    asyncio.run(bot._run_loop())
    assert len(histories) == 10
    assert bot.status == BotStatus.ERROR
    assert bot.df is None


@pytest.mark.parametrize("pending_order", [False, True])
def test_higher_frame_outage_retries_and_evaluates_appended_primary_once(tmp_path, monkeypatch, pending_order):
    cfg = BotConfig("btcusdt", "AgenticAI", "60", 100, mode="real", leverage=1, id="one",
                    ai_timeframes=["60", "240", "D"],
                    execution={"entry_fee_rate": .001, "exit_fee_rate": .001, "slippage_bps": 5})
    bot, old, new, clock, observed = active_bot(tmp_path, monkeypatch, cfg)
    four_hours, daily = old.copy(), old.copy()
    four_hours.index = pd.DatetimeIndex([pd.Timestamp("2025-12-31T20:00:00")])
    daily.index = pd.DatetimeIndex([pd.Timestamp("2025-12-30T20:30:00")])
    frames = {"60": old, "240": four_hours, "D": daily}
    events, daily_requests = [], 0

    if pending_order:
        exchange = Exchange()
        exchange.active = [{"id": 42, "srcCurrency": "btc", "dstCurrency": "usdt", "side": "buy", "status": "Open",
                            "entryPrice": "100", "openedAt": "2026-01-01T00:00:00Z"}]
        exchange.orders[28] = {"id": 28, "status": "Active", "amount": "1", "matchedAmount": "0"}
        bot._exchange_client = exchange
        executor = OrderExecutor(bot, exchange)
        intent = executor._new("close", "btc", "usdt", "long", {"position_id": 42, "amount": "1", "price": "100"}, [42])
        intent.update(order_id=28, state="acknowledged")
        executor.save(intent)

        def order_status(order_id):
            events.append("reconcile")
            return exchange.orders[order_id]

        monkeypatch.setattr(exchange, "order_status", order_status)

    def latest(fetcher):
        nonlocal daily_requests
        interval = fetcher.config.interval
        events.append(interval)
        if interval == "60":
            return new.copy()  # The retry repeats the primary candle already appended.
        if interval == "D":
            daily_requests += 1
            if daily_requests == 1:
                raise requests.exceptions.SSLError("Higher candle TLS connection interrupted")
        return frames[interval].copy()

    async def wait(coroutine, timeout):
        coroutine.close()
        observed["waits"].append(timeout)
        observed["snapshots"].append(bot._store.get("runtime", bot.id))
        if len(observed["waits"]) > 2:
            bot._stop_event.set()
            return
        clock[0] += timeout
        raise asyncio.TimeoutError

    monkeypatch.setattr("bot.DataFetcher.fetch", lambda fetcher: frames[fetcher.config.interval].copy())
    monkeypatch.setattr("bot.DataFetcher.fetch_latest", latest)
    monkeypatch.setattr("bot.asyncio.wait_for", wait)
    asyncio.run(bot._run_loop())
    assert bot.status == BotStatus.STOPPED
    assert observed["waits"] == [5, 10]
    outage = observed["snapshots"][1]
    assert outage["desired_status"] == "running"
    assert outage["_exchange_position_id"] == 42
    assert outage["candles"]["index"] == ["2026-01-01T00:00:00", "2026-01-01T01:00:00"]
    assert "Candle data unavailable" in outage["error"]
    expected_cycle = ["reconcile", "60", "240", "D"] if pending_order else ["60", "240", "D"]
    assert events == expected_cycle * 2
    assert observed["evaluated"] == [old.index[-1], new.index[-1]]
    assert observed["synced"] == [old.index[-1], new.index[-1]]
    assert bot.error is None or pending_order and "Unresolved exchange order" in bot.error


def test_invalid_higher_frame_data_still_sets_error(tmp_path, monkeypatch):
    cfg = BotConfig("btcusdt", "AgenticAI", "60", 100, mode="real", leverage=1, id="one",
                    ai_timeframes=["60", "240"],
                    execution={"entry_fee_rate": .001, "exit_fee_rate": .001, "slippage_bps": 5})
    bot, old, new, clock, observed = active_bot(tmp_path, monkeypatch, cfg)

    def latest(fetcher):
        if fetcher.config.interval == "240":
            raise ValueError("Invalid higher-frame OHLCV")
        return new.copy()

    async def wait(coroutine, timeout):
        coroutine.close()
        observed["waits"].append(timeout)
        clock[0] += timeout
        raise asyncio.TimeoutError

    monkeypatch.setattr("bot.DataFetcher.fetch", lambda _: old.copy())
    monkeypatch.setattr("bot.DataFetcher.fetch_latest", latest)
    monkeypatch.setattr("bot.asyncio.wait_for", wait)
    asyncio.run(bot._run_loop())
    assert bot.status == BotStatus.ERROR
    assert bot.error == "Invalid higher-frame OHLCV"
    assert observed["evaluated"] == [old.index[-1]]
    assert observed["synced"] == [old.index[-1]]
