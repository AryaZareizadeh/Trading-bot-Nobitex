import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import position_limits as limits
from tests.test_execution_model import candles, simulate


def test_published_rule_parser_accepts_changed_day_count_and_rejects_unrelated_numbers():
    assert limits.parse_published_limit("<p>هر سفارش تعهدی، حداکثر تا ۶۰ روز قابل تمدید است.</p>") == 60
    assert limits.parse_published_limit("<p>هر سفارش تعهدی، حداکثر تا 45 روز قابل تمدید است.</p>") == 45
    with pytest.raises(ValueError, match="not found"):
        limits.parse_published_limit("<p>کارمزد تمدید هر 60 روز محاسبه می‌شود.</p>")


def test_refresh_uses_changed_rule_then_last_good_value(monkeypatch):
    monkeypatch.setenv("TRADER_OFFLINE", "false")
    from config import Config

    monkeypatch.setattr(limits, "_days", limits.DEFAULT_MAX_HOLD_DAYS)
    monkeypatch.setattr(limits, "_verified_at", None)
    monkeypatch.setattr(limits, "_last_refresh_ok", False)
    class Response:
        def __init__(self, html):
            self.html = html
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, *_):
            return self.html.encode()

    current = ["<p>هر سفارش تعهدی، حداکثر تا 45 روز قابل تمدید است.</p>"]
    monkeypatch.setattr(limits, "urlopen", lambda *_a, **_k: Response(current[0]))
    monkeypatch.setattr(limits, "_next_refresh", 0)
    assert limits.current_max_hold_days() == 45
    assert limits.published_limit_status()["refresh_ok"]
    assert Config().max_hold_days == 45
    current[0] = "not a rule"
    monkeypatch.setattr(limits, "_next_refresh", 0)
    assert limits.current_max_hold_days() == 45
    assert not limits.published_limit_status()["refresh_ok"]


def test_offline_rule_lookup_never_opens_a_network_connection(monkeypatch):
    monkeypatch.setattr(limits, "_next_refresh", 0)
    monkeypatch.setattr(limits, "_days", 45)
    def forbidden(*args, **kwargs):
        pytest.fail("Offline rule lookup attempted network access")
    monkeypatch.setattr(limits, "urlopen", forbidden)
    assert limits.current_max_hold_days() == 45


def test_simulator_closes_at_open_when_order_reaches_exchange_limit():
    result = simulate(candles([100] * 7), {0: "Long"}, credit=100, leverage=1,
                      execution={"max_hold_days": 3})
    assert result["summary"]["trade_count"] == 1
    assert result["trades"][0]["Days Open"] == 3
    assert result["trades"][0]["End Time"].startswith("2025-01-05")


def test_exchange_expiration_uses_exact_timestamp_or_conservative_date():
    now = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
    assert limits.should_close_before_expiry({"expirationDate": (now + timedelta(minutes=30)).isoformat()}, now)
    assert not limits.should_close_before_expiry({"expirationDate": (now + timedelta(hours=3)).isoformat()}, now)
    assert limits.exchange_expiration("2026-09-27") == datetime(2026, 9, 26, 20, 30, tzinfo=timezone.utc)


def test_live_bot_uses_exchange_expiration_even_if_published_rule_changes(tmp_path, monkeypatch):
    from bot import Bot, BotConfig
    from exchange_execution import OrderExecutor
    from storage import Store

    bot = Bot(BotConfig("btcusdt", "AdvRSI", "60", 100, mode="real"))
    bot._store = Store(tmp_path / "db.sqlite3")
    bot._exchange_position_id = 42
    bot._real_state = "long"
    bot.current_price = 100
    bot._store.claim_position(bot._account_key(), 42, bot.id)
    now = datetime.now(timezone.utc)
    position = {"id": 42, "srcCurrency": "btc", "dstCurrency": "usdt", "side": "buy",
                "status": "Open", "entryPrice": "100", "openedAt": (now-timedelta(days=1)).isoformat(),
                "expirationDate": (now+timedelta(minutes=20)).isoformat()}
    class Exchange:
        def positions(self, *_):
            return [position]
    bot._exchange_client = Exchange()
    monkeypatch.setattr(bot, "_ensure_exchange_client", lambda: True)
    async def reconcile(self):
        return True
    monkeypatch.setattr(OrderExecutor, "reconcile", reconcile)
    closed = []
    async def close(*_):
        closed.append(42)
        bot._exchange_position_id = None
        return True
    monkeypatch.setattr(bot, "_close_exchange_position", close)
    asyncio.run(bot._sync_exchange())
    assert closed == [42]
    assert bot._real_state == "none"
