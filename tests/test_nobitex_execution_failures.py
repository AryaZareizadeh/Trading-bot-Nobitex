"""Exchange rejection and temporary read failures must not strand live execution."""
import asyncio
from types import SimpleNamespace

import pytest

import exchange_execution
from exchange_execution import OrderExecutor
from nobitex_client import NobitexAPIError
from tests.test_recovery import Exchange, make_bot


@pytest.fixture
def poll_clock(monkeypatch):
    clock = SimpleNamespace(now=0.)
    async def sleep(seconds):
        clock.now += seconds
    monkeypatch.setattr(exchange_execution, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(exchange_execution.asyncio, "sleep", sleep)
    return clock


@pytest.mark.parametrize("code", ["BadPrice", "ParseError", "TradeLimitation", "InvalidMarketPair",
                                  "FeatureUnavailable", "PriceConditionFailed", "TooManyRequests"])
def test_explicit_rejected_open_releases_market_and_reports_reason(tmp_path, code):
    bot, client = make_bot(tmp_path), Exchange()
    def rejected(**payload):
        raise NobitexAPIError(code, "Order rejected", http_status=400 if code != "TooManyRequests" else 429)
    client.open_margin_order = rejected
    with pytest.raises(RuntimeError, match=code):
        asyncio.run(OrderExecutor(bot, client).open("btc", "usdt", "buy", "1", "100", 1))
    assert not bot.unresolved_intents()
    assert list(bot._store.list("orders").values())[0]["state"] == "rejected"
    # A genuine rejection must not reserve the market forever.
    asyncio.run(OrderExecutor(make_bot(tmp_path, "other"), Exchange()).open("btc", "usdt", "buy", "1", "100", 1))


@pytest.mark.parametrize("code", ["NoOpenPosition", "ExceedLiability", "ExceedTotalAsset"])
def test_explicit_rejected_close_preserves_position_without_uncertain_order(tmp_path, code):
    bot, client = make_bot(tmp_path), Exchange()
    executor = OrderExecutor(bot, client)
    asyncio.run(executor.open("btc", "usdt", "buy", "1", "100", 1))
    def rejected(**payload):
        raise NobitexAPIError(code, "Close rejected", http_status=400)
    client.close_position = rejected
    with pytest.raises(RuntimeError, match=code):
        asyncio.run(executor.close(42, "1", "100"))
    assert not bot.unresolved_intents()
    assert bot._exchange_position_id == 42
    assert bot._store.owner(bot._account_key(), 42) == bot.id


@pytest.mark.parametrize("code,http_status", [("DuplicateOrder", 400), ("UnknownError", 400), ("BadPrice", 503)])
def test_ambiguous_submission_stays_reserved(tmp_path, code, http_status):
    bot, client = make_bot(tmp_path), Exchange()
    def ambiguous(**payload):
        raise NobitexAPIError(code, "Ambiguous response", http_status=http_status)
    client.open_margin_order = ambiguous
    with pytest.raises(RuntimeError, match="uncertain"):
        asyncio.run(OrderExecutor(bot, client).open("btc", "usdt", "buy", "1", "100", 1))
    assert bot.unresolved_intents()[0]["state"] == "uncertain"


def test_poll_recovers_from_read_failure_without_new_submission(tmp_path, poll_clock):
    bot, client = make_bot(tmp_path), Exchange()
    executor = OrderExecutor(bot, client)
    status = client.order_status
    reads = 0
    def intermittent(order_id):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise TimeoutError("Temporary status timeout")
        return status(order_id)
    client.order_status = intermittent
    asyncio.run(executor.open("btc", "usdt", "buy", "1", "100", 1, timeout=60))
    assert bot._exchange_position_id == 42
    assert not bot.unresolved_intents()
    assert len(client.sent) == 1


def test_terminal_order_awaiting_position_is_not_canceled_again(tmp_path, poll_clock):
    bot, client = make_bot(tmp_path), Exchange()
    executor = OrderExecutor(bot, client)
    intent = executor._new("open", "btc", "usdt", "buy", {"amount": "1", "price": "100"}, [])
    asyncio.run(executor.submit(intent))
    client.active = []  # Position publication lags behind the completed fill.
    canceled = []
    client.cancel_order = lambda order_id: canceled.append(order_id)
    with pytest.raises(RuntimeError, match="unresolved"):
        asyncio.run(executor.poll(intent, 0))
    assert not canceled
    assert bot.unresolved_intents()[0]["state"] == "awaiting_position"


def test_later_fill_refreshes_owned_position_without_relisting_account(tmp_path, poll_clock):
    bot, client = make_bot(tmp_path), Exchange()
    positions = client.positions
    position_calls = []
    def counted(status="active"):
        position_calls.append(status)
        return positions(status)
    client.positions = counted
    reads = 0
    def partial_then_done(order_id):
        nonlocal reads
        reads += 1
        return dict(client.orders[order_id], status="Active" if reads == 1 else "Done",
                    matchedAmount="0.25" if reads == 1 else "1")
    client.order_status = partial_then_done
    asyncio.run(OrderExecutor(bot, client).open("btc", "usdt", "buy", "1", "100", 1, timeout=60))
    assert bot._exchange_position_id == 42
    assert not bot.unresolved_intents()
    assert position_calls == ["active", "active"]
    assert len(client.sent) == 1


def test_terminal_zero_fill_reports_failure_without_market_reservation(tmp_path, poll_clock):
    bot, client = make_bot(tmp_path), Exchange()
    def canceled(order_id):
        return dict(client.orders[order_id], status="Canceled", matchedAmount="0")
    client.order_status = canceled
    with pytest.raises(RuntimeError, match="without.*fill"):
        asyncio.run(OrderExecutor(bot, client).open("btc", "usdt", "buy", "1", "100", 1))
    assert not bot.unresolved_intents()
    assert bot._exchange_position_id is None


def test_later_partial_fill_preserves_requested_exit(tmp_path):
    bot, client = make_bot(tmp_path), Exchange()
    executor = OrderExecutor(bot, client)
    intent = executor._new("open", "btc", "usdt", "buy", {"amount": "1", "price": "100"}, [])
    asyncio.run(executor.submit(intent))
    client.orders[12].update(status="Active", matchedAmount="0.25")
    asyncio.run(executor.refresh(intent))
    bot._seed_position.update(exit_requested=True, last_evaluated_at="2026-01-01T01:00:00Z")
    client.active[0]["entryPrice"] = "101"
    client.orders[12]["matchedAmount"] = "0.5"
    asyncio.run(executor.refresh(intent))
    assert bot._seed_position["entry_price"] == 101.
    assert bot._seed_position["exit_requested"] is True
    assert bot._seed_position["last_evaluated_at"] == "2026-01-01T01:00:00Z"
