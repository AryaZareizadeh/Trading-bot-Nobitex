import asyncio
from dataclasses import asdict

import pandas as pd
import pytest

from bot import Bot, BotConfig, BotStatus
from bot_manager import BotManager
from storage import Store
from strategy import AdvRsiLongParams, AdvRsiShortParams


def test_combined_bot_api_keeps_both_credits_on_create_edit_duplicate_and_restart(monkeypatch, tmp_path):
    import server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "current_max_hold_days", lambda: 60)
    manager = BotManager(Store(tmp_path / "bots.sqlite3"), tmp_path)
    params = {"long": asdict(AdvRsiLongParams(rsi_length=14)),
              "short": asdict(AdvRsiShortParams(rsi_length=38))}
    with TestClient(server.create_app(manager=manager, offline=True, security=False)) as client:
        response = client.post("/api/bots", json={
            "symbol": "btcusdt", "timeframe": "60", "strategy_name": "AdvRSIIndependent",
            "credit": 999, "long_credit": 120, "short_credit": 300,
            "custom_params": params, "leverage": 2,
        })
        assert response.status_code == 201, response.text
        state = response.json()
        bot_id = state["id"]
        assert state["credit"] == 420
        assert state["sides"]["long"]["credit"] == 120
        assert state["sides"]["short"]["credit"] == 300
        response = client.patch(f"/api/bots/{bot_id}", json={"short_credit": 700})
        assert response.status_code == 200, response.text
        assert response.json()["credit"] == 820
        assert response.json()["long_credit"] == 120
        response = client.post(f"/api/bots/{bot_id}/duplicate")
        assert response.status_code == 201, response.text
        assert response.json()["long_credit"] == 120
        assert response.json()["short_credit"] == 700
        assert len(manager.get_all()) == 2
    restored = BotManager(manager.store, tmp_path)
    restored.load()
    bot = restored.get_bot(bot_id)
    assert bot.sides["long"].cfg.custom_params["rsi_length"] == 14
    assert bot.sides["short"].cfg.custom_params["rsi_length"] == 38
    assert bot.sides["long"].id != bot.sides["short"].id


def test_real_directional_bot_evaluates_its_strategy(monkeypatch):
    # A misplaced simulation return would leave both real runners without signals.
    from types import SimpleNamespace
    from indicators import Indicators

    index = pd.date_range("2025-01-01", periods=60, freq="h")
    frame = pd.DataFrame({"Open": 100., "High": 101., "Low": 99., "Close": 100., "Volume": 1.}, index=index)
    monkeypatch.setattr(Indicators, "stoch_rsi", lambda *_: SimpleNamespace(
        k=pd.Series([10.] * 59 + [40.], index=index), d=pd.Series([10.] * 59 + [40.], index=index)))
    monkeypatch.setattr(Indicators, "rsi_with_ma", lambda *_: (
        pd.Series(40., index=index), pd.Series(30., index=index)))
    bot = Bot(BotConfig("btcusdt", "AdvRSILong", "60", 100, leverage=2, mode="real",
                        custom_params={"entry_wait_candles": 0}))
    bot.df = frame
    bot._run_strategy()
    assert len(bot.open_positions) == 1
    assert bot.open_positions[0]["direction"] == "Long"


def dual_fixture(tmp_path, mode="simulation"):
    manager = BotManager(Store(tmp_path / "dual.sqlite3"), tmp_path)
    bot = manager.create_bot(BotConfig(
        "btcusdt", "AdvRSIIndependent", "60", 999, leverage=2, mode=mode,
        long_credit=120, short_credit=300,
        custom_params={"long": {"rsi_length": 14, "rsi_fast_length": 14, "entry_wait_candles": 0},
                       "short": {"rsi_length": 38, "rsi_fast_length": 20, "entry_wait_candles": 0}},
        execution={"fill_timing": "legacy_close", "entry_fee_rate": .001},
    ))
    return manager, bot


def dual_signals(monkeypatch):
    from types import SimpleNamespace
    from indicators import Indicators

    def stoch(provider, rsi_length, *_):
        n = len(provider._index)
        values = ([10.] * 59 + [40.] + [10.] * (n - 60) if rsi_length == 14
                  else [90.] * 59 + [60.] * (n - 59))
        k = pd.Series(values, index=provider._index)
        return SimpleNamespace(k=k, d=k)
    monkeypatch.setattr(Indicators, "stoch_rsi", stoch)
    monkeypatch.setattr(Indicators, "rsi_with_ma", lambda provider, fast, *_: (
        pd.Series(40. if fast == 14 else 60., index=provider._index),
        pd.Series(30. if fast == 14 else 70., index=provider._index)))
    index = pd.date_range("2025-01-01", periods=61, freq="h")
    return pd.DataFrame({"Open": 100., "High": 101., "Low": 99., "Close": 100., "Volume": 1.}, index=index)


def test_both_positions_open_and_long_exit_keeps_short_account_untouched(monkeypatch, tmp_path):
    manager, bot = dual_fixture(tmp_path)
    frame = dual_signals(monkeypatch)
    bot.df = frame.iloc[:60]
    bot._live_from_ts = frame.index[0]
    bot._run_strategy()
    state = bot.to_state_dict()
    assert {p["direction"] for p in state["open_positions"]} == {"Long", "Short"}
    long_pos, short_pos = bot.sides["long"].open_positions[0], bot.sides["short"].open_positions[0]
    assert long_pos["margin"] == pytest.approx(120 / 1.002)
    assert short_pos["margin"] == pytest.approx(300 / 1.002)
    short_before = bot.sides["short"].balance
    bot.df = frame
    bot._run_strategy()
    assert [p["direction"] for p in bot.open_positions] == ["Short"]
    assert bot.sides["short"].balance == short_before
    assert bot.closed_trade_count == 1
    assert bot.sides["short"].closed_trade_count == 0
    recovered = BotManager(manager.store, tmp_path)
    recovered.load()
    restored = recovered.get_bot(bot.id)
    assert [p["direction"] for p in restored.open_positions] == ["Short"]
    assert restored.to_state_dict()["total_pnl"] == bot.to_state_dict()["total_pnl"]
    assert restored.get_chart_data("long")["chart_data"]["strategy"] == "AdvRSILong"
    assert restored.get_chart_data("short")["chart_data"]["strategy"] == "AdvRSIShort"


class HedgedExchange:
    def __init__(self):
        self.active, self.orders, self.sent = [], {}, []

    def positions(self, status="active"):
        return list(self.active)

    def open_margin_order(self, **payload):
        self.sent.append(payload)
        order_id, pos_id = len(self.sent), 40 + len(self.sent)
        self.orders[order_id] = {"id": order_id, "status": "Done", "matchedAmount": "1", "amount": "1",
                                 "fee": "0.01", "averagePrice": "100"}
        self.active.append({"id": pos_id, "srcCurrency": "btc", "dstCurrency": "usdt", "side": payload["side"],
                            "status": "Open", "entryPrice": "100", "openedAt": "2026-01-01T00:00:00Z"})
        return self.orders[order_id]

    def order_status(self, order_id):
        return self.orders[order_id]

    def position(self, position_id):
        return next(p for p in self.active if p["id"] == position_id)

    def close_position(self, position_id, **_):
        pos = self.position(position_id)
        pos["status"] = "Closed"
        order_id = 100 + position_id
        self.orders[order_id] = {"id": order_id, "status": "Done", "matchedAmount": "1", "amount": "1"}
        return self.orders[order_id]


def test_real_both_positions_survive_restart_and_closing_one_keeps_the_other(tmp_path):
    from exchange_execution import OrderExecutor

    manager, bot = dual_fixture(tmp_path, "real")
    exchange = HedgedExchange()
    asyncio.run(OrderExecutor(bot.sides["long"], exchange).open("btc", "usdt", "buy", "1", "100", 2))
    asyncio.run(OrderExecutor(bot.sides["short"], exchange).open("btc", "usdt", "sell", "1", "100", 2))
    assert {p["direction"] for p in bot.to_state_dict()["open_positions"]} == {"Long", "Short"}
    assert set(manager.store.list("runtime")) == {bot.id}
    recovered = BotManager(manager.store, tmp_path)
    recovered.load()
    restored = recovered.get_bot(bot.id)
    assert restored.sides["long"]._exchange_position_id == 41
    assert restored.sides["short"]._exchange_position_id == 42
    assert manager.store.owner(restored.sides["long"]._account_key(), 41) == restored.sides["long"].id
    assert manager.store.owner(restored.sides["short"]._account_key(), 42) == restored.sides["short"].id
    assert asyncio.run(OrderExecutor(restored.sides["long"], exchange).close(41, "1", "100"))
    assert restored.sides["long"]._exchange_position_id is None
    assert restored.sides["short"]._exchange_position_id == 42
    assert exchange.position(42)["status"] == "Open"
    assert restored.has_owned_position()
    with pytest.raises(ValueError, match="owned position"):
        recovered.remove_bot(restored.id)
    with pytest.raises(ValueError, match="owned position"):
        recovered.update_bot(restored, restored.cfg)


@pytest.mark.parametrize("other_side", ["buy", "sell"])
def test_combined_live_entry_rejects_positions_outside_its_own_other_runner(tmp_path, other_side):
    from exchange_execution import OrderExecutor

    manager, bot = dual_fixture(tmp_path, "real")
    exchange = HedgedExchange()
    exchange.active = [{"id": 77, "srcCurrency": "btc", "dstCurrency": "usdt", "side": other_side, "status": "Open"}]
    manager.store.claim_position(bot.sides["long"]._account_key(), 77, "another-bot")
    with pytest.raises(RuntimeError, match="existing position"):
        asyncio.run(OrderExecutor(bot.sides["long"], exchange).open("btc", "usdt", "buy", "1", "100", 2))
    assert exchange.sent == []


def test_combined_metrics_align_times_and_do_not_average_side_drawdowns(tmp_path):
    _, bot = dual_fixture(tmp_path)
    bot.sides["long"].equity_curve = [{"time": 10, "equity": 120}, {"time": 20, "equity": 60}]
    bot.sides["short"].equity_curve = [{"time": 20, "equity": 360}]
    # Short is idle at t=10. Its gain at t=20 offsets the long loss completely.
    state = bot.to_state_dict()
    assert state["balance_history"] == [420, 420]
    assert state["max_drawdown"] == 0


def test_start_and_stop_control_both_runners_and_restored_bot_stays_idle(monkeypatch, tmp_path):
    from data import DataFetcher
    import position_limits

    manager, bot = dual_fixture(tmp_path)
    now = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("h")
    frame = pd.DataFrame({"Open": 100., "High": 101., "Low": 99., "Close": 100., "Volume": 1.},
                         index=pd.date_range(end=now - pd.Timedelta(hours=1), periods=600, freq="h"))
    monkeypatch.setattr(DataFetcher, "fetch", lambda *_: frame.copy())
    monkeypatch.setattr(position_limits, "current_max_hold_days", lambda: 60)
    async def run():
        await bot.start()
        for _ in range(100):
            if all(side.df is not None for side in bot.execution_bots()):
                break
            await asyncio.sleep(.01)
        assert bot.status == BotStatus.RUNNING
        assert all(side.status == BotStatus.RUNNING for side in bot.execution_bots())
        await bot.stop()
        assert all(side.status == BotStatus.STOPPED and side._task.done() for side in bot.execution_bots())
    asyncio.run(run())
    recovered = BotManager(manager.store, tmp_path)
    recovered.load()
    assert recovered.get_bot(bot.id).status == BotStatus.IDLE


def test_optimizer_results_create_one_combined_bot_and_require_both_eligible_sides(monkeypatch, tmp_path):
    import server
    from optimizer import IndependentOptimizerJob
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "current_max_hold_days", lambda: 60)
    manager = BotManager(Store(tmp_path / "optimizer.sqlite3"), tmp_path)
    app = server.create_app(manager=manager, offline=True, security=False)
    with TestClient(app) as client:
        job = IndependentOptimizerJob("btcusdt", "60", 500, 120, 300, 2,
                                      execution={"entry_fee_rate": .001})
        job.status = "done"
        for side, cls in (("long", AdvRsiLongParams), ("short", AdvRsiShortParams)):
            child = job.sides[side]
            child.status = "done"
            child.best_params = asdict(cls(rsi_length=14 if side == "long" else 38))
            child.best_pnl = 0
        app.state.optimizer_manager._jobs[job.id] = job
        response = client.post(f"/api/optimizer/run/{job.id}/bots", json={"combined": True, "sides": ["short", "long"]})
        assert response.status_code == 201, response.text
        created = response.json()["bots"]
        assert len(created) == len(manager.get_all()) == 1
        assert created[0]["strategy"] == "AdvRSIIndependent"
        bot = manager.get_bot(created[0]["id"])
        assert bot.cfg.long_credit == 120 and bot.cfg.short_credit == 300
        assert bot.sides["long"].cfg.custom_params["rsi_length"] == 14
        assert bot.sides["short"].cfg.custom_params["rsi_length"] == 38
        assert bot.sides["long"].cfg.execution["entry_fee_rate"] == .001
        assert bot.status == BotStatus.IDLE
        assert client.post(f"/api/optimizer/run/{job.id}/bots", json={"combined": True, "sides": ["long"]}).status_code == 400
        job.sides["short"].best_params = None
        assert client.post(f"/api/optimizer/run/{job.id}/bots", json={"combined": True}).status_code == 409
        assert len(manager.get_all()) == 1


def test_regular_bot_cannot_bypass_a_combined_bots_durable_market_ownership(tmp_path):
    from exchange_execution import OrderExecutor

    manager, bot = dual_fixture(tmp_path, "real")
    exchange = HedgedExchange()
    asyncio.run(OrderExecutor(bot.sides["long"], exchange).open("btc", "usdt", "buy", "1", "100", 2))
    exchange.active = []  # A transient omission in the exchange response must not bypass ownership.
    other = manager.create_bot(BotConfig("btcusdt", "AdvRSI", "60", 100, mode="real"))
    with pytest.raises(RuntimeError, match="already owned"):
        asyncio.run(OrderExecutor(other, exchange).open("btc", "usdt", "sell", "1", "100", 2))
    assert len(exchange.sent) == 1


def test_combined_backtest_uses_one_candle_window_and_separate_capital(monkeypatch, tmp_path):
    from data import DataFetcher
    from indicators import Indicators
    import position_limits
    from types import SimpleNamespace

    _, bot = dual_fixture(tmp_path)
    frame = pd.DataFrame({"Open": 100., "High": 101., "Low": 99., "Close": 100., "Volume": 1.},
                         index=pd.date_range("2025-01-01", periods=900, freq="h"))
    calls = []
    monkeypatch.setattr(DataFetcher, "fetch", lambda *_: calls.append(True) or frame)
    monkeypatch.setattr(position_limits, "current_max_hold_days", lambda: 60)
    def stoch(provider, length, *_):
        values = ([10.] * 898 + [40.] * 2) if length == 14 else ([90.] * 898 + [60.] * 2)
        k = pd.Series(values, index=frame.index)
        return SimpleNamespace(k=k, d=k)
    monkeypatch.setattr(Indicators, "stoch_rsi", stoch)
    monkeypatch.setattr(Indicators, "rsi_with_ma", lambda provider, fast, *_: (
        pd.Series(40. if fast == 14 else 60., index=frame.index),
        pd.Series(30. if fast == 14 else 70., index=frame.index)))
    result = asyncio.run(bot.run_backtest())
    assert len(calls) == 1
    assert result["candle_count"] == 500
    assert result["sides"]["long"]["open_count"] == result["sides"]["short"]["open_count"] == 1
    assert result["sides"]["long"]["reserved_margin"] == pytest.approx(120 / 1.002)
    assert result["sides"]["short"]["reserved_margin"] == pytest.approx(300 / 1.002)
    assert result["total_pnl"] == pytest.approx(-420 * .002 / 1.002)


def test_stopped_combined_bot_can_adopt_both_directions_and_blocks_reconfiguration(monkeypatch, tmp_path):
    import server
    from fastapi.testclient import TestClient

    manager, bot = dual_fixture(tmp_path, "real")
    exchange = HedgedExchange()
    exchange.open_margin_order(side="buy")
    exchange.open_margin_order(side="sell")
    monkeypatch.setattr(server, "current_max_hold_days", lambda: 60)
    with TestClient(server.create_app(manager=manager, offline=True, security=False, exchange_client=exchange)) as client:
        for pos_id in (41, 42):
            response = client.post(f"/api/bots/{bot.id}/adopt-position", json={"position_id": pos_id})
            assert response.status_code == 200, response.text
        assert len(response.json()["open_positions"]) == 2
        assert client.patch(f"/api/bots/{bot.id}", json={"short_credit": 700}).status_code == 400
        assert manager.get_bot(bot.id).cfg.short_credit == 300

