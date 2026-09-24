import asyncio
import pandas as pd
import pytest

from bot import Bot, BotConfig
from storage import Store


class Exchange:
    def __init__(self):
        self.sent = []
        self.active = []
        self.orders = {}
        self.lose_response = False

    def positions(self, status="active"):
        return list(self.active)

    def open_margin_order(self, **payload):
        self.sent.append(payload)
        self.orders[12] = {"id": 12, "status": "Done", "matchedAmount": "1", "amount": "1", "fee": "0.01", "averagePrice": "100"}
        self.active = [{"id": 42, "srcCurrency": "btc", "dstCurrency": "usdt", "side": "buy", "status": "Open", "entryPrice": "100", "openedAt": "2026-01-01T00:00:00Z"}]
        if self.lose_response:
            raise TimeoutError("response lost")
        return self.orders[12]

    def order_status(self, order_id):
        return self.orders[order_id]

    def order_by_client_id(self, client_id):
        raise ValueError("Only active orders searchable; completed order absent")

    def position(self, position_id):
        return next(p for p in self.active if p["id"] == position_id)


def make_bot(tmp_path, name="one"):
    bot = Bot(BotConfig("btcusdt", "AdvRSI", "60", 100, mode="real", id=name))
    bot._store = Store(tmp_path / "db.sqlite3")
    return bot


def test_lost_submission_response_blocks_resubmission_after_restart(tmp_path):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    exchange.lose_response = True
    executor = OrderExecutor(bot, exchange)
    with pytest.raises(RuntimeError, match="uncertain"):
        asyncio.run(executor.open("btc", "usdt", "buy", "1", "100", 1))
    restored = make_bot(tmp_path)
    restored.restore(bot.snapshot())
    with pytest.raises(RuntimeError, match="Unresolved"):
        asyncio.run(OrderExecutor(restored, exchange).open("btc", "usdt", "buy", "1", "100", 1))
    assert len(exchange.sent) == 1
    assert len(restored.unresolved_intents()) == 1


def test_filled_order_claims_position_and_persists_actual_fee(tmp_path):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    asyncio.run(OrderExecutor(bot, exchange).open("btc", "usdt", "buy", "1", "100", 1))
    assert bot._exchange_position_id == 42
    assert bot._store.owner(bot._account_key(), 42) == bot.id
    assert bot.unresolved_intents() == []
    fills = list(bot._store.list("orders").values())
    assert fills[0]["exchange_order"]["fee"] == "0.01"
    assert fills[0]["client_order_id"] == exchange.sent[0]["client_order_id"]


def test_existing_manual_position_is_not_adopted(tmp_path):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    exchange.active = [{"id": 77, "srcCurrency": "btc", "dstCurrency": "usdt", "side": "buy", "status": "Open"}]
    with pytest.raises(RuntimeError, match="existing"):
        asyncio.run(OrderExecutor(bot, exchange).open("btc", "usdt", "buy", "1", "100", 1))
    assert not exchange.sent


def test_pending_order_other_bot_reserves_market(tmp_path):
    from exchange_execution import OrderExecutor
    one, exchange = make_bot(tmp_path), Exchange()
    exchange.lose_response = True
    with pytest.raises(RuntimeError):
        asyncio.run(OrderExecutor(one, exchange).open("btc", "usdt", "buy", "1", "100", 1))
    exchange.active = []
    other = make_bot(tmp_path, "two")
    with pytest.raises(RuntimeError, match="reserved"):
        asyncio.run(OrderExecutor(other, exchange).open("btc", "usdt", "buy", "1", "100", 1))
    assert len(exchange.sent) == 1


def test_simulation_snapshot_replay_preserves_result(tmp_path):
    from tests.fakes import synthetic_candles
    bot = Bot(BotConfig("btcusdt", "AdvRSI", "60", 100))
    bot._store = Store(tmp_path / "db.sqlite3")
    bot.df = synthetic_candles(700)
    bot._live_from_ts = bot.df.index[300]
    bot._run_strategy()
    snapshot = bot.snapshot()
    restored = Bot(bot.cfg)
    restored._store = bot._store
    restored.restore(snapshot)
    restored._run_strategy()
    assert restored.balance == bot.balance
    assert restored.completed_trades == bot.completed_trades
    assert restored.closed_trade_count == bot.closed_trade_count


def test_resolution_and_runtime_are_atomic_on_snapshot_failure(tmp_path, monkeypatch):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    executor = OrderExecutor(bot, exchange)
    intent = executor._new("open", "btc", "usdt", "buy", {"amount": "1", "price": "100"}, [])
    intent["state"] = "resolved"
    def crash():
        raise RuntimeError("snapshot failure")
    monkeypatch.setattr(bot, "snapshot", crash)
    with pytest.raises(RuntimeError, match="snapshot failure"):
        executor.save(intent)
    assert bot._store.get("orders", intent["client_order_id"])["state"] == "prepared"


def test_submit_cannot_replay_an_accepted_intent(tmp_path):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    executor = OrderExecutor(bot, exchange)
    intent = executor._new("open", "btc", "usdt", "buy", {"amount": "1", "price": "100"}, [])
    asyncio.run(executor.submit(intent))
    # Even a stale in-memory copy must not bypass the durable guard.
    intent["state"] = "prepared"
    with pytest.raises(RuntimeError, match="already"):
        asyncio.run(executor.submit(intent))
    assert len(exchange.sent) == 1


def closing_intent(tmp_path):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    executor = OrderExecutor(bot, exchange)
    asyncio.run(executor.open("btc", "usdt", "buy", "1", "100", 1))
    intent = executor._new("close", "btc", "usdt", "long", {"position_id": 42, "amount": "1", "price": "100"}, [42])
    intent["state"] = "uncertain"
    executor.save(intent)
    return bot, exchange, executor, intent


def test_lost_close_response_archives_actual_position_and_clears_seed(tmp_path):
    bot, exchange, executor, intent = closing_intent(tmp_path)
    exchange.active[0].update(status="Closed", pnl="4.25", fee="0.05")
    asyncio.run(executor.refresh(intent))
    assert bot._exchange_position_id is None
    assert bot._seed_position is None
    assert bot._pending_order is None
    assert bot._real_positions == []
    assert bot._store.get("closed_positions", f"{bot._account_key()}:42")["position"]["pnl"] == "4.25"


def test_wrong_position_response_cannot_resolve_close(tmp_path, monkeypatch):
    bot, exchange, executor, intent = closing_intent(tmp_path)
    monkeypatch.setattr(exchange, "position", lambda position_id: {"id": 99, "status": "Closed"})
    with pytest.raises(RuntimeError, match="position"):
        asyncio.run(executor.refresh(intent))
    assert bot._exchange_position_id == 42
    assert len(bot.unresolved_intents()) == 1


def test_prepared_before_send_crash_reconciles_without_network_submission(tmp_path):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    executor = OrderExecutor(bot, exchange)
    executor._new("open", "btc", "usdt", "buy", {"amount": "1", "price": "100"}, [])
    assert asyncio.run(OrderExecutor(make_bot(tmp_path), exchange).reconcile())
    assert not exchange.sent


def test_acknowledged_before_fill_crash_recovers_position(tmp_path):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    executor = OrderExecutor(bot, exchange)
    intent = executor._new("open", "btc", "usdt", "buy", {"amount": "1", "price": "100"}, [])
    asyncio.run(executor.submit(intent))
    restored = make_bot(tmp_path)
    restored.restore(bot._store.get("runtime", bot.id))
    assert asyncio.run(OrderExecutor(restored, exchange).reconcile())
    assert restored._exchange_position_id == 42
    assert len(exchange.sent) == 1


def test_partial_fill_is_visible_when_cancel_times_out(tmp_path, monkeypatch):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    original = exchange.open_margin_order
    def partial(**payload):
        order = original(**payload)
        order.update(status="Active", matchedAmount="0.75")
        return order
    def failed_cancel(order_id):
        raise TimeoutError("cancel response lost")
    monkeypatch.setattr(exchange, "open_margin_order", partial)
    monkeypatch.setattr(exchange, "cancel_order", failed_cancel, raising=False)
    with pytest.raises(TimeoutError):
        asyncio.run(OrderExecutor(bot, exchange).open("btc", "usdt", "buy", "1", "100", 1, timeout=0))
    restored = make_bot(tmp_path)
    restored.restore(bot._store.get("runtime", bot.id))
    assert restored._exchange_position_id == 42
    assert restored._pending_order["matched_pct"] == 75
    assert len(restored._real_positions) == 1
    assert len(restored.unresolved_intents()) == 1


def test_cancel_partial_remainder_preserves_position(tmp_path, monkeypatch):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    original = exchange.open_margin_order
    def partial(**payload):
        order = original(**payload)
        order.update(status="Active", matchedAmount="0.75")
        return order
    canceled = []
    def cancel(order_id):
        canceled.append(order_id)
        exchange.orders[order_id]["status"] = "Canceled"
    monkeypatch.setattr(exchange, "open_margin_order", partial)
    monkeypatch.setattr(exchange, "cancel_order", cancel, raising=False)
    asyncio.run(OrderExecutor(bot, exchange).open("btc", "usdt", "buy", "1", "100", 1, timeout=0))
    assert canceled == [12]
    assert bot._exchange_position_id == 42
    assert not bot.unresolved_intents()
    assert bot._pending_order is None


def test_fill_evidence_survives_position_lookup_failure(tmp_path, monkeypatch):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    executor = OrderExecutor(bot, exchange)
    intent = executor._new("open", "btc", "usdt", "buy", {"amount": "1", "price": "100"}, [])
    asyncio.run(executor.submit(intent))
    exchange.orders[12] = dict(exchange.orders[12], fee="0.02", matchedAmount="0.5", status="Active")
    def failed_lookup(*args):
        raise TimeoutError("positions unavailable")
    monkeypatch.setattr(exchange, "positions", failed_lookup)
    with pytest.raises(TimeoutError):
        asyncio.run(executor.refresh(intent))
    assert bot._store.get("orders", intent["client_order_id"])["exchange_order"]["fee"] == "0.02"


def test_lost_response_active_lookup_rebuilds_pending_progress(tmp_path, monkeypatch):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    exchange.lose_response = True
    with pytest.raises(RuntimeError, match="uncertain"):
        asyncio.run(OrderExecutor(bot, exchange).open("btc", "usdt", "buy", "1", "100", 1))
    exchange.orders[12].update(status="Active", matchedAmount="0.25")
    monkeypatch.setattr(exchange, "order_by_client_id", lambda client_id: exchange.orders[12])
    restored = make_bot(tmp_path)
    restored.restore(bot._store.get("runtime", bot.id))
    assert not asyncio.run(OrderExecutor(restored, exchange).reconcile())
    assert restored._pending_order["order_id"] == 12
    assert restored._pending_order["matched_pct"] == 25


def test_wrong_side_fill_remains_unresolved(tmp_path):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    executor = OrderExecutor(bot, exchange)
    intent = executor._new("open", "btc", "usdt", "buy", {"amount": "1", "price": "100"}, [])
    asyncio.run(executor.submit(intent))
    exchange.active[0]["side"] = "sell"
    assert not asyncio.run(executor.reconcile())
    assert bot._exchange_position_id is None
    assert bot._store.owner(bot._account_key(), 42) is None


def test_existing_position_ownership_cannot_be_taken_by_fill(tmp_path):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    executor = OrderExecutor(bot, exchange)
    intent = executor._new("open", "btc", "usdt", "buy", {"amount": "1", "price": "100"}, [])
    asyncio.run(executor.submit(intent))
    bot._store.claim_position(bot._account_key(), 42, "another-bot")
    with pytest.raises(ValueError, match="owned"):
        asyncio.run(executor.reconcile())
    assert bot._exchange_position_id is None
    assert bot._store.owner(bot._account_key(), 42) == "another-bot"


def test_close_timeout_stays_reserved_and_never_replays(tmp_path, monkeypatch):
    bot, exchange, executor, prepared = closing_intent(tmp_path)
    # Complete the fixture's unsent intent before exercising the public close.
    prepared["state"] = "rejected"
    executor.save(prepared)
    sent = []
    def lost_close(**payload):
        sent.append(payload)
        raise TimeoutError("close response lost")
    monkeypatch.setattr(exchange, "close_position", lost_close, raising=False)
    with pytest.raises(RuntimeError, match="uncertain"):
        asyncio.run(executor.close(42, "1", "100", timeout=0))
    assert not asyncio.run(executor.close(42, "1", "100", timeout=0))
    assert len(sent) == 1
    assert bot._exchange_position_id == 42
    assert len(bot.unresolved_intents()) == 1


def test_other_account_cannot_reconcile_intent(tmp_path):
    bot, exchange, executor, intent = closing_intent(tmp_path)
    intent["account"] = "testnet:another"
    with pytest.raises(RuntimeError, match="another"):
        asyncio.run(executor.refresh(intent))
    assert bot._exchange_position_id == 42


def test_manual_binding_validates_order_then_recovers(tmp_path):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    exchange.lose_response = True
    executor = OrderExecutor(bot, exchange)
    with pytest.raises(RuntimeError):
        asyncio.run(executor.open("btc", "usdt", "buy", "1", "100", 1))
    intent = bot.unresolved_intents()[0]
    exchange.orders[12].update(srcCurrency="btc", dstCurrency="usdt", type="buy", created_at=intent["created_at"])
    assert asyncio.run(executor.bind_order(intent["client_order_id"], 12))
    assert bot._exchange_position_id == 42
    assert len(exchange.sent) == 1


def test_manual_binding_rejects_unrelated_order(tmp_path):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    exchange.lose_response = True
    executor = OrderExecutor(bot, exchange)
    with pytest.raises(RuntimeError):
        asyncio.run(executor.open("btc", "usdt", "buy", "1", "100", 1))
    intent = bot.unresolved_intents()[0]
    exchange.orders[12].update(srcCurrency="eth", dstCurrency="usdt", type="buy", created_at=intent["created_at"])
    with pytest.raises(ValueError, match="match"):
        asyncio.run(executor.bind_order(intent["client_order_id"], 12))
    assert bot.unresolved_intents()[0]["order_id"] is None


def test_filled_then_closed_position_recovers_from_explicit_order_position_id(tmp_path, monkeypatch):
    from exchange_execution import OrderExecutor
    bot, exchange = make_bot(tmp_path), Exchange()
    executor = OrderExecutor(bot, exchange)
    intent = executor._new("open", "btc", "usdt", "buy", {"amount": "1", "price": "100"}, [])
    asyncio.run(executor.submit(intent))
    closed = dict(exchange.active[0], status="Closed", pnl="3.50")
    exchange.active = []
    exchange.orders[12]["positionId"] = 42
    monkeypatch.setattr(exchange, "position", lambda position_id: closed)
    assert asyncio.run(executor.reconcile())
    assert bot._exchange_position_id is None
    assert bot._store.get("closed_positions", f"{bot._account_key()}:42")["position"]["pnl"] == "3.50"
