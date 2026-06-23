from bot import Bot, BotConfig


class BotManager:
    def __init__(self):
        self._bots: dict[str, Bot] = {}

    def create_bot(self, cfg: BotConfig) -> Bot:
        bot = Bot(cfg)
        self._bots[bot.id] = bot
        return bot

    def get_bot(self, bot_id: str) -> Bot | None:
        return self._bots.get(bot_id)

    def get_all(self) -> list[Bot]:
        return list(self._bots.values())

    def remove_bot(self, bot_id: str) -> None:
        self._bots.pop(bot_id, None)

    def get_all_states(self) -> list[dict]:
        return [bot.to_state_dict() for bot in self._bots.values()]


bot_manager = BotManager()
