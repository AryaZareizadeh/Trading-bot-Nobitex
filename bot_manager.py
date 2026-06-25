import json
from pathlib import Path

from bot import Bot, BotConfig

_BOTS_FILE = Path(__file__).parent / "bots.json"


class BotManager:
    def __init__(self):
        self._bots: dict[str, Bot] = {}

    def _save(self) -> None:
        data = [
            {
                "id": bot.id,
                "name": bot.cfg.name,
                "symbol": bot.cfg.symbol,
                "strategy_name": bot.cfg.strategy_name,
                "timeframe": bot.cfg.timeframe,
                "credit": bot.cfg.credit,
                "leverage": bot.cfg.leverage,
                "mode": bot.cfg.mode,
                "custom_params": bot.cfg.custom_params,
            }
            for bot in self._bots.values()
        ]
        try:
            _BOTS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def load(self) -> None:
        if not _BOTS_FILE.exists():
            return
        try:
            data = json.loads(_BOTS_FILE.read_text(encoding="utf-8"))
            for item in data:
                cfg = BotConfig(**item)
                bot = Bot(cfg)
                self._bots[bot.id] = bot
        except Exception:
            pass

    def create_bot(self, cfg: BotConfig) -> Bot:
        bot = Bot(cfg)
        self._bots[bot.id] = bot
        self._save()
        return bot

    def get_bot(self, bot_id: str) -> Bot | None:
        return self._bots.get(bot_id)

    def get_all(self) -> list[Bot]:
        return list(self._bots.values())

    def remove_bot(self, bot_id: str) -> None:
        self._bots.pop(bot_id, None)
        self._save()

    def get_all_states(self) -> list[dict]:
        return [bot.to_state_dict() for bot in self._bots.values()]


bot_manager = BotManager()
