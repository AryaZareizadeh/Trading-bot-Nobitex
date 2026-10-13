"""Offline regressions for owned fills, settlement and provider calendars."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from bot import Bot, BotConfig
from config import Config
from exchange_execution import OrderExecutor
from indicators import Indicators
from storage import Store
from strategy import AdvRSICombinedStrategy, AdvRsiParams, TrendFilteredMomentumStrategy
from tests.test_recovery import Exchange, closing_intent, make_bot


def frame(prices, index=None):
    return pd.DataFrame({name: prices for name in ("Open", "High", "Low", "Close", "Volume")},
                        index=index if index is not None else pd.date_range("2026-01-01", periods=len(prices), freq="h"))


def indicators(monkeypatch, df, k=None, d=None):
    k = pd.Series(k if k is not None else [40.] * len(df), index=df.index)
    d = pd.Series(d if d is not None else k, index=df.index)
    monkeypatch.setattr(Indicators, "stoch_rsi", lambda *args: SimpleNamespace(k=k, d=d))
    monkeypatch.setattr(Indicators, "rsi_with_ma", lambda *args: (k*0+45, k*0+40))
    monkeypatch.setattr(Indicators, "ema", lambda *args: k*0+90)
    monkeypatch.setattr(Indicators, "macd", lambda *args: SimpleNamespace(hist=k*0+1))
    return Indicators(df)


@pytest.mark.parametrize("strategy_cls", [AdvRSICombinedStrategy, TrendFilteredMomentumStrategy])
def test_seed_does_not_replay_preentry_liquidation(monkeypatch, strategy_cls):
    df = frame([70., 70., 110., 95.])
    seed = {"direction": "Long", "entry_price": 100., "entry_time": str(df.index[2] + pd.Timedelta(seconds=5))}
    strategy = strategy_cls(Config(leverage=5, max_hold_days=30))
    records = strategy.run(df, indicators(monkeypatch, df), seed_positions=[seed])
    assert strategy.open_positions == [{"direction": "Long", "entry_price": 100., "entry_time": seed["entry_time"]}]
    assert not records["Liquidated"].any()
    assert (pd.to_datetime(records["End Time"]).values >= pd.to_datetime(records["Start Time"]).values).all()


def test_owned_seed_suppresses_duplicate_historical_entry(monkeypatch):
    df = frame([100.] * 5)
    seed = {"direction": "Long", "entry_price": 99., "entry_time": str(df.index[3] + pd.Timedelta(seconds=5))}
    strategy = AdvRSICombinedStrategy(Config(max_hold_days=30), AdvRsiParams(entry_wait_candles=0))
    strategy.run(df, indicators(monkeypatch, df, [10, 30, 40, 40, 40]), seed_positions=[seed])
    assert len(strategy.open_positions) == 1
    assert strategy.open_positions[0]["entry_price"] == 99.


def test_live_seed_is_preserved_when_exit_requested_until_exchange_settles(monkeypatch):
    bot = Bot(BotConfig("btcusdt", "AdvRSI", "60", 100, mode="real", leverage=5))
    bot.df = frame([100.] * 49 + [85.])
    indicators(monkeypatch, bot.df)
    bot._live_from_ts = bot.df.index[0]
    bot._exchange_position_id, bot._real_state = 42, "long"
    bot._seed_position = {"direction": "Long", "entry_price": 100., "entry_time": str(bot.df.index[48])}
    bot._run_strategy()
    assert bot._strategy_signal() == "none"
    assert bot._seed_position is not None
    assert bot._seed_position["entry_price"] == 100.
    bot._run_strategy()
    assert bot._strategy_signal() == "none"
    assert bot._seed_position is not None


@pytest.mark.parametrize("direction,execution_side", [("long", "sell"), ("short", "buy")])
@pytest.mark.parametrize("terminal", ["Canceled", "Done"])
def test_uncertain_close_binds_execution_side_and_preserves_residual(tmp_path, direction, execution_side, terminal):
    bot, exchange, executor, intent = closing_intent(tmp_path)
    bot._real_state = direction
    exchange.active[0]["side"] = "buy" if direction == "long" else "sell"
    intent["side"] = direction  # Legacy durable intents remain recoverable.
    intent.pop("position_direction", None)
    executor.save(intent)
    exchange.orders[28] = dict(id=28, type=execution_side, side="close", srcCurrency="btc", dstCurrency="usdt",
                               amount="1", matchedAmount="0.4", status=terminal, created_at=intent["created_at"], positionId=42)
    assert asyncio.run(executor.bind_order(intent["client_order_id"], 28))
    assert bot._exchange_position_id == 42
    assert bot._real_state == direction
    assert not bot.unresolved_intents()
    assert not exchange.sent[1:]


@pytest.mark.parametrize("association", [{"positionId": 99}, {"side": "open"}])
def test_close_binding_rejects_conflicting_kind_or_position(tmp_path, association):
    bot, exchange, executor, intent = closing_intent(tmp_path)
    exchange.orders[28] = dict(id=28, type="sell", side="close", srcCurrency="btc", dstCurrency="usdt",
                               amount="1", matchedAmount="0.4", status="Canceled", created_at=intent["created_at"], positionId=42)
    exchange.orders[28].update(association)
    with pytest.raises(ValueError, match="match"):
        asyncio.run(executor.bind_order(intent["client_order_id"], 28))
    assert bot.unresolved_intents()[0]["order_id"] is None


@pytest.mark.parametrize("order_id", [None, 28])
@pytest.mark.parametrize("status", ["Expired", "Liquidated"])
@pytest.mark.parametrize("settled", [True, False])
def test_system_settlement_waits_for_closed_at(tmp_path, order_id, status, settled):
    bot, exchange, executor, intent = closing_intent(tmp_path)
    exchange.active[0].update(status=status, closedAt="2026-01-02T00:00:00Z" if settled else None)
    exchange.positions = lambda visibility="active": exchange.active if visibility == "active" else []
    if order_id:
        intent["order_id"] = order_id
        exchange.orders[order_id] = dict(id=order_id, amount="1", matchedAmount="0.4", status="Canceled")
    executor.save(intent)
    asyncio.run(executor.refresh(intent))
    assert (intent["state"] == "resolved") is settled
    assert (bot._exchange_position_id is None) is settled
    archived = bot._store.get("closed_positions", f"{bot._account_key()}:42")
    assert bool(archived) is settled
    saved = bot._store.get("runtime", bot.id)
    assert (saved["_exchange_position_id"] is None) is settled


def test_expired_settlement_can_be_proved_by_matching_past_history(tmp_path):
    bot, exchange, executor, intent = closing_intent(tmp_path)
    exchange.active[0].update(status="Expired", closedAt=None)
    past = dict(exchange.active[0], pnl="8.25")
    exchange.positions = lambda visibility="active": [past] if visibility == "past" else []
    asyncio.run(executor.refresh(intent))
    assert intent["state"] == "resolved"
    assert bot._exchange_position_id is None
    assert bot._store.get("closed_positions", f"{bot._account_key()}:42")["position"]["pnl"] == "8.25"


def test_close_submission_persists_direction_separately_from_side(tmp_path, monkeypatch):
    bot, exchange, executor, old_intent = closing_intent(tmp_path)
    old_intent["state"] = "rejected"
    executor.save(old_intent)
    monkeypatch.setattr(exchange, "close_position", lambda **kwargs: (_ for _ in ()).throw(TimeoutError("lost")), raising=False)
    with pytest.raises(RuntimeError, match="uncertain"):
        asyncio.run(executor.close(42, "1", "100"))
    new_intent = bot.unresolved_intents()[0]
    assert new_intent["position_direction"] == "long"
    assert new_intent["side"] == "sell"


@pytest.mark.parametrize("strategy_cls", [AdvRSICombinedStrategy, TrendFilteredMomentumStrategy])
def test_seed_incremental_checkpoint_prevents_replaying_previously_evaluated_bar(monkeypatch, strategy_cls):
    df = frame([100., 100., 95.])
    seed = {"direction": "Long", "entry_price": 100., "entry_time": str(df.index[0])}
    strategy = strategy_cls(Config(leverage=5, max_hold_days=30))
    strategy.run(df, indicators(monkeypatch, df), seed_positions=[seed])
    assert seed["last_evaluated_at"] == str(df.index[-1])
    changed = frame([70., 70., 95.])
    recovered = strategy_cls(Config(leverage=5, max_hold_days=30))
    recovered.run(changed, indicators(monkeypatch, changed), seed_positions=[seed])
    assert len(recovered.open_positions) == 1
    assert recovered.open_positions[0]["entry_price"] == 100.


def test_later_actual_fill_replaces_old_signal_with_actual_price(monkeypatch, tmp_path):
    bot, exchange = make_bot(tmp_path), Exchange()
    executor = OrderExecutor(bot, exchange)
    bot.df = frame([70.] * 45 + [100., 100., 110., 110., 95.])
    indicators(monkeypatch, bot.df, [40.] * 45 + [10., 10., 30., 40., 40.])
    bot.cfg.custom_params = {"entry_wait_candles": 0}
    bot.cfg.leverage = 5
    bot._live_from_ts = bot.df.index[0]
    exchange.active = []
    opened = executor._new("open", "btc", "usdt", "buy", {"amount": "1", "price": "110"}, [])
    asyncio.run(executor.submit(opened))
    exchange.active[0].update(openedAt=str(bot.df.index[48] + pd.Timedelta(seconds=5)), entryPrice="100")
    asyncio.run(executor.refresh(opened))
    bot._run_strategy()
    assert bot._strategy_signal() == "long"
    assert bot._seed_position["entry_price"] == 100.
    assert len(bot.open_positions) == 1
    assert bot.open_positions[0]["entry_price"] == 100.
    assert bot.closed_trade_count == 0


@pytest.mark.parametrize("order_id", [None, 28])
def test_partial_then_expired_unknown_close_never_submits_duplicate(tmp_path, monkeypatch, order_id):
    bot, exchange, executor, intent = closing_intent(tmp_path)
    exchange.active[0].update(status="Expired", closedAt=None)
    exchange.positions = lambda visibility="active": exchange.active if visibility == "active" else []
    if order_id:
        intent["order_id"] = order_id
        exchange.orders[order_id] = dict(id=order_id, amount="1", matchedAmount="0.4", status="Canceled")
    executor.save(intent)
    close_calls = []
    monkeypatch.setattr(exchange, "close_position", lambda **kwargs: close_calls.append(kwargs), raising=False)
    assert not asyncio.run(executor.close(42, "0.6", "100", timeout=0))
    assert close_calls == []
    assert len(bot.unresolved_intents()) == 1


def test_owned_expired_position_without_intent_waits_for_system_settlement(tmp_path, monkeypatch):
    bot, exchange, executor, intent = closing_intent(tmp_path)
    intent["state"] = "rejected"
    executor.save(intent)
    exchange.active[0].update(status="Expired", closedAt=None)
    exchange.positions = lambda visibility="active": exchange.active if visibility == "active" else []
    bot._exchange_client = exchange
    asyncio.run(bot._sync_exchange())
    assert bot._exchange_position_id == 42
    assert bot._seed_position is not None
    assert "settlement" in bot.error
    exchange.active[0]["closedAt"] = "2026-01-03T00:00:00Z"
    asyncio.run(bot._sync_exchange())
    assert bot._exchange_position_id is None
    assert bot._store.get("closed_positions", f"{bot._account_key()}:42")["position"]["status"] == "Expired"


@pytest.mark.parametrize("start", ["2026-02-01", "2024-02-01", "2026-04-01", "2026-01-01"])
@pytest.mark.parametrize("source", ["binance", "nobitex"])
def test_monthly_append_and_refresh_obey_provider_calendar(monkeypatch, start, source):
    zone = "UTC" if source == "binance" else "Asia/Tehran"
    first = pd.Timestamp(start, tz=zone).tz_convert("UTC").tz_localize(None)
    following = (pd.Timestamp(start, tz=zone) + pd.offsets.MonthBegin()).tz_convert("UTC").tz_localize(None)
    bot = Bot(BotConfig("btcusdt", "AdvRSI", "M", 100))
    bot.df = frame([100.], pd.DatetimeIndex([first]))
    bot.df.attrs["source"] = source
    fresh = frame([100.], pd.DatetimeIndex([following]))
    fresh.attrs["source"] = source
    assert bot._validate_and_append(fresh)
    monkeypatch.setattr("data.DataFetcher.fetch", lambda self: fresh)
    asyncio.run(bot._refresh_data())
    assert list(bot.df.index) == [first, following]


def test_monthly_live_boundary_and_wake_use_calendar(monkeypatch):
    bot = Bot(BotConfig("btcusdt", "AdvRSI", "M", 100, mode="real"))
    last = pd.Timestamp("2026-02-01", tz="Asia/Tehran").tz_convert("UTC").tz_localize(None)
    bot.df = frame([100.], pd.DatetimeIndex([last]))
    bot.df.attrs["source"] = "nobitex"
    async def nothing():
        pass
    monkeypatch.setattr(bot, "_refresh_data", nothing)
    monkeypatch.setattr(bot, "_sync_exchange", nothing)
    monkeypatch.setattr(bot, "_run_strategy", lambda: None)
    monkeypatch.setattr("bot.time.time", lambda: last.timestamp())
    waits = []
    async def wait(awaitable, timeout):
        awaitable.close()
        waits.append(timeout)
    monkeypatch.setattr("bot.asyncio.wait_for", wait)
    asyncio.run(bot._run_loop())
    march = pd.Timestamp("2026-03-01", tz="Asia/Tehran").tz_convert("UTC").tz_localize(None)
    april = pd.Timestamp("2026-04-01", tz="Asia/Tehran").tz_convert("UTC").tz_localize(None)
    assert bot._live_from_ts == march
    assert waits == [april.timestamp() - last.timestamp() + 5]


def test_delegation_lookup_runs_outside_trading_event_loop(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    bot.df = frame([100.])
    loop_thread = threading.get_ident()
    observed = []
    monkeypatch.setattr(bot, "_fetch_delegation_limit", lambda *args: observed.append(threading.get_ident()))
    async def opened(*args, **kwargs):
        return []
    monkeypatch.setattr(OrderExecutor, "open", opened)
    asyncio.run(bot._place_single_order(SimpleNamespace(live_price=lambda symbol: 100.), "btc", "usdt", "buy", 1))
    assert observed and observed[0] != loop_thread


def test_audit_survives_ui_limit_restart_and_redacts_credentials(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("bot._notify", lambda *args: None)
    bot = make_bot(tmp_path)
    with caplog.at_level("INFO", logger="trading.audit"):
        for i in range(205):
            bot._log_event("warning", f"Order {i}; Authorization: Token secret-value; token=abc123; password=hunter2")
    assert len(bot.activity_log) == 200
    records = Store(tmp_path / "db.sqlite3").list("audit_events")
    assert len(records) == 205
    assert all(record["bot_id"] == bot.id for record in records.values())
    combined = json.dumps(records) + json.dumps(bot.activity_log) + caplog.text
    assert not any(secret in combined for secret in ("secret-value", "abc123", "hunter2"))
    assert any(getattr(record, "event", None) == "warning" for record in caplog.records)
