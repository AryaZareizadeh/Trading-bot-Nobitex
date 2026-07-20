"""Multi-combination backtest runner for the dashboard's Tester tab.

Given a set of margin symbols, strategies and timeframes, this runs a 500-candle
backtest on every (symbol × strategy × timeframe) combination and reports the
profitability of each.

Two design choices keep this within Nobitex's per-minute request budget:

1. **Candle data depends only on (symbol, timeframe), never on the strategy.**
   So each unique (symbol, timeframe) is fetched exactly once and the resulting
   DataFrame is reused across every selected strategy.
2. **Fetches are throttled** to at most `_FETCHES_PER_MINUTE` requests/minute.
   Strategy evaluation itself is pure CPU work and makes no API calls.

Runs execute as in-memory background jobs (consistent with the rest of the
dashboard: no DB, state lost on restart) that the frontend polls for progress.
"""

import asyncio
import json
import time
import uuid
from pathlib import Path

import pandas as pd

from bot import STRATEGY_MAP
from config import Config
from data import DataFetcher
from indicators import Indicators

_BACKTEST_CANDLES = 500

# Throttle market-data fetches so a large run cannot blow through Nobitex's
# per-minute limit. Each unique (symbol, timeframe) costs one fetch.
_FETCHES_PER_MINUTE = 30
_MIN_SECONDS_BETWEEN_FETCHES = 60.0 / _FETCHES_PER_MINUTE

# Hard cap on combinations per run, to bound runtime and memory.
_MAX_COMBOS = 240

_PREFS_FILE = Path(__file__).parent / "tester.json"
_MAX_JOBS_KEPT = 20

# Display labels for the strategies registered in bot.STRATEGY_MAP.
STRATEGY_LABELS: dict[str, str] = {
    "AdvRSI": "Advanced RSI Combined",
    "TrendMomentum": "Trend-Filtered Momentum",
    "ExtremeReversal": "Extreme Reversal",
}

# Timeframes offered in the tester (mirrors the create-bot modal).
TIMEFRAMES: list[dict] = [
    {"value": "1", "label": "1 minute"},
    {"value": "5", "label": "5 minutes"},
    {"value": "15", "label": "15 minutes"},
    {"value": "30", "label": "30 minutes"},
    {"value": "60", "label": "1 hour"},
    {"value": "180", "label": "3 hours"},
    {"value": "240", "label": "4 hours"},
    {"value": "D", "label": "1 day"},
]
_VALID_TIMEFRAMES = {tf["value"] for tf in TIMEFRAMES}


# ── Core backtest ───────────────────────────────────────────────────────────────

def _fetch_df(symbol: str, timeframe: str) -> pd.DataFrame:
    """Fetch the last `_BACKTEST_CANDLES` candles for one (symbol, timeframe)."""
    cfg = Config()
    cfg.symbol = symbol
    cfg.interval = timeframe
    cfg.limit = _BACKTEST_CANDLES
    return DataFetcher(cfg).fetch()


# Candidate column names for a trade's direction. "Position" (values "Long" /
# "Short") is what the dashboard frontend actually reads (see index.html's
# trade table), so it's checked first; the rest are kept as fallbacks in case
# a different strategy module labels it differently.
_DIRECTION_KEYS = ("Position", "Direction", "Side", "Type", "Trade Type")


def _trade_direction(record: dict) -> str:
    """Best-effort extraction of a trade's Long/Short label from a trade record."""
    for key in _DIRECTION_KEYS:
        val = record.get(key)
        if val:
            return str(val).strip().lower()
    return ""


_EMPTY_SUMMARY_EXTRA = {
    "long_count": 0,
    "short_count": 0,
    "long_pct": 0.0,
    "sum_profits": 0.0,
    "sum_losses": 0.0,
    "profit_efficiency": None,
    "profit_share": None,
}


def _summarize(trades_df: pd.DataFrame, open_count: int, candle_count: int) -> dict:
    """Reduce a strategy's trade log to a profitability summary.

    Mirrors Bot.run_backtest: the first closed trade is dropped because the
    indicators lack warmup data at the very start of the window.
    """
    if trades_df.empty:
        return {"profitable": None, "total_pnl": 0.0, "trade_count": 0, "win_rate": 0.0,
                "candle_count": candle_count, "price_changes": [], **_EMPTY_SUMMARY_EXTRA}

    closed_count = max(0, len(trades_df) - open_count)
    records = trades_df.to_dict("records")
    valid_records = records[1:closed_count]  # skip the first (no indicator warmup)
    if not valid_records:
        return {"profitable": None, "total_pnl": 0.0, "trade_count": 0, "win_rate": 0.0,
                "candle_count": candle_count, "price_changes": [], **_EMPTY_SUMMARY_EXTRA}

    closed_pnls = [float(str(r.get("P&L ($)", 0))) for r in valid_records]
    total_pnl = sum(closed_pnls)
    wins = sum(1 for p in closed_pnls if p > 0)
    win_rate = round(wins / len(closed_pnls) * 100, 1) if closed_pnls else 0.0

    # Sum of all winning trades and sum of all losing trades separately.
    sum_profits = round(sum(p for p in closed_pnls if p > 0), 2)
    sum_losses = round(sum(p for p in closed_pnls if p < 0), 2)  # negative (or 0 if none)

    # Profit Efficiency: net profit as a % of total absolute P&L churned
    # (profits + |losses|). 100% = every trade was a winner (no churn at all);
    # lower % means losses ate up more of the gross P&L before netting out.
    # Undefined (None) when there's no gross P&L to divide by (e.g. all
    # trades broke exactly even).
    gross_abs_pnl = sum_profits + abs(sum_losses)
    profit_efficiency = round(total_pnl / gross_abs_pnl * 100, 1) if gross_abs_pnl != 0 else None

    # Profit Share: gross winning P&L as a % of total absolute P&L churned
    # (profits + |losses|). Unlike profit_efficiency this uses the gross
    # winnings, not the net result, so it's never pulled down by losses —
    # 100% = no losing trades at all. Undefined (None) with no gross P&L.
    profit_share = round(sum_profits / gross_abs_pnl * 100, 1) if gross_abs_pnl != 0 else None

    # Long/Short ratio, derived from whichever direction column is present.
    long_count = 0
    short_count = 0
    for r in valid_records:
        d = _trade_direction(r)
        if "long" in d or d == "buy":
            long_count += 1
        elif "short" in d or d == "sell":
            short_count += 1
    long_pct = round(long_count / len(valid_records) * 100, 1) if valid_records else 0.0

    price_changes = []
    for r in valid_records:
        try:
            price_changes.append(float(str(r.get("Price Change", "0%")).replace("%", "").replace("+", "")))
        except ValueError:
            price_changes.append(0.0)

    return {
        "profitable": total_pnl > 0,
        "total_pnl": round(total_pnl, 2),
        "trade_count": len(valid_records),
        "win_rate": win_rate,
        "candle_count": candle_count,
        "price_changes": price_changes,
        "long_count": long_count,
        "short_count": short_count,
        "long_pct": long_pct,
        "sum_profits": sum_profits,
        "sum_losses": sum_losses,
        "profit_efficiency": profit_efficiency,
        "profit_share": profit_share,
    }


def run_backtest_on_df(df: pd.DataFrame, strategy_name: str, credit: float, leverage: int) -> dict:
    """Run one strategy over a pre-fetched DataFrame and summarize the result."""
    cfg = Config()
    cfg.leverage = leverage
    cfg.trade_size = credit
    cfg.initial_capital = credit

    indicators = Indicators(df)
    strategy = STRATEGY_MAP[strategy_name](cfg)
    trades_df = strategy.run(df, indicators, live_start_idx=0)
    return _summarize(trades_df, len(strategy.open_positions), len(df))


# ── Background job ───────────────────────────────────────────────────────────────

class TesterJob:
    """A single batch run over a list of (symbol, strategy, timeframe) combos."""

    def __init__(self, combos: list[tuple[str, str, str]], credit: float, leverage: int):
        self.id = uuid.uuid4().hex[:8]
        self.combos = combos
        self.credit = credit
        self.leverage = leverage
        self.status = "pending"          # pending | running | done | cancelled | error
        self.total = len(combos)
        self.completed = 0
        self.current = ""
        self.error: str | None = None
        self.results: list[dict] = []
        self.created_at = time.time()
        self._cancel = False
        self._task: asyncio.Task | None = None

    def cancel(self) -> None:
        self._cancel = True

    async def run(self) -> None:
        self.status = "running"
        df_cache: dict[tuple[str, str], pd.DataFrame] = {}
        last_fetch = 0.0  # time.monotonic() of the last API call

        try:
            for symbol, strategy, timeframe in self.combos:
                if self._cancel:
                    self.status = "cancelled"
                    return

                self.current = f"{symbol.upper()} · {STRATEGY_LABELS.get(strategy, strategy)} · {timeframe}"
                key = (symbol, timeframe)
                df = df_cache.get(key)

                # Fetch candles once per (symbol, timeframe), throttled.
                if df is None:
                    wait = _MIN_SECONDS_BETWEEN_FETCHES - (time.monotonic() - last_fetch)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    try:
                        df = await asyncio.to_thread(_fetch_df, symbol, timeframe)
                    except Exception as exc:
                        last_fetch = time.monotonic()
                        self.results.append(self._error_result(symbol, strategy, timeframe, f"data fetch failed: {exc}"))
                        self.completed += 1
                        continue
                    last_fetch = time.monotonic()
                    if df is None or df.empty or len(df) < 50:
                        df_cache[key] = df if df is not None else pd.DataFrame()
                        self.results.append(self._error_result(symbol, strategy, timeframe, "insufficient candle data"))
                        self.completed += 1
                        continue
                    df_cache[key] = df
                elif df.empty or len(df) < 50:
                    self.results.append(self._error_result(symbol, strategy, timeframe, "insufficient candle data"))
                    self.completed += 1
                    continue

                # Strategy evaluation — pure CPU, no API call.
                try:
                    summary = await asyncio.to_thread(run_backtest_on_df, df, strategy, self.credit, self.leverage)
                    self.results.append({
                        "symbol": symbol.upper(),
                        "strategy": strategy,
                        "strategy_label": STRATEGY_LABELS.get(strategy, strategy),
                        "timeframe": timeframe,
                        "error": None,
                        **summary,
                    })
                except Exception as exc:
                    self.results.append(self._error_result(symbol, strategy, timeframe, f"strategy error: {exc}"))

                self.completed += 1

            self.status = "done"
        except asyncio.CancelledError:
            self.status = "cancelled"
            raise
        except Exception as exc:
            self.status = "error"
            self.error = str(exc)

    @staticmethod
    def _error_result(symbol: str, strategy: str, timeframe: str, msg: str) -> dict:
        return {
            "symbol": symbol.upper(),
            "strategy": strategy,
            "strategy_label": STRATEGY_LABELS.get(strategy, strategy),
            "timeframe": timeframe,
            "error": msg,
            "profitable": None,
            "total_pnl": 0.0,
            "trade_count": 0,
            "win_rate": 0.0,
            "candle_count": 0,
            **_EMPTY_SUMMARY_EXTRA,
        }

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "total": self.total,
            "completed": self.completed,
            "current": self.current,
            "error": self.error,
            "credit": self.credit,
            "leverage": self.leverage,
            "results": self.results,
        }


# ── Manager / registry ───────────────────────────────────────────────────────────

class TesterManager:
    def __init__(self):
        self._jobs: dict[str, TesterJob] = {}
        self._prefs: dict = self._load_prefs()

    # ── Preferences persistence ──────────────────────────────────────────────
    def _load_prefs(self) -> dict:
        defaults = {"favorites": [], "strategies": [], "timeframes": [], "credit": 100.0, "leverage": 5}
        if not _PREFS_FILE.exists():
            return defaults
        try:
            data = json.loads(_PREFS_FILE.read_text(encoding="utf-8"))
            defaults.update({k: data[k] for k in defaults if k in data})
        except Exception:
            pass
        return defaults

    def _save_prefs(self) -> None:
        try:
            _PREFS_FILE.write_text(json.dumps(self._prefs, indent=2), encoding="utf-8")
        except Exception:
            pass

    def save_prefs(self, favorites, strategies, timeframes, credit, leverage) -> None:
        self._prefs = {
            "favorites": [s.lower() for s in favorites],
            "strategies": [s for s in strategies if s in STRATEGY_MAP],
            "timeframes": [t for t in timeframes if t in _VALID_TIMEFRAMES],
            "credit": float(credit),
            "leverage": int(leverage),
        }
        self._save_prefs()

    def get_config(self) -> dict:
        return {
            **self._prefs,
            "available_strategies": [
                {"value": k, "label": STRATEGY_LABELS.get(k, k)} for k in STRATEGY_MAP
            ],
            "available_timeframes": TIMEFRAMES,
            "fetches_per_minute": _FETCHES_PER_MINUTE,
            "candles": _BACKTEST_CANDLES,
            "max_combos": _MAX_COMBOS,
        }

    # ── Jobs ─────────────────────────────────────────────────────────────────
    def build_combos(self, symbols, strategies, timeframes) -> list[tuple[str, str, str]]:
        """Validated cartesian product, ordered so cached fetches are reused back-to-back."""
        syms = [s.lower().replace("/", "").strip() for s in symbols if s.strip()]
        strats = [s for s in strategies if s in STRATEGY_MAP]
        tfs = [t for t in timeframes if t in _VALID_TIMEFRAMES]
        combos = [
            (sym, strat, tf)
            for sym in dict.fromkeys(syms)
            for tf in dict.fromkeys(tfs)
            for strat in dict.fromkeys(strats)
        ]
        return combos

    def start_job(self, combos, credit, leverage) -> TesterJob:
        self._prune()
        job = TesterJob(combos, float(credit), int(leverage))
        self._jobs[job.id] = job
        job._task = asyncio.create_task(job.run())
        return job

    def get_job(self, job_id: str) -> TesterJob | None:
        return self._jobs.get(job_id)

    def _prune(self) -> None:
        """Drop the oldest finished jobs once we exceed the keep limit."""
        if len(self._jobs) < _MAX_JOBS_KEPT:
            return
        finished = sorted(
            (j for j in self._jobs.values() if j.status in ("done", "cancelled", "error")),
            key=lambda j: j.created_at,
        )
        while len(self._jobs) >= _MAX_JOBS_KEPT and finished:
            self._jobs.pop(finished.pop(0).id, None)


tester_manager = TesterManager()