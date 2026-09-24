import json
import pytest


def test_store_roundtrip_claims_and_backup(tmp_path):
    from storage import Store
    store = Store(tmp_path / "state.db")
    store.put("runtime", "one", {"balance": "19.58"})
    assert Store(store.path).get("runtime", "one") == {"balance": "19.58"}
    store.claim_position("account", 42, "one")
    with pytest.raises(ValueError, match="owned"):
        store.claim_position("account", 42, "two")
    store.backup(tmp_path / "backup.db")
    assert Store(tmp_path / "backup.db").owner("account", 42) == "one"


def test_process_lock_exclusive_and_released(tmp_path):
    from storage import Store
    first, second = Store(tmp_path / "state.db"), Store(tmp_path / "state.db")
    first.acquire_runtime()
    try:
        with pytest.raises(RuntimeError, match="already"):
            second.acquire_runtime()
    finally:
        first.release_runtime()
    second.acquire_runtime()
    second.release_runtime()


def test_legacy_import_once_preserves_custom_params(tmp_path):
    from storage import Store
    from bot_manager import BotManager
    (tmp_path / "bots.json").write_text(json.dumps([dict(id="abc", symbol="btcusdt", strategy_name="AdvRSI", timeframe="60", credit=100, custom_params={"rsi_length": 42})]))
    manager = BotManager(store=Store(tmp_path / "state.db"), legacy_dir=tmp_path)
    manager.load()
    assert manager.get_bot("abc").cfg.custom_params == {"rsi_length": 42}
    manager.remove_bot("abc")
    manager.load()
    assert manager.get_all() == []
    assert (tmp_path / "bots.json").exists()


def test_malformed_import_visible_and_atomic(tmp_path):
    from storage import Store
    from bot_manager import BotManager
    (tmp_path / "bots.json").write_text("{bad")
    store = Store(tmp_path / "state.db")
    with pytest.raises(ValueError):
        BotManager(store=store, legacy_dir=tmp_path).load()
    assert store.get("meta", "imported") is None


def test_runtime_restores_without_auto_start(tmp_path):
    from storage import Store
    from bot_manager import BotManager
    from bot import BotConfig
    store = Store(tmp_path / "state.db")
    manager = BotManager(store=store, legacy_dir=tmp_path)
    manager.load()
    bot = manager.create_bot(BotConfig("btcusdt", "AdvRSI", "60", 100))
    bot.balance = 123.45
    bot.completed_trades = [{"P&L ($)": "23.45"}]
    bot._exchange_position_id = 42
    bot._persist()
    restored = BotManager(store=Store(store.path), legacy_dir=tmp_path)
    restored.load()
    other = restored.get_bot(bot.id)
    assert other.balance == 123.45
    assert other.completed_trades == bot.completed_trades
    assert other._exchange_position_id == 42
    assert other.status.value == "idle"
