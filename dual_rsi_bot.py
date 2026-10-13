"""One managed bot with independent long and short RSI execution accounts."""
import asyncio
import copy
import math
from dataclasses import replace

import pandas as pd

from bot import Bot, BotConfig, BotStatus
from strategy import AdvRsiLongParams, AdvRsiShortParams

STRATEGY_NAME = "AdvRSIIndependent"
SIDE_STRATEGIES = {"long": "AdvRSILong", "short": "AdvRSIShort"}


def combined_equity(sides):
    """Sum timestamp-aligned equity, including a side's idle initial credit."""
    series = []
    for side in sides:
        points = side.equity_curve
        if points:
            series.append((pd.Series({p["time"]: p["equity"] for p in points}), side.cfg.credit))
        else:
            series.append((pd.Series(dtype=float), side.cfg.credit))
    times = sorted({ts for curve, _ in series for ts in curve.index})
    if not times:
        return []
    total = sum(curve.reindex(times).ffill().fillna(credit) for curve, credit in series)
    return [{"time": int(ts), "equity": float(value)} for ts, value in total.items()]


class _SideBot(Bot):
    def __init__(self, cfg, parent, side):
        super().__init__(cfg)
        self.parent, self.side = parent, side

    def runtime_record(self):
        # Order resolution and both accounts are committed in the same transaction.
        return self.parent.id, self.parent.snapshot()

    def can_share_position(self, position):
        other = self.parent.sides["short" if self.side == "long" else "long"]
        expected = "sell" if self.side == "long" else "buy"
        return (position.get("side") == expected and position.get("id") == other._exchange_position_id
                and self._store is not None
                and self._store.owner(self._account_key(), position["id"]) == other.id)

    async def _sync_exchange(self):
        # Serialize submissions within this bot so each sees the other's latest fill.
        async with self.parent._execution_lock:
            await super()._sync_exchange()


class DualRsiBot(Bot):
    def __init__(self, cfg: BotConfig):
        cfg = copy.deepcopy(cfg)
        for side in SIDE_STRATEGIES:
            value = getattr(cfg, side + "_credit")
            value = 100.0 if value is None else float(value)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{side.title()} credit must be finite and positive")
            setattr(cfg, side + "_credit", value)
        cfg.credit = cfg.long_credit + cfg.short_credit
        params = cfg.custom_params or {}
        if not isinstance(params, dict) or set(params) - set(SIDE_STRATEGIES):
            raise ValueError("Combined RSI parameters must have separate long and short dictionaries")
        for side, cls in (("long", AdvRsiLongParams), ("short", AdvRsiShortParams)):
            try:
                cls(**params.get(side, {}))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid {side} parameters: {exc}") from exc
        super().__init__(replace(cfg, strategy_name="AdvRSILong", custom_params=None))
        self.cfg = cfg
        self._execution_lock = asyncio.Lock()
        self.sides = {
            side: _SideBot(replace(cfg, id=f"{cfg.id}:{side}", strategy_name=strategy,
                                  credit=getattr(cfg, side + "_credit"),
                                  name=f"{cfg.name or cfg.id} · {side.title()}",
                                  custom_params=copy.deepcopy(params.get(side)),
                                  long_credit=None, short_credit=None), self, side)
            for side, strategy in SIDE_STRATEGIES.items()
        }

    def execution_bots(self):
        return list(self.sides.values())

    def unresolved_intents(self):
        return [intent for bot in self.execution_bots() for intent in bot.unresolved_intents()]

    def _aggregate(self):
        runners = self.execution_bots()
        self.open_positions = [dict(pos, side=side) for side, bot in self.sides.items() for pos in bot.open_positions]
        self._real_positions = [dict(pos, side=side) for side, bot in self.sides.items() for pos in bot._real_positions]
        closed, pending = [], []
        for side, bot in self.sides.items():
            closed.extend(dict(trade, side=side) for trade in bot.completed_trades[:bot.closed_trade_count])
            pending.extend(dict(trade, side=side) for trade in bot.completed_trades[bot.closed_trade_count:])
        closed.sort(key=lambda trade: str(trade.get("End Time", "")))
        self.completed_trades = closed + pending
        self.closed_trade_count = len(closed)
        self.total_pnl = sum(bot.total_pnl for bot in runners)
        self.balance = sum(bot.balance for bot in runners)
        self.win_rate = 100 * sum(float(t.get("P&L ($)", 0)) > 0 for t in closed) / len(closed) if closed else 0.0
        self.equity_curve = combined_equity(runners)
        values = [p["equity"] for p in self.equity_curve]
        if not values:
            value = self.cfg.credit
            values = [value]
            for trade in closed:
                value += float(trade.get("P&L ($)", 0))
                values.append(value)
        peak, drawdown = self.cfg.credit, 0.0
        for value in values:
            peak = max(peak, value)
            drawdown = max(drawdown, 100 * (peak - value) / peak)
        self.max_drawdown = round(drawdown, 2)
        self.balance_history = values[-60:]
        frames = [bot.df for bot in runners if bot.df is not None]
        self.df = max(frames, key=len) if frames else None
        updated = [bot for bot in runners if bot.last_updated is not None]
        latest = max(updated, key=lambda bot: bot.last_updated) if updated else next(
            (bot for bot in runners if bot.current_price is not None), None)
        self.current_price = latest.current_price if latest else None
        self.last_updated = latest.last_updated if latest else None
        errors = [f"{side.title()}: {bot.error}" for side, bot in self.sides.items() if bot.error]
        self.error = "; ".join(errors) if errors else None

    def snapshot(self):
        self._aggregate()
        raw = super().snapshot()
        raw["candles"] = None  # The full, independent runner snapshots own candle history.
        raw["numeric_paths"] = [path for path in raw["numeric_paths"] if path[0] != "candles"]
        raw["side_runtimes"] = {side: bot.snapshot() for side, bot in self.sides.items()}
        return raw

    def restore(self, raw):
        super().restore(raw)
        for side, saved in raw.get("side_runtimes", {}).items():
            self.sides[side].restore(saved)
        self._aggregate()

    def _run_strategy(self):
        # Also useful when evaluating both runners against one supplied candle frame.
        for bot in self.execution_bots():
            bot.df = self.df
            bot._live_from_ts = self._live_from_ts
            bot._run_strategy()
        self._aggregate()

    async def start(self):
        if self.status == BotStatus.RUNNING:
            return
        self.status = BotStatus.RUNNING
        self._log_event("bot_started", "Long and short runners started")
        await asyncio.gather(*(bot.start() for bot in self.execution_bots()))
        self._task = asyncio.create_task(self._watch_sides())
        self._persist()

    async def _watch_sides(self):
        await asyncio.wait([bot._task for bot in self.execution_bots()], return_when=asyncio.FIRST_COMPLETED)
        failed = any(bot.status == BotStatus.ERROR for bot in self.execution_bots())
        for bot in self.execution_bots():
            if bot.status == BotStatus.RUNNING:
                await bot.stop()
        self.status = BotStatus.ERROR if failed else BotStatus.STOPPED
        self._aggregate()
        self._log_event('bot_down', self.error or 'One of the independent runners ended')
        self._persist()

    async def stop(self):
        if self.status == BotStatus.STOPPED and (self._task is None or self._task.done()):
            return
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await asyncio.gather(*(bot.stop() for bot in self.execution_bots()))
        self.status = BotStatus.STOPPED
        self._log_event("bot_stopped", "Long and short runners stopped")
        self._persist()

    def get_chart_data(self, side="long"):
        if side not in self.sides:
            raise ValueError("Choose long or short for the chart")
        data = self.sides[side].get_chart_data()
        return {**data, "paired": True, "side": side}

    async def run_backtest(self):
        from data import DataFetcher
        from execution_model import evaluate_research, required_warmup
        from position_limits import current_max_hold_days

        execution = {**(self.cfg.execution or {}), "max_hold_days": await asyncio.to_thread(current_max_hold_days)}
        warmup = max(required_warmup(bot._build_custom_params() or cls())
                     for bot, cls in zip(self.execution_bots(), (AdvRsiLongParams, AdvRsiShortParams)))
        config = self.sides["long"]._make_config()
        config.limit = 500 + warmup
        df = await asyncio.to_thread(DataFetcher(config).fetch)
        start = max(warmup, len(df) - 500)
        results = {}
        for side, bot in self.sides.items():
            results[side] = await asyncio.to_thread(evaluate_research, df, bot.cfg.strategy_name,
                bot.cfg.credit, bot.cfg.leverage, bot.cfg.custom_params, execution, start)
        trades = sorted([dict(t, side=side) for side, result in results.items() for t in result["trades"]],
                        key=lambda t: str(t.get("End Time", "")))
        pnl = sum(result["summary"]["total_pnl"] for result in results.values())
        pnls = [float(t["P&L ($)"]) for t in trades]
        long_count = sum(t["Position"] == "Long" for t in trades)
        return {"total_pnl": pnl, "profitable": pnl > 0, "trade_count": len(trades),
                "win_rate": 100 * sum(p > 0 for p in pnls) / len(pnls) if pnls else 0,
                "sum_profits": sum(p for p in pnls if p > 0), "sum_losses": sum(p for p in pnls if p < 0),
                "long_pct": 100 * long_count / len(trades) if trades else 0,
                "candle_count": max(0, len(df) - start), "trades": trades,
                "sides": {side: result["summary"] for side, result in results.items()}}

    def to_state_dict(self):
        self._aggregate()
        data = super().to_state_dict()
        logs = list(self.activity_log)
        for side, bot in self.sides.items():
            logs.extend(dict(item, detail=f"{side.title()} · {item['detail']}") for item in bot.activity_log)
        logs.sort(key=lambda item: item["time"], reverse=True)
        data.update(paired=True, long_credit=self.cfg.long_credit, short_credit=self.cfg.short_credit,
                    sides={side: bot.to_state_dict() for side, bot in self.sides.items()},
                    pending_orders=[bot._pending_order for bot in self.execution_bots() if bot._pending_order],
                    activity_log=logs[:200])
        return data

    def to_detail_dict(self):
        data = self.to_state_dict()
        details = {side: bot.to_detail_dict() for side, bot in self.sides.items()}
        data.update(trades=self.completed_trades, sides=details, unresolved_orders=self.unresolved_intents(),
                    order_ledger=[item for d in details.values() for item in d["order_ledger"]],
                    closed_exchange_positions=[item for d in details.values() for item in d["closed_exchange_positions"]])
        from bot_history import detail_history
        data.update(detail_history(self))
        return data


def make_bot(cfg):
    return DualRsiBot(cfg) if cfg.strategy_name == STRATEGY_NAME else Bot(cfg)
