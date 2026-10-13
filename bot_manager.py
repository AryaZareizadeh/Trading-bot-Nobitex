"""Bot registry backed by transactional SQLite configuration and snapshots."""
import json
from dataclasses import asdict
from pathlib import Path

from bot import Bot, BotConfig
from dual_rsi_bot import make_bot, STRATEGY_NAME as DUAL_RSI_NAME
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

    def _configure_bot(self, bot, validate_model=True):
        for runner in [bot, *[child for child in bot.execution_bots() if child is not bot]]:
            runner._client_factory = self.client_factory
            runner._tenant_account_id = self.account_id
            runner._testnet_factory = self.testnet_factory
            runner._store = self.store
            runner._model_dir = self.store.path.parent / "models"
            if validate_model:
                runner._validate_saved_model()
        return bot

    @property
    def store(self):
        return self._store or get_store()

    def _save(self, archived_trades=None):
        with self.store.connection() as conn:
            for key, record in (archived_trades or {}).items():
                if record['status'] == 'open':
                    record = dict(record, status='reset')
                self.store.write(conn, 'bot_trades', key, record)
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
                make_bot(cfg)
            with self.store.connection() as conn:
                for cfg in configs:
                    self.store.write(conn, "bots", cfg.id, asdict(cfg))
                for name in ("tester", "optimizer"):
                    if name in imports:
                        self.store.write(conn, "prefs", name, imports[name])
                self.store.write(conn, "meta", "imported", True)
        loaded = {}
        for key, raw in self.store.list("bots").items():
            # A removed checkpoint must not prevent loading unrelated bots.
            # Saved-model availability and metadata are checked again at Start.
            bot = self._configure_bot(make_bot(BotConfig(**raw)), validate_model=False)
            bot._store = self.store
            snapshot = self.store.get("runtime", key)
            if snapshot:
                bot.restore(snapshot)
                if not snapshot.get('created_at'):
                    bot.created_at = None
                if snapshot.get('desired_status') == 'running':
                    bot._log_event('runtime_interrupted',
                                   'Previously running bot restored idle. Interruption detected now; exact shutdown time is unknown.')
                    bot._persist()
            else:
                bot.created_at = None  # Imported configurations have no reliable creation timestamp.
            loaded[bot.id] = bot
        self._bots = loaded

    def create_bot(self, cfg):
        bot = self._configure_bot(make_bot(cfg))
        bot._store = self.store
        with self.store.connection() as conn:
            self._record_creation(bot, conn)
            self.store.write(conn, "bots", bot.id, asdict(bot.cfg))
            self.store.write(conn, "runtime", bot.id, bot.snapshot())
        self._bots[bot.id] = bot
        return bot

    def _record_creation(self, bot, conn):
        from trading_audit import audit_record, record_event
        record = audit_record(bot.id, 'bot_created',
                              f'{bot.cfg.symbol.upper()} | {bot.cfg.strategy_name} | {bot.cfg.mode}')
        record['time'] = bot.created_at.isoformat()
        record_event(self.store, record, conn=conn)
        bot.activity_log.append({key: record[key] for key in ('time', 'event', 'detail')})

    def create_bots(self, configs):
        """Validate and persist a group atomically before exposing any of its bots."""
        bots = [self._configure_bot(make_bot(cfg)) for cfg in configs]
        with self.store.connection() as conn:
            for bot in bots:
                self._record_creation(bot, conn)
                self.store.write(conn, "bots", bot.id, asdict(bot.cfg))
                self.store.write(conn, "runtime", bot.id, bot.snapshot())
        for bot in bots:
            bot._store = self.store
            self._bots[bot.id] = bot
        return bots

    def update_bot(self, bot, cfg):
        if bot.has_owned_position() or bot.unresolved_intents():
            raise ValueError("Cannot reconfigure a bot with an owned position or unresolved order")
        candidate = self._configure_bot(make_bot(cfg))
        from bot_history import simulation_records
        archived_trades = simulation_records(bot)
        if DUAL_RSI_NAME in (bot.cfg.strategy_name, cfg.strategy_name):
            candidate.created_at = bot.created_at
            candidate.activity_log = list(bot.activity_log)
            candidate.status = bot.status
            candidate._exchange_client = bot._exchange_client
            for runner in candidate.execution_bots():
                runner._exchange_client = bot._exchange_client
            self._bots[bot.id] = candidate
            try:
                self._save(archived_trades)
            except Exception:
                self._bots[bot.id] = bot
                raise
            candidate._log_event('reconfigured', f'{cfg.symbol.upper()} | {cfg.strategy_name} | {cfg.mode}')
            candidate._persist()
            return candidate
        previous_cfg, previous = bot.cfg, bot.snapshot()
        try:
            bot.reconfigure(cfg, log_event=False)
            self._save(archived_trades)
        except Exception:
            bot.cfg = previous_cfg
            bot.restore(previous)
            raise
        bot._log_event('reconfigured', f'{cfg.symbol.upper()} | {cfg.strategy_name} | {cfg.mode}')
        bot._persist()
        return bot

    def get_bot(self, bot_id):
        return self._bots.get(bot_id)

    def get_all(self):
        return list(self._bots.values())

    def remove_bot(self, bot_id):
        bot = self._bots.get(bot_id)
        if bot and (bot.has_owned_position() or bot.unresolved_intents()):
            raise ValueError("Cannot delete a bot with an owned position or unresolved order")
        with self.store.connection() as conn:
            conn.execute("DELETE FROM records WHERE namespace IN ('bots','runtime') AND key=?", (bot_id,))
        self._bots.pop(bot_id, None)

    def get_all_states(self):
        return [bot.to_state_dict() for bot in self._bots.values()]


bot_manager = BotManager()
