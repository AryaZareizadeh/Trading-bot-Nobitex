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
    mode: str = "simulation"   # "simulation" | "real"
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

    @property
    def id(self) -> str:
        return self.cfg.id

    def _make_config(self) -> Config:
        c = Config()
        c.symbol = self.cfg.symbol
        c.interval = self.cfg.timeframe
        c.limit = 400
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

        # Candles before live_start_idx are warmup-only; strategies skip recording
        # trades entered there while still using them for indicator lookback.
        live_start_idx = 0
        if self._live_from_ts is not None:
            live_start_idx = int(self.df.index.searchsorted(self._live_from_ts))

        trades_df = strategy.run(self.df, indicators, live_start_idx)

        self.completed_trades = trades_df.to_dict("records") if not trades_df.empty else []
        self.open_positions   = strategy.open_positions
        self.total_pnl        = sum(float(str(t.get("P&L ($)", 0))) for t in self.completed_trades)
        self.balance          = self.cfg.credit + self.total_pnl
        self.current_price    = float(self.df["Close"].iloc[-1])

    def _validate_and_append(self, new_df: pd.DataFrame) -> bool:
        """Append new candle to df. Returns False if a data gap is detected."""
        if self.df is None or new_df is None or new_df.empty:
            return False

        last_ts = self.df.index[-1].timestamp()
        first_new_ts = new_df.index[0].timestamp()

        # Already have this candle — not a gap, just nothing new yet
        if first_new_ts <= last_ts:
            return True

        candle_secs = RESOLUTION_SECONDS[self.cfg.timeframe]
        expected_next_ts = last_ts + candle_secs

        # More than one full candle of gap → repair needed
        if first_new_ts > expected_next_ts + candle_secs:
            return False

        self.df = pd.concat([self.df, new_df])
        self.df = self.df[~self.df.index.duplicated(keep="last")].sort_index().tail(400)
        return True

    async def start(self) -> None:
        if self.status == BotStatus.RUNNING:
            return
        self._stop_event.clear()
        self.status = BotStatus.RUNNING
        self.error = None
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

    async def _run_loop(self) -> None:
        try:
            # ── Initial full fetch of 250 candles ─────────────────────────
            fetcher = DataFetcher(self._make_config())
            for attempt in range(5):
                try:
                    self.df = await asyncio.to_thread(fetcher.fetch)
                    break
                except Exception as exc:
                    if attempt == 4:
                        raise RuntimeError(f"Initial data fetch failed: {exc}") from exc
                    await asyncio.sleep(2 ** attempt)

            # Mark where live trading begins. _run_strategy is NOT called here —
            # the 250 warmup candles are purely for indicator context; no trades are
            # generated until the first live candle arrives in the loop below.
            _res = RESOLUTION_SECONDS[self.cfg.timeframe]
            self._live_from_ts = self.df.index[-1] + pd.Timedelta(seconds=_res)
            self.current_price = float(self.df["Close"].iloc[-1])
            self.last_updated = datetime.now(timezone.utc)
            self.error = None

            # ── Per-candle live loop ───────────────────────────────────────
            while not self._stop_event.is_set():
                candle_secs = RESOLUTION_SECONDS[self.cfg.timeframe]
                last_ts = self.df.index[-1].timestamp()
                next_close_ts = last_ts + candle_secs
                # Sleep until next candle closes (+5s buffer so the API has it ready)
                sleep_secs = max(5.0, next_close_ts - time.time() + 5)

                # Wait, but bail out immediately if stop is requested
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

                # Fetch the latest closed candle with exponential-backoff retries
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
                    # All retries failed → full re-fetch to stay consistent
                    try:
                        self.df = await asyncio.to_thread(fetcher.fetch)
                        self.error = "Candle fetch failed; did full re-fetch for integrity."
                    except Exception as exc:
                        self.error = f"Full re-fetch failed: {exc}"
                        continue
                else:
                    ok = self._validate_and_append(new_df)
                    if not ok:
                        # Gap detected → repair with full re-fetch
                        try:
                            self.df = await asyncio.to_thread(fetcher.fetch)
                            self.error = "Data gap detected; re-fetched all 250 candles."
                        except Exception as exc:
                            self.error = f"Gap repair failed: {exc}"
                            continue
                    else:
                        self.error = None

                try:
                    self._run_strategy()
                except Exception as exc:
                    self.error = f"Strategy error: {exc}"

                self.last_updated = datetime.now(timezone.utc)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.status = BotStatus.ERROR
            self.error = str(exc)
        finally:
            if self.status == BotStatus.RUNNING:
                self.status = BotStatus.STOPPED

    def to_state_dict(self) -> dict:
        return {
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
            "candle_count": len(self.df) if self.df is not None else 0,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
            "created_at": self.created_at.isoformat(),
            "error": self.error,
        }

    def to_detail_dict(self) -> dict:
        d = self.to_state_dict()
        d["trades"] = self.completed_trades
        return d
