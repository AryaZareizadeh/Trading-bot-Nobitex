import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import pandas as pd

from config import Config
from data import DataFetcher
from indicators import IndicatorSet
from strategy import AdvRSICombinedStrategy, ComboStrategy, ClaudeSuggestedStrategy, SRSIStrategy

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

RESOLUTION_SECONDS: dict[str, int] = {
    "1": 60, "5": 300, "15": 900, "30": 1800,
    "60": 3600, "180": 10800, "240": 14400,
    "D": 86400, "W": 604800, "M": 2592000,
}

STRATEGY_MAP = {
    "AdvRSI": AdvRSICombinedStrategy,
    "Combo": ComboStrategy,
    "ClaudeSuggested": ClaudeSuggestedStrategy,
    "SRSI": SRSIStrategy,
}


class BotStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class BotConfig:
    symbol: str
    strategy_name: str
    timeframe: str
    credit: float
    leverage: int = 5
    mode: str = "simulation"
    name: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])


class Bot:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        if cfg.mode not in ("simulation", "real"):
            raise ValueError(f"Invalid mode: {cfg.mode!r}")
        self.status = BotStatus.IDLE
        self.df: Optional[pd.DataFrame] = None
        self.open_positions: list[dict] = []
        self.completed_trades: list[dict] = []
        self.total_pnl: float = 0.0
        self.balance: float = cfg.credit
        self.current_price: Optional[float] = None
        self.last_updated: Optional[datetime] = None
        self.error: Optional[str] = None
        self.created_at: datetime = datetime.now(timezone.utc)
        self._task: Optional[asyncio.Task] = None
        self._stop_event: asyncio.Event = asyncio.Event()
        self._live_from_ts: Optional[pd.Timestamp] = None

        # Performance metrics
        self.win_rate: float = 0.0
        self.max_drawdown: float = 0.0
        self.balance_history: list[float] = []   # balance at each completed trade, last 60 pts
        self.closed_trade_count: int = 0         # trades that have actually closed (excludes pending open-position records)

        # Activity log (newest appended, capped at 200)
        self.activity_log: list[dict] = []

        # Real-trading state (unused in simulation mode)
        self._exchange_client = None
        self._real_state: str = "none"
        self._exchange_position_id: Optional[int] = None

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log_event(self, event: str, detail: str = "") -> None:
        self.activity_log.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "detail": detail,
        })
        if len(self.activity_log) > 200:
            self.activity_log = self.activity_log[-200:]

    # ── Reconfiguration ───────────────────────────────────────────────────────

    def reconfigure(self, new_cfg: BotConfig) -> None:
        """Reset bot to a fresh state with a new config. Call only when stopped."""
        self.cfg = new_cfg
        self.df = None
        self.open_positions = []
        self.completed_trades = []
        self.total_pnl = 0.0
        self.balance = new_cfg.credit
        self.current_price = None
        self.last_updated = None
        self.error = None
        self._live_from_ts = None
        self.win_rate = 0.0
        self.max_drawdown = 0.0
        self.balance_history = []
        self.closed_trade_count = 0
        self._exchange_client = None
        self._real_state = "none"
        self._exchange_position_id = None
        self.status = BotStatus.IDLE
        self._log_event(
            "reconfigured",
            f"{new_cfg.symbol} | {new_cfg.strategy_name} | {new_cfg.timeframe} | {new_cfg.leverage}x | {new_cfg.mode}",
        )

    @property
    def id(self) -> str:
        return self.cfg.id

    # ── Real-trading helpers ───────────────────────────────────────────────────

    def _parse_symbol(self) -> tuple[str, str]:
        sym = self.cfg.symbol.lower()
        if sym.endswith("usdt"):
            return sym[:-4], "usdt"
        if sym.endswith("irt"):
            return sym[:-3], "rls"
        raise ValueError(f"Cannot infer currency pair from symbol {sym!r}")

    def _strategy_signal(self) -> str:
        if self._live_from_ts is None:
            return "none"
        for pos in self.open_positions:
            entry = pd.Timestamp(pos["entry_time"])
            if entry >= self._live_from_ts:
                return "long" if pos["direction"] == "Long" else "short"
        return "none"

    async def _sync_exchange(self) -> None:
        if self.cfg.mode != "real" or self.current_price is None:
            return
        if self._exchange_client is None:
            try:
                import os
                from nobitex_client import NobitexClient
                testnet = os.getenv("NOBITEX_TESTNET", "false").lower() == "true"
                self._exchange_client = NobitexClient(testnet=testnet)
            except Exception as exc:
                self.error = f"Exchange client init failed: {exc}"
                return

        signal = self._strategy_signal()
        if signal == self._real_state:
            return

        src, dst = self._parse_symbol()
        price = self.current_price
        price_str = f"{price:.2f}"
        amount = round(self.cfg.credit * self.cfg.leverage / price, 6)
        amount_str = f"{amount:.6f}"
        client = self._exchange_client

        try:
            if self._real_state != "none" and self._exchange_position_id is not None:
                try:
                    pos = client.position(self._exchange_position_id)
                    close_amt = (
                        pos.get("delegatedAmount")
                        or pos.get("liability")
                        or amount_str
                    )
                    client.close_position(
                        self._exchange_position_id,
                        amount=str(close_amt),
                        price=price_str,
                        execution="market",
                    )
                except Exception as exc:
                    self.error = f"Close position {self._exchange_position_id} failed: {exc}"
                    return
                self._exchange_position_id = None

            if signal != "none":
                existing_ids: set[int] = {p["id"] for p in client.positions("active")}
                side = "buy" if signal == "long" else "sell"
                client.open_margin_order(
                    src_currency=src,
                    dst_currency=dst,
                    side=side,
                    amount=amount_str,
                    price=price_str,
                    leverage=self.cfg.leverage,
                    execution="market",
                )
                await asyncio.sleep(2)
                new_positions = client.positions("active")
                new_ids = {p["id"] for p in new_positions} - existing_ids
                self._exchange_position_id = new_ids.pop() if new_ids else None

            self._real_state = signal
            self.error = None

        except Exception as exc:
            self.error = f"Exchange sync error: {exc}"

    # ── Config / strategy helpers ──────────────────────────────────────────────

    def _make_config(self) -> Config:
        c = Config()
        c.symbol = self.cfg.symbol
        c.interval = self.cfg.timeframe
        c.limit = 500
        c.leverage = self.cfg.leverage
        c.trade_size = self.cfg.credit
        c.initial_capital = self.cfg.credit
        return c

    def _run_strategy(self) -> None:
        if self.df is None or len(self.df) < 50:
            return

        config = self._make_config()
        indicators = IndicatorSet(self.df, config)
        strategy_cls = STRATEGY_MAP[self.cfg.strategy_name]
        strategy = strategy_cls(config)

        live_start_idx = 0
        if self._live_from_ts is not None:
            live_start_idx = int(self.df.index.searchsorted(self._live_from_ts))

        # Capture state before running for change detection
        old_closed_count = self.closed_trade_count
        old_pos_keys = {(p["direction"], p["entry_time"]) for p in self.open_positions}

        trades_df = strategy.run(self.df, indicators, live_start_idx)

        self.completed_trades = trades_df.to_dict("records") if not trades_df.empty else []
        self.open_positions = strategy.open_positions
        self.current_price = float(self.df["Close"].iloc[-1])

        # Each strategy appends one pending record per open position at the END of its log.
        # So the first `closed_trade_count` records are real closed trades; the rest are pending.
        new_closed_count = len(self.completed_trades) - len(self.open_positions)
        self.closed_trade_count = max(0, new_closed_count)

        # P&L is computed only from closed trades
        closed_pnls = [
            float(str(t.get("P&L ($)", 0)))
            for t in self.completed_trades[:self.closed_trade_count]
        ]
        self.total_pnl = sum(closed_pnls)
        self.balance = self.cfg.credit + self.total_pnl

        # Win rate
        wins = sum(1 for p in closed_pnls if p > 0)
        self.win_rate = round(wins / len(closed_pnls) * 100, 1) if closed_pnls else 0.0

        # Max drawdown from closed trade P&L series
        if closed_pnls:
            running = self.cfg.credit
            peak = running
            max_dd = 0.0
            for pnl in closed_pnls:
                running += pnl
                if running > peak:
                    peak = running
                dd = (peak - running) / peak * 100 if peak > 0 else 0.0
                if dd > max_dd:
                    max_dd = dd
            self.max_drawdown = round(max_dd, 2)
        else:
            self.max_drawdown = 0.0

        # Balance history — from closed trades only
        hist = [self.cfg.credit]
        running = self.cfg.credit
        for pnl in closed_pnls:
            running += pnl
            hist.append(round(running, 2))
        self.balance_history = hist[-60:]

        # Log newly CLOSED trades (closed_count increases only when a position actually closes)
        if self.closed_trade_count > old_closed_count:
            for t in self.completed_trades[old_closed_count:self.closed_trade_count]:
                pnl = float(str(t.get("P&L ($)", 0)))
                sign = "+" if pnl >= 0 else ""
                liq = " [LIQUIDATED]" if t.get("Liquidated") else ""
                self._log_event(
                    "trade_closed",
                    f"{t.get('Position')} closed @ ${t.get('End Price')} | P&L: {sign}${pnl:.2f}{liq}",
                )

        # Log newly OPENED positions (new entry_time appeared in open_positions)
        new_pos_keys = {(p["direction"], p["entry_time"]) for p in self.open_positions}
        for direction, entry_time in new_pos_keys - old_pos_keys:
            pos = next(
                (p for p in self.open_positions if p["direction"] == direction and p["entry_time"] == entry_time),
                None,
            )
            if pos:
                self._log_event(
                    "position_opened",
                    f"{direction} opened @ ${pos['entry_price']:.2f}",
                )

    def _validate_and_append(self, new_df: pd.DataFrame) -> bool:
        if self.df is None or new_df is None or new_df.empty:
            return False

        last_ts = self.df.index[-1].timestamp()
        first_new_ts = new_df.index[0].timestamp()

        if first_new_ts <= last_ts:
            return True

        candle_secs = RESOLUTION_SECONDS[self.cfg.timeframe]
        expected_next_ts = last_ts + candle_secs

        if first_new_ts > expected_next_ts + candle_secs:
            return False

        self.df = pd.concat([self.df, new_df])
        self.df = self.df[~self.df.index.duplicated(keep="last")].sort_index().tail(500)
        return True

    async def run_backtest(self) -> dict:
        """Fetch 500 candles and simulate the strategy over all of them. Returns profitability summary."""
        config = self._make_config()
        fetcher = DataFetcher(config)
        df = await asyncio.to_thread(fetcher.fetch)

        indicators = IndicatorSet(df, config)
        strategy_cls = STRATEGY_MAP[self.cfg.strategy_name]
        strategy = strategy_cls(config)
        trades_df = strategy.run(df, indicators, live_start_idx=0)

        candle_count = len(df)
        if trades_df.empty:
            return {"profitable": None, "total_pnl": 0.0, "trade_count": 0, "win_rate": 0.0, "candle_count": candle_count}

        open_count = len(strategy.open_positions)
        closed_count = max(0, len(trades_df) - open_count)
        records = trades_df.to_dict("records")

        # Skip the first closed trade — indicators lack warmup data at the very start
        valid_records = records[1:closed_count]
        if not valid_records:
            return {"profitable": None, "total_pnl": 0.0, "trade_count": 0, "win_rate": 0.0, "candle_count": candle_count, "trades": []}

        closed_pnls = [float(str(r.get("P&L ($)", 0))) for r in valid_records]
        total_pnl = sum(closed_pnls)
        wins = sum(1 for p in closed_pnls if p > 0)
        win_rate = round(wins / len(closed_pnls) * 100, 1) if closed_pnls else 0.0

        return {
            "profitable": total_pnl > 0,
            "total_pnl": round(total_pnl, 2),
            "trade_count": len(valid_records),
            "win_rate": win_rate,
            "candle_count": candle_count,
            "trades": valid_records,
        }

    async def start(self) -> None:
        if self.status == BotStatus.RUNNING:
            return
        self._stop_event.clear()
        self.status = BotStatus.RUNNING
        self.error = None
        self._log_event("bot_started", f"Mode: {self.cfg.mode}")
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.status = BotStatus.STOPPED
        self._log_event("bot_stopped")

    async def _run_loop(self) -> None:
        try:
            fetcher = DataFetcher(self._make_config())
            for attempt in range(5):
                try:
                    self.df = await asyncio.to_thread(fetcher.fetch)
                    break
                except Exception as exc:
                    if attempt == 4:
                        raise RuntimeError(f"Initial data fetch failed: {exc}") from exc
                    await asyncio.sleep(2 ** attempt)

            _res = RESOLUTION_SECONDS[self.cfg.timeframe]
            self._live_from_ts = self.df.index[-1] + pd.Timedelta(seconds=_res)
            self.current_price = float(self.df["Close"].iloc[-1])
            self.last_updated = datetime.now(timezone.utc)
            self.error = None
            self._log_event("data_loaded", f"Loaded {len(self.df)} warmup candles. Live trading starts at next candle close.")

            while not self._stop_event.is_set():
                candle_secs = RESOLUTION_SECONDS[self.cfg.timeframe]
                last_ts = self.df.index[-1].timestamp()
                next_close_ts = last_ts + candle_secs
                sleep_secs = max(5.0, next_close_ts - time.time() + 5)

                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._stop_event.wait()),
                        timeout=sleep_secs,
                    )
                    break
                except asyncio.TimeoutError:
                    pass

                if self._stop_event.is_set():
                    break

                new_df: Optional[pd.DataFrame] = None
                fetcher = DataFetcher(self._make_config())
                for attempt in range(5):
                    try:
                        new_df = await asyncio.to_thread(fetcher.fetch_latest)
                        if new_df is not None and not new_df.empty:
                            break
                    except Exception:
                        pass
                    if attempt < 4:
                        await asyncio.sleep(2 ** attempt)

                if new_df is None or new_df.empty:
                    try:
                        self.df = await asyncio.to_thread(fetcher.fetch)
                        self.error = "Candle fetch failed; did full re-fetch for integrity."
                        self._log_event("warning", self.error)
                    except Exception as exc:
                        self.error = f"Full re-fetch failed: {exc}"
                        self._log_event("error", self.error)
                        continue
                else:
                    ok = self._validate_and_append(new_df)
                    if not ok:
                        try:
                            self.df = await asyncio.to_thread(fetcher.fetch)
                            self.error = "Data gap detected; re-fetched all candles."
                            self._log_event("warning", self.error)
                        except Exception as exc:
                            self.error = f"Gap repair failed: {exc}"
                            self._log_event("error", self.error)
                            continue
                    else:
                        self.error = None

                try:
                    self._run_strategy()
                except Exception as exc:
                    self.error = f"Strategy error: {exc}"
                    self._log_event("error", self.error)

                await self._sync_exchange()
                self.last_updated = datetime.now(timezone.utc)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.status = BotStatus.ERROR
            self.error = str(exc)
            self._log_event("error", str(exc))
        finally:
            if self.status == BotStatus.RUNNING:
                self.status = BotStatus.STOPPED

    def to_state_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.cfg.name or self.id,
            "mode": self.cfg.mode,
            "symbol": self.cfg.symbol.upper(),
            "strategy": self.cfg.strategy_name,
            "timeframe": self.cfg.timeframe,
            "credit": self.cfg.credit,
            "leverage": self.cfg.leverage,
            "status": self.status.value,
            "open_positions": self.open_positions,
            "current_price": self.current_price,
            "total_pnl": round(self.total_pnl, 2),
            "balance": round(self.balance, 2),
            "trade_count": len(self.completed_trades),
            "closed_trade_count": self.closed_trade_count,
            "candle_count": len(self.df) if self.df is not None else 0,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
            "created_at": self.created_at.isoformat(),
            "error": self.error,
            # Metrics
            "win_rate": self.win_rate,
            "max_drawdown": self.max_drawdown,
            "balance_history": self.balance_history[-30:],
        }
        if self.cfg.mode == "real":
            d["exchange_state"] = self._real_state
            d["exchange_position_id"] = self._exchange_position_id
        return d

    def to_detail_dict(self) -> dict:
        d = self.to_state_dict()
        d["trades"] = self.completed_trades
        d["activity_log"] = list(reversed(self.activity_log))
        return d
