"""Bot registry backed by transactional SQLite configuration and snapshots."""
import json
from dataclasses import asdict
from pathlib import Path

from bot import Bot, BotConfig
from storage import get_store
from settings import ROOT


class BotManager:
    def __init__(self, store=None, legacy_dir=None, client_factory=None, account_id=None,
                 testnet_factory=None):
        self._bots = {}
        self._store = store
        self.legacy_dir = Path(legacy_dir) if legacy_dir else ROOT
        self.client_factory = client_factory
        self.account_id = account_id
        self.testnet_factory = testnet_factory

    def _configure_bot(self, bot):
        bot._client_factory = self.client_factory
        bot._tenant_account_id = self.account_id
        bot._testnet_factory = self.testnet_factory
        return bot

    @property
    def store(self):
        return self._store or get_store()

    def _save(self):
        with self.store.connection() as conn:
            for bot in self._bots.values():
                self.store.write(conn, "bots", bot.id, asdict(bot.cfg))
                self.store.write(conn, "runtime", bot.id, bot.snapshot())

    def load(self):
        if not self.store.get("meta", "imported"):
            imports = {}
            for filename in ("bots", "tester", "optimizer"):
                path = self.legacy_dir / f"{filename}.json"
                if path.exists():
                    imports[filename] = json.loads(path.read_text(encoding="utf-8"))
            configs = [BotConfig(**item) for item in imports.get("bots", [])]
            for cfg in configs:
                Bot(cfg)
            with self.store.connection() as conn:
                for cfg in configs:
                    self.store.write(conn, "bots", cfg.id, asdict(cfg))
                for name in ("tester", "optimizer"):
                    if name in imports:
                        self.store.write(conn, "prefs", name, imports[name])
                self.store.write(conn, "meta", "imported", True)
        loaded = {}
        for key, raw in self.store.list("bots").items():
            bot = self._configure_bot(Bot(BotConfig(**raw)))
            bot._store = self.store
            snapshot = self.store.get("runtime", key)
            if snapshot:
                bot.restore(snapshot)
            loaded[bot.id] = bot
        self._bots = loaded

    def create_bot(self, cfg):
        bot = self._configure_bot(Bot(cfg))
        bot._store = self.store
        with self.store.connection() as conn:
            self.store.write(conn, "bots", bot.id, asdict(cfg))
            self.store.write(conn, "runtime", bot.id, bot.snapshot())
        self._bots[bot.id] = bot
        return bot

    def update_bot(self, bot, cfg):
        if bot._exchange_position_id is not None or bot.unresolved_intents():
            raise ValueError("Cannot reconfigure a bot with an owned position or unresolved order")
        Bot(cfg)
        previous_cfg, previous = bot.cfg, bot.snapshot()
        try:
            bot.reconfigure(cfg)
            self._save()
        except Exception:
            bot.cfg = previous_cfg
            bot.restore(previous)
            raise

    def get_bot(self, bot_id):
        return self._bots.get(bot_id)

    def get_all(self):
        return list(self._bots.values())

    def remove_bot(self, bot_id):
        bot = self._bots.get(bot_id)
        if bot and (bot._exchange_position_id is not None or bot.unresolved_intents()):
            raise ValueError("Cannot delete a bot with an owned position or unresolved order")
        with self.store.connection() as conn:
            conn.execute("DELETE FROM records WHERE namespace IN ('bots','runtime') AND key=?", (bot_id,))
        self._bots.pop(bot_id, None)

    def get_all_states(self):
        return [bot.to_state_dict() for bot in self._bots.values()]


bot_manager = BotManager()
