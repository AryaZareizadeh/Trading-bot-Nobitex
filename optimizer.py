"""Indicator-input optimizer for the dashboard's Optimization tab.

Given a single (symbol, timeframe, candle-count, strategy), this searches the
strategy's indicator inputs for the set that maximizes backtested net P&L.

**Why Differential Evolution?**
The objective — net P&L of a backtest as a function of the indicator inputs — is
non-convex, noisy and non-differentiable, and every input is an integer-bounded
knob (RSI length, StochRSI smoothing, oversold/overbought gates, MACD periods…).
That rules out gradient methods and makes exhaustive grid search explode
combinatorially across 8–12 parameters. Differential Evolution is a population
based *global* optimizer designed for exactly this regime: it tolerates noise,
escapes local optima far better than hill-climbing, and `scipy` supports an
``integrality`` mask so it searches the integer lattice directly. Because the
candle data depends only on (symbol, timeframe) it is fetched **once** and every
DE evaluation is then pure CPU — so we can afford the hundreds–thousands of
backtests DE needs.

**Parallelism.** Each backtest is pure, GIL-bound Python/NumPy work, so threads
cannot run them simultaneously. Instead we fan each DE *generation* out across a
pool of worker processes — one per CPU core — so a 16-core machine runs ~16
backtests at once. SciPy drives this for us when handed ``workers=pool.map`` plus
``updating="deferred"``. See ``OptimizerJob._run_de_parallel``.

Runs execute as in-memory background jobs (consistent with the rest of the
dashboard: no DB, state lost on restart) that the frontend polls for progress.
"""

import asyncio
import json
import multiprocessing as mp
import os
import sys
import time
import uuid
from dataclasses import fields
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.optimize import differential_evolution

from backtester import STRATEGY_LABELS, TIMEFRAMES, _VALID_TIMEFRAMES, _summarize
from bot import STRATEGY_MAP
from config import Config
from data import DataFetcher
from indicators import Indicators
from strategy import AdvRsiParams, StochRsiExtremeParams, TrendMomentumParams, PARAMS_CLASS

exchange = "binance"  # default exchange for optimizer; can be overridden in Config

# ── Search-space definition ──────────────────────────────────────────────────────
# Each strategy's tunable indicator inputs and their integer search bounds
# (inclusive). Every field of the strategy's params dataclass is optimized; the
# default value is read from the dataclass so the UI can show "default → tuned".

# param_name -> (low, high). Bounds are inclusive integer ranges.
PARAM_BOUNDS: dict[str, dict[str, tuple[int, int]]] = {
    "AdvRSI": {
        "rsi_length": (6, 50),
        "stoch_length": (6, 50),
        "k_smooth": (4, 20),
        "d_smooth": (4, 20),
        "rsi_fast_length": (6, 50),
        "rsi_ma_length": (2, 25),
        "oversold": (1, 35),
        "overbought": (65, 99),
        "exit_oversold": (1, 40),
        "exit_overbought": (60, 99),
        "entry_wait_candles": (0, 20),
    },
    "ExtremeReversal": {
        "rsi_length": (2, 100),
        "stoch_length": (2, 100),
        "k_smooth": (1, 30),
        "d_smooth": (1, 30),
        "rsi_fast_length": (2, 50),
        "rsi_ma_length": (2, 50),
        "oversold": (1, 50),
        "overbought": (50, 99),
        "long_kd_max": (1, 50),
        "long_rsi_max": (5, 60),
        "short_kd_min": (50, 99),
        "short_rsi_min": (40, 99),
    },
    "TrendMomentum": {
        "rsi_length": (2, 100),
        "stoch_length": (2, 100),
        "k_smooth": (1, 30),
        "d_smooth": (1, 30),
        "ema_trend_length": (10, 200),
        "macd_fast": (2, 20),
        "macd_slow": (21, 100),
        "macd_signal": (2, 30),
        "oversold": (1, 50),
        "overbought": (50, 99),
    },
}

# Candle-count limits for the fetch.
_MIN_CANDLES = 200
_MAX_CANDLES = 30000
_DEFAULT_CANDLES = 1000

# Differential-Evolution budget. Total backtests ≈ popsize · n_params · (maxiter+1).
_DE_POPSIZE = 15
_DE_MAXITER = 30
_DE_TOL = 0.01
_DE_SEED = None

# Cores used when the user leaves the selector on "Auto" (capped to the machine's
# CPU count, and overridable with the OPTIMIZER_WORKERS env var).
_DEFAULT_WORKERS = 14

_PREFS_FILE = Path(__file__).parent / "optimizer.json"
_MAX_JOBS_KEPT = 20


def _defaults_for(strategy_name: str) -> dict:
    """Default value of every optimized field, read from the params dataclass."""
    cls = PARAMS_CLASS[strategy_name]
    inst = cls()
    return {f.name: getattr(inst, f.name) for f in fields(cls) if f.name in PARAM_BOUNDS[strategy_name]}


# ── Objective ────────────────────────────────────────────────────────────────────

def _repair(strategy_name: str, kwargs: dict) -> dict:
    """Enforce inter-parameter constraints DE's box bounds cannot express."""
    # MACD requires a fast period strictly below the slow period.
    if "macd_fast" in kwargs and "macd_slow" in kwargs:
        if kwargs["macd_slow"] <= kwargs["macd_fast"]:
            kwargs["macd_slow"] = kwargs["macd_fast"] + 1
    # oversold must sit below overbought (ranges are disjoint, but guard anyway).
    if "oversold" in kwargs and "overbought" in kwargs:
        if kwargs["oversold"] >= kwargs["overbought"]:
            kwargs["oversold"], kwargs["overbought"] = kwargs["overbought"], kwargs["oversold"]
    return kwargs


def _build_params(strategy_name: str, names: list[str], vector) -> object:
    """Turn a DE solution vector into the strategy's params dataclass."""
    kwargs = {name: int(round(float(v))) for name, v in zip(names, vector)}
    kwargs = _repair(strategy_name, kwargs)
    return PARAMS_CLASS[strategy_name](**kwargs)


def _compute_score(summary: dict, objective: str, credit: float, min_price_change: float = 5.0) -> float:
    """Return the value DE minimizes for the given objective (lower = better)."""
    pnl = summary.get("total_pnl", 0.0)
    wr = summary.get("win_rate", 0.0)
    if objective == "winrate":
        # Maximize win rate; penalize zero-trade solutions.
        tc = summary.get("trade_count", 0)
        return -wr if tc > 0 else 100.0
    if objective == "balanced":
        # Maximize both: normalize pnl to percentage-of-credit, then sum with win rate.
        pnl_pct = (pnl / credit * 100) if credit > 0 else 0.0
        return -(pnl_pct + wr)
    if objective == "large_move":
        # Maximize the % of trades where |price change| >= threshold.
        changes = summary.get("price_changes", [])
        tc = len(changes)
        if tc == 0:
            return 100.0
        return -(sum(1 for c in changes if abs(c) >= min_price_change) / tc * 100)
    # Default: maximize profit.
    return -pnl


def evaluate(
    df: pd.DataFrame,
    strategy_name: str,
    params,
    credit: float,
    leverage: int,
    warmup_df: pd.DataFrame | None = None,
) -> dict:
    """Backtest one parameter set over a pre-fetched DataFrame; return its summary.

    If *warmup_df* is supplied (e.g. the training split), it is prepended to *df*
    so that indicators have a full history at the first evaluation candle.  Only
    trades that start within *df* are counted; candle_count reflects len(df) only.
    """
    cfg = Config()
    cfg.exchange = exchange
    cfg.leverage = leverage
    cfg.trade_size = credit
    cfg.initial_capital = credit

    if warmup_df is not None:
        combined_df = pd.concat([warmup_df, df])
        indicators = Indicators(combined_df)
        start_idx = len(warmup_df)
    else:
        combined_df = df
        indicators = Indicators(df)
        start_idx = 0

    strategy = STRATEGY_MAP[strategy_name](cfg, params)
    trades_df = strategy.run(combined_df, indicators, live_start_idx=start_idx)
    return _summarize(trades_df, len(strategy.open_positions), len(df))


def _fetch_df(symbol: str, timeframe: str, candles: int) -> pd.DataFrame:
    cfg = Config()
    cfg.exchange = exchange
    cfg.symbol = symbol
    cfg.interval = timeframe
    cfg.limit = candles
    return DataFetcher(cfg).fetch()


# ── Parallel evaluation across CPU cores ─────────────────────────────────────────
# A DE generation is a batch of independent backtests. We evaluate them in a pool
# of worker processes (sidestepping the GIL that would serialize threads). The
# candle DataFrame and the run's fixed context (strategy, credit, leverage) are the
# same for every backtest, so we ship them to each worker ONCE via the pool
# initializer and cache them in this per-process global — rather than re-pickling
# the DataFrame alongside each of the thousands of candidate vectors.
_WORKER: dict = {}


def _system_cpu_count() -> int:
    """Total logical processors on the hardware, regardless of process affinity.

    Python 3.13 changed os.cpu_count() to return the affinity-limited count.
    When a process is pinned to a CPU subset (e.g. only E-cores on an Intel hybrid
    chip), that causes the worker cap to silently freeze at the affinity count.
    This function always returns the hardware total so user-selected core counts
    are not silently truncated.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            # ALL_PROCESSOR_GROUPS = 0xFFFF: total across every processor group
            n = ctypes.windll.kernel32.GetMaximumProcessorCount(0xFFFF)
            if n > 0:
                return n
        except Exception:
            pass
    return os.cpu_count() or 1


def _expand_cpu_affinity() -> None:
    """Expand this worker's CPU affinity and priority for full P+E core use.

    Spawned worker processes inherit the parent's (possibly restricted) affinity
    mask and default to background priority. On Intel hybrid CPUs (12th-gen+),
    both of those facts conspire to pin workers to E-cores only:
    - restricted affinity → only E-cores in the mask
    - background priority → Intel Thread Director routes remaining work to E-cores
    This function runs once per worker (via _worker_init) to undo both effects.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            proc = kernel32.GetCurrentProcess()
            # 1. Expand affinity to the full system mask (all P+E cores)
            proc_mask = ctypes.c_size_t()
            sys_mask = ctypes.c_size_t()
            if kernel32.GetProcessAffinityMask(proc, ctypes.byref(proc_mask),
                                               ctypes.byref(sys_mask)):
                kernel32.SetProcessAffinityMask(proc, sys_mask)
            # 2. Raise priority so Intel Thread Director routes to P-cores
            ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
            kernel32.SetPriorityClass(proc, ABOVE_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass
    else:
        try:
            os.sched_setaffinity(0, range(_system_cpu_count()))
        except (AttributeError, OSError):
            pass


def _worker_init(df, strategy_name, credit, leverage, names, objective, min_price_change) -> None:
    """Pool initializer: runs once per worker process, caching the shared context."""
    _expand_cpu_affinity()
    _WORKER.update(df=df, strategy=strategy_name, credit=credit,
                   leverage=leverage, names=names, objective=objective,
                   min_price_change=min_price_change)


def _worker_objective(vector) -> float:
    """Backtest one DE candidate inside a worker; return the value DE minimizes.

    Must be a module-level function (not a closure) so it can be pickled and sent
    to the worker processes. It reads everything but the candidate vector from the
    per-process ``_WORKER`` cache populated by ``_worker_init``.
    """
    params = _build_params(_WORKER["strategy"], _WORKER["names"], vector)
    summary = evaluate(_WORKER["df"], _WORKER["strategy"], params,
                       _WORKER["credit"], _WORKER["leverage"])
    return _compute_score(summary, _WORKER["objective"], _WORKER["credit"], _WORKER["min_price_change"])


def _resolve_workers() -> int:
    """Number of worker processes to use for the DE search.

    Defaults to ``_DEFAULT_WORKERS`` cores (capped to the machine's CPU count).
    Override with the ``OPTIMIZER_WORKERS`` env var: a positive integer caps the
    count, ``-1`` means use every core.
    """
    cpu = _system_cpu_count()
    raw = os.getenv("OPTIMIZER_WORKERS", "").strip()
    if raw:
        try:
            n = int(raw)
        except ValueError:
            n = 0
        if n == -1:
            return cpu
        if n >= 1:
            return min(n, cpu)
    return min(_DEFAULT_WORKERS, cpu)


# ── Background job ───────────────────────────────────────────────────────────────

class OptimizerJob:
    """A single Differential-Evolution search over one strategy's inputs."""

    def __init__(self, symbol: str, strategy: str, timeframe: str, candles: int,
                 credit: float, leverage: int, cores: int = 0,
                 maxiter: int = _DE_MAXITER,
                 custom_bounds: dict | None = None,
                 objective: str = "profit",
                 min_price_change: float = 5.0,
                 train_pct: float | None = None):
        self.id = uuid.uuid4().hex[:8]
        self.symbol = symbol
        self.strategy = strategy
        self.timeframe = timeframe
        self.candles = candles
        self.credit = credit
        self.leverage = leverage
        self.requested_cores = cores     # 0 = Auto; else the user-picked core count
        self.objective = objective
        self.min_price_change = float(min_price_change) if min_price_change else 5.0
        self.custom_bounds = custom_bounds or {}

        self.status = "pending"          # pending | running | done | cancelled | error
        self.error: str | None = None
        self.created_at = time.time()

        self.maxiter = max(5, int(maxiter)) if maxiter else _DE_MAXITER
        self.workers = 1                 # CPU cores used by the search (set when DE starts)
        self.iteration = 0
        self.evaluations = 0
        self.current_delta: float | None = None  # DE population convergence metric (stops when <= _DE_TOL)
        self.best_pnl: float | None = None
        self.best_params: dict | None = None
        self.best_summary: dict | None = None
        self.baseline: dict | None = None      # result with the strategy's default inputs
        self.candle_count = 0
        self.history: list[dict] = []    # per-generation best snapshots
        self.best_trades: list[dict] | None = None  # trade log from the best run
        self.chart_data: dict | None = None          # OHLCV + indicator series for the chart

        # Tester-mode fields (only populated when train_pct is set)
        self.train_pct = train_pct
        self.train_candle_count = 0
        self.test_candle_count = 0
        self.test_summary: dict | None = None
        self.test_baseline: dict | None = None
        self.test_trades: list[dict] | None = None
        self.test_chart_data: dict | None = None

        self._cancel = False
        self._task: asyncio.Task | None = None

    def cancel(self) -> None:
        self._cancel = True

    @property
    def _effective_bounds(self) -> dict[str, tuple[int, int]]:
        """Merge default PARAM_BOUNDS with any user-supplied custom ranges."""
        bounds = dict(PARAM_BOUNDS[self.strategy])
        for name, val in self.custom_bounds.items():
            if name in bounds and isinstance(val, (list, tuple)) and len(val) == 2:
                param_lo = PARAM_BOUNDS[self.strategy][name][0]
                lo = max(param_lo, int(val[0]))   # never go below the safe TA-Lib minimum
                hi = max(lo + 1, int(val[1]))
                bounds[name] = (lo, hi)
        return bounds

    # ── DE plumbing ──────────────────────────────────────────────────────────
    def _de_kwargs(self, bounds, integrality, callback) -> dict:
        """The DE settings shared by the serial and parallel search paths."""
        return dict(
            bounds=bounds,
            integrality=integrality,
            popsize=_DE_POPSIZE,
            maxiter=self.maxiter,
            tol=_DE_TOL,
            rng=_DE_SEED,
            polish=False,        # L-BFGS polish ignores integrality; skip it
            mutation=(0.5, 1.0),
            recombination=0.7,
            callback=callback,
            init="sobol",        # space-filling start for better global coverage
        )

    def _run_de(self, df: pd.DataFrame) -> None:
        """Pick a search path: parallel across cores, else single-core."""
        # A specific core count picked in the UI wins; 0 means "Auto" (defer to the
        # CPU-count / OPTIMIZER_WORKERS default).
        if self.requested_cores and self.requested_cores >= 1:
            self.workers = min(self.requested_cores, _system_cpu_count())
        else:
            self.workers = _resolve_workers()
        if self.workers <= 1:
            self._run_de_serial(df)
            return
        try:
            self._run_de_parallel(df, self.workers)
        except Exception:
            # Multiprocessing can fail to start in locked-down environments; never
            # let that abort a run — fall back to the single-core path.
            self.workers = 1
            self.iteration = 0
            self.evaluations = 0
            self._run_de_serial(df)

    def _run_de_serial(self, df: pd.DataFrame) -> None:
        """Single-core search: one backtest at a time in this process."""
        eff = self._effective_bounds
        names = list(eff.keys())
        bounds = [eff[n] for n in names]
        integrality = np.ones(len(bounds), dtype=bool)

        best = {"score": np.inf, "pnl": -np.inf, "params": None, "summary": None}

        def objective(vector) -> float:
            params = _build_params(self.strategy, names, vector)
            summary = evaluate(df, self.strategy, params, self.credit, self.leverage)
            score = _compute_score(summary, self.objective, self.credit, self.min_price_change)
            self.evaluations += 1
            if score < best["score"]:
                best["score"] = score
                best["pnl"] = summary["total_pnl"]
                best["params"] = {f.name: getattr(params, f.name) for f in fields(params)}
                best["summary"] = summary
            return score

        def callback(intermediate_result=None, convergence=None):
            self.iteration += 1
            # SciPy passes `convergence` = tol / (raw_convergence + eps), a ratio that
            # hits 1.0 at the stopping point. Invert it back to the raw population
            # convergence metric so the UI can show it against _DE_TOL directly.
            if convergence:
                self.current_delta = _DE_TOL / convergence
            self.best_pnl = best["pnl"] if best["params"] is not None else None
            self.best_params = best["params"]
            self.best_summary = best["summary"]
            if best["params"] is not None:
                self.history.append({
                    "iteration": self.iteration,
                    "pnl": best["pnl"],
                    "win_rate": best["summary"].get("win_rate", 0) if best["summary"] else 0,
                    "trade_count": best["summary"].get("trade_count", 0) if best["summary"] else 0,
                    "delta": self.current_delta,
                })
            if self._cancel:
                return True   # signal DE to stop
            return False

        differential_evolution(objective, **self._de_kwargs(bounds, integrality, callback))

        # Final best (in case the last improvement landed after the last callback).
        self.best_pnl = best["pnl"] if best["params"] is not None else None
        self.best_params = best["params"]
        self.best_summary = best["summary"]

    def _run_de_parallel(self, df: pd.DataFrame, workers: int) -> None:
        """Multi-core search: each generation's backtests run in a worker pool.

        ``updating="deferred"`` is mandatory here — it makes DE evaluate a whole
        generation before advancing, which is what lets the generation be split
        across processes. Because the workers run in separate address spaces they
        can't update ``best``/``evaluations`` directly, so the parent reconstructs
        the running best from SciPy's per-generation callback instead.
        """
        eff = self._effective_bounds
        names = list(eff.keys())
        bounds = [eff[n] for n in names]
        integrality = np.ones(len(bounds), dtype=bool)
        # SciPy's population size for this many parameters; one generation runs this
        # many backtests, which lets us report an approximate running total.
        pop_per_gen = max(5, _DE_POPSIZE * len(bounds))

        best = {"score": None, "pnl": None, "params": None, "summary": None}

        def record(vector) -> None:
            """Re-score one DE vector in the parent and keep it if it's the best."""
            params = _build_params(self.strategy, names, vector)
            summary = evaluate(df, self.strategy, params, self.credit, self.leverage)
            score = _compute_score(summary, self.objective, self.credit, self.min_price_change)
            if best["score"] is None or score < best["score"]:
                best["score"] = score
                best["pnl"] = summary["total_pnl"]
                best["params"] = {f.name: getattr(params, f.name) for f in fields(params)}
                best["summary"] = summary

        def callback(intermediate_result=None, convergence=None):
            self.iteration += 1
            # SciPy's DE calls this with the best-so-far *vector* (an ndarray) and a
            # `convergence` value — the worker processes can't reach `best`, so the
            # parent re-scores that vector here (one cheap backtest per generation)
            # to recover the full summary for the live display. `getattr(.,"x",.)`
            # also tolerates newer SciPy passing an OptimizeResult instead.
            if intermediate_result is not None:
                record(getattr(intermediate_result, "x", intermediate_result))
            # Same inversion as the serial path: turn SciPy's stopping ratio back
            # into the raw population convergence metric, compared against _DE_TOL.
            if convergence:
                self.current_delta = _DE_TOL / convergence
            self.evaluations = pop_per_gen * (self.iteration + 1)   # approximate
            self.best_pnl = best["pnl"]
            self.best_params = best["params"]
            self.best_summary = best["summary"]
            if best["pnl"] is not None:
                self.history.append({
                    "iteration": self.iteration,
                    "pnl": best["pnl"],
                    "win_rate": best["summary"].get("win_rate", 0) if best["summary"] else 0,
                    "trade_count": best["summary"].get("trade_count", 0) if best["summary"] else 0,
                    "delta": self.current_delta,
                })
            if self._cancel:
                return True   # signal DE to stop (takes effect after this generation)
            return False

        # "spawn" is the only start method on Windows and avoids fork-safety issues
        # elsewhere; the candle data + context are handed to each worker just once.
        ctx = mp.get_context("spawn")
        pool = ctx.Pool(
            processes=workers,
            initializer=_worker_init,
            initargs=(df, self.strategy, self.credit, self.leverage, names, self.objective, self.min_price_change),
        )
        try:
            result = differential_evolution(
                _worker_objective,
                updating="deferred",     # required to evaluate a generation in parallel
                workers=pool.map,        # SciPy maps each generation over the pool
                **self._de_kwargs(bounds, integrality, callback),
            )
        finally:
            pool.terminate()
            pool.join()

        # Reconcile with DE's own final answer in case the last improvement landed
        # after the final callback fired.
        if getattr(result, "x", None) is not None:
            record(result.x)
        self.best_pnl = best["pnl"]
        self.best_params = best["params"]
        self.best_summary = best["summary"]

    async def run(self) -> None:
        self.status = "running"
        try:
            df = await asyncio.to_thread(_fetch_df, self.symbol, self.timeframe, self.candles)
            if df is None or df.empty or len(df) < 50:
                self.status = "error"
                self.error = "insufficient candle data"
                return
            self.candle_count = len(df)

            # In tester mode, split df into train (for DE) and test (for evaluation).
            train_df = df
            test_df: pd.DataFrame | None = None
            if self.train_pct is not None:
                train_n = max(1, int(len(df) * self.train_pct / 100))
                if train_n < _MIN_CANDLES:
                    self.status = "error"
                    self.error = (
                        f"Training set too small ({train_n} candles < {_MIN_CANDLES} minimum). "
                        "Increase candle count or lower the train %."
                    )
                    return
                test_n = len(df) - train_n
                if test_n < 50:
                    self.status = "error"
                    self.error = (
                        f"Test set too small ({test_n} candles). "
                        "Reduce train % or increase candle count."
                    )
                    return
                train_df = df.iloc[:train_n].copy()
                test_df = df.iloc[train_n:].copy()
                self.train_candle_count = len(train_df)
                self.test_candle_count = len(test_df)

            # Baseline: the strategy's shipped default inputs, for comparison.
            self.baseline = await asyncio.to_thread(
                evaluate, train_df, self.strategy, PARAMS_CLASS[self.strategy](),
                self.credit, self.leverage,
            )

            await asyncio.to_thread(self._run_de, train_df)

            if not self._cancel and self.best_params:
                await asyncio.to_thread(self._compute_best_trades, train_df)
                if test_df is not None:
                    await asyncio.to_thread(self._compute_test_results, df, train_n)

            if self._cancel:
                self.status = "cancelled"
            else:
                self.status = "done"
        except asyncio.CancelledError:
            self.status = "cancelled"
            raise
        except Exception as exc:
            self.status = "error"
            self.error = str(exc)

    def _compute_best_trades(self, df: pd.DataFrame) -> None:
        """Re-run the strategy once with best_params to capture the full trade log."""
        if not self.best_params:
            return
        cfg = Config()
        cfg.exchange = exchange
        cfg.leverage = self.leverage
        cfg.trade_size = self.credit
        cfg.initial_capital = self.credit
        indicators = Indicators(df)
        params = PARAMS_CLASS[self.strategy](**self.best_params)
        strategy = STRATEGY_MAP[self.strategy](cfg, params)
        trades_df = strategy.run(df, indicators, live_start_idx=0)
        records = trades_df.to_dict("records")
        open_count = len(strategy.open_positions)
        closed_count = max(0, len(records) - open_count)
        if closed_count > 1:
            offset = float(records[0]["Balance ($)"])
            adjusted = []
            for r in records[1:closed_count]:
                r = dict(r)
                r["Balance ($)"] = f"{float(r['Balance ($)']) - offset:.2f}"
                adjusted.append(r)
            self.best_trades = adjusted
        else:
            self.best_trades = []

        # ── Chart data ───────────────────────────────────────────────────────
        _tehran = ZoneInfo("Asia/Tehran")

        def _to_series(s):
            return [None if pd.isna(v) else round(float(v), 6) for v in s]

        # Build Tehran-time → Unix-UTC lookup so trade markers can be placed
        time_map: dict[str, int] = {}
        candles: list[dict] = []
        for ts, row in df.iterrows():
            ts_utc = ts.tz_localize("UTC") if ts.tzinfo is None else ts
            unix = int(ts_utc.timestamp())
            fmt = ts_utc.tz_convert(_tehran).strftime("%Y-%m-%d %H:%M:%S")
            candles.append({
                "time": unix,
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
            })
            time_map[fmt] = unix

        # Annotate each trade record with unix timestamps for chart markers
        for r in (self.best_trades or []):
            r["_start_ts"] = time_map.get(r.get("Start Time", ""))
            r["_end_ts"] = time_map.get(r.get("End Time", ""))

        # Per-strategy indicator series
        ind: dict = {}
        if self.strategy in ("AdvRSI", "ExtremeReversal"):
            srsi = indicators.stoch_rsi(
                params.rsi_length, params.stoch_length,
                params.k_smooth, params.d_smooth,
            )
            rsi, rsi_ma = indicators.rsi_with_ma(
                params.rsi_fast_length, params.rsi_ma_length,
            )
            ind = {
                "stoch_k":   _to_series(srsi.k),
                "stoch_d":   _to_series(srsi.d),
                "rsi":       _to_series(rsi),
                "rsi_ma":    _to_series(rsi_ma),
                "oversold":  params.oversold,
                "overbought": params.overbought,
            }
        elif self.strategy == "TrendMomentum":
            srsi = indicators.stoch_rsi(
                params.rsi_length, params.stoch_length,
                params.k_smooth, params.d_smooth,
            )
            ema  = indicators.ema(params.ema_trend_length)
            macd = indicators.macd(params.macd_fast, params.macd_slow, params.macd_signal)
            ind = {
                "stoch_k":    _to_series(srsi.k),
                "stoch_d":    _to_series(srsi.d),
                "ema":        _to_series(ema),
                "macd_hist":  _to_series(macd.hist),
                "oversold":   params.oversold,
                "overbought": params.overbought,
            }

        self.chart_data = {"candles": candles, "indicators": ind, "strategy": self.strategy}

    def _compute_test_results(self, full_df: pd.DataFrame, train_n: int) -> None:
        """Evaluate best and default params on the held-out test set.

        The training candles (full_df[:train_n]) are used as indicator warmup so
        that RSI/StochRSI/EMA values are properly initialised at the first test
        candle.  Only trades that start at or after index train_n are counted.
        """
        if not self.best_params:
            return
        train_df = full_df.iloc[:train_n]
        test_df  = full_df.iloc[train_n:].copy()

        best_params_obj = PARAMS_CLASS[self.strategy](**self.best_params)

        # Summary metrics — each evaluate() call prepends train_df for warmup
        self.test_summary  = evaluate(test_df, self.strategy, best_params_obj,
                                      self.credit, self.leverage, warmup_df=train_df)
        self.test_baseline = evaluate(test_df, self.strategy, PARAMS_CLASS[self.strategy](),
                                      self.credit, self.leverage, warmup_df=train_df)

        # Trade log — run strategy over full_df so indicators have warmup history
        cfg = Config()
        cfg.exchange = exchange
        cfg.leverage = self.leverage
        cfg.trade_size = self.credit
        cfg.initial_capital = self.credit
        indicators = Indicators(full_df)
        strategy = STRATEGY_MAP[self.strategy](cfg, best_params_obj)
        trades_df = strategy.run(full_df, indicators, live_start_idx=train_n)
        records = trades_df.to_dict("records")
        open_count = len(strategy.open_positions)
        closed_count = max(0, len(records) - open_count)
        if closed_count > 1:
            offset = float(records[0]["Balance ($)"])
            adjusted = []
            for r in records[1:closed_count]:
                r = dict(r)
                r["Balance ($)"] = f"{float(r['Balance ($)']) - offset:.2f}"
                adjusted.append(r)
            self.test_trades = adjusted
        else:
            self.test_trades = []

        # Chart data for the test period (same structure as self.chart_data)
        _tehran = ZoneInfo("Asia/Tehran")

        def _to_series(s):
            return [None if pd.isna(v) else round(float(v), 6) for v in s]

        time_map: dict[str, int] = {}
        candles: list[dict] = []
        for ts, row in test_df.iterrows():
            ts_utc = ts.tz_localize("UTC") if ts.tzinfo is None else ts
            unix = int(ts_utc.timestamp())
            fmt = ts_utc.tz_convert(_tehran).strftime("%Y-%m-%d %H:%M:%S")
            candles.append({
                "time": unix,
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
            })
            time_map[fmt] = unix

        for r in (self.test_trades or []):
            r["_start_ts"] = time_map.get(r.get("Start Time", ""))
            r["_end_ts"] = time_map.get(r.get("End Time", ""))

        # Indicator series computed on full_df, sliced to the test window so the
        # chart values also benefit from the training-set warmup history.
        ind: dict = {}
        if self.strategy in ("AdvRSI", "ExtremeReversal"):
            srsi = indicators.stoch_rsi(
                best_params_obj.rsi_length, best_params_obj.stoch_length,
                best_params_obj.k_smooth, best_params_obj.d_smooth,
            )
            rsi, rsi_ma = indicators.rsi_with_ma(
                best_params_obj.rsi_fast_length, best_params_obj.rsi_ma_length,
            )
            ind = {
                "stoch_k":    _to_series(srsi.k.iloc[train_n:]),
                "stoch_d":    _to_series(srsi.d.iloc[train_n:]),
                "rsi":        _to_series(rsi.iloc[train_n:]),
                "rsi_ma":     _to_series(rsi_ma.iloc[train_n:]),
                "oversold":   best_params_obj.oversold,
                "overbought": best_params_obj.overbought,
            }
        elif self.strategy == "TrendMomentum":
            srsi = indicators.stoch_rsi(
                best_params_obj.rsi_length, best_params_obj.stoch_length,
                best_params_obj.k_smooth, best_params_obj.d_smooth,
            )
            ema = indicators.ema(best_params_obj.ema_trend_length)
            macd = indicators.macd(best_params_obj.macd_fast, best_params_obj.macd_slow,
                                   best_params_obj.macd_signal)
            ind = {
                "stoch_k":    _to_series(srsi.k.iloc[train_n:]),
                "stoch_d":    _to_series(srsi.d.iloc[train_n:]),
                "ema":        _to_series(ema.iloc[train_n:]),
                "macd_hist":  _to_series(macd.hist.iloc[train_n:]),
                "oversold":   best_params_obj.oversold,
                "overbought": best_params_obj.overbought,
            }

        self.test_chart_data = {"candles": candles, "indicators": ind, "strategy": self.strategy}

    def _params_view(self) -> list[dict]:
        """Best params paired with their defaults and bounds, for display."""
        if not self.best_params:
            return []
        defaults = _defaults_for(self.strategy)
        eff = self._effective_bounds
        out = []
        for name in eff:
            out.append({
                "name": name,
                "default": defaults.get(name),
                "value": self.best_params.get(name),
                "low": eff[name][0],
                "high": eff[name][1],
            })
        return out

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "error": self.error,
            "symbol": self.symbol.upper(),
            "strategy": self.strategy,
            "strategy_label": STRATEGY_LABELS.get(self.strategy, self.strategy),
            "timeframe": self.timeframe,
            "candles": self.candles,
            "candle_count": self.candle_count,
            "credit": self.credit,
            "leverage": self.leverage,
            "objective": self.objective,
            "iteration": self.iteration,
            "maxiter": self.maxiter,
            "workers": self.workers,
            "evaluations": self.evaluations,
            "current_delta": self.current_delta,
            "delta_tol": _DE_TOL,
            "best_pnl": self.best_pnl,
            "best_params": self.best_params,
            "best_summary": self.best_summary,
            "best_trades": self.best_trades,
            "chart_data": self.chart_data,
            "params": self._params_view(),
            "baseline": self.baseline,
            "history": self.history[-60:],   # cap to last 60 generations
            # Tester-mode fields
            "train_pct": self.train_pct,
            "train_candle_count": self.train_candle_count,
            "test_candle_count": self.test_candle_count,
            "test_summary": self.test_summary,
            "test_baseline": self.test_baseline,
            "test_trades": self.test_trades,
            "test_chart_data": self.test_chart_data,
        }


# ── Manager / registry ───────────────────────────────────────────────────────────

class OptimizerManager:
    def __init__(self):
        self._jobs: dict[str, OptimizerJob] = {}
        self._prefs: dict = self._load_prefs()

    # ── Preferences persistence ──────────────────────────────────────────────
    def _load_prefs(self) -> dict:
        defaults = {
            "symbol": "", "strategy": "", "timeframe": "240",
            "candles": _DEFAULT_CANDLES, "credit": 100.0, "leverage": 5,
            "min_price_change": 5.0,
            "cores": 0,       # 0 = Auto
            "maxiter": _DE_MAXITER,
            "objective": "profit",
            "custom_bounds": None,
        }
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

    def save_prefs(self, symbol, strategy, timeframe, candles, credit, leverage,
                   cores=0, maxiter=_DE_MAXITER, objective="profit",
                   custom_bounds=None, min_price_change=5.0) -> None:
        self._prefs = {
            "symbol": (symbol or "").lower().replace("/", "").strip(),
            "strategy": strategy if strategy in STRATEGY_MAP else "",
            "timeframe": timeframe if timeframe in _VALID_TIMEFRAMES else "240",
            "candles": _clamp_candles(candles),
            "credit": float(credit),
            "leverage": int(leverage),
            "cores": _clamp_cores(cores),
            "maxiter": max(5, min(500, int(maxiter))) if maxiter else _DE_MAXITER,
            "objective": objective if objective in ("profit", "winrate", "balanced", "large_move") else "profit",
            "custom_bounds": custom_bounds if isinstance(custom_bounds, dict) else None,
            "min_price_change": max(0.1, float(min_price_change)) if min_price_change else 5.0,
        }
        self._save_prefs()

    def get_config(self) -> dict:
        return {
            **self._prefs,
            "available_strategies": [
                {
                    "value": k,
                    "label": STRATEGY_LABELS.get(k, k),
                    "params": [
                        {"name": n, "low": lo, "high": hi, "default": _defaults_for(k).get(n)}
                        for n, (lo, hi) in PARAM_BOUNDS[k].items()
                    ],
                }
                for k in STRATEGY_MAP if k in PARAM_BOUNDS
            ],
            "available_timeframes": TIMEFRAMES,
            "min_candles": _MIN_CANDLES,
            "max_candles": _MAX_CANDLES,
            "default_candles": _DEFAULT_CANDLES,
            "max_cores": _system_cpu_count(),          # upper bound for the cores selector
            "default_cores": _resolve_workers(),     # what "Auto" resolves to right now
            "default_maxiter": _DE_MAXITER,
        }

    # ── Jobs ─────────────────────────────────────────────────────────────────
    def start_job(self, symbol, strategy, timeframe, candles, credit, leverage,
                  cores=0, maxiter=_DE_MAXITER, objective="profit",
                  custom_bounds=None, min_price_change=5.0, train_pct=None) -> OptimizerJob:
        self._prune()
        job = OptimizerJob(
            symbol.lower().replace("/", "").strip(), strategy, timeframe,
            _clamp_candles(candles), float(credit), int(leverage), _clamp_cores(cores),
            maxiter=maxiter, custom_bounds=custom_bounds, objective=objective,
            min_price_change=min_price_change, train_pct=train_pct,
        )
        self._jobs[job.id] = job
        job._task = asyncio.create_task(job.run())
        return job

    def get_job(self, job_id: str) -> OptimizerJob | None:
        return self._jobs.get(job_id)

    def _prune(self) -> None:
        if len(self._jobs) < _MAX_JOBS_KEPT:
            return
        finished = sorted(
            (j for j in self._jobs.values() if j.status in ("done", "cancelled", "error")),
            key=lambda j: j.created_at,
        )
        while len(self._jobs) >= _MAX_JOBS_KEPT and finished:
            self._jobs.pop(finished.pop(0).id, None)


def _clamp_candles(candles) -> int:
    try:
        n = int(candles)
    except (TypeError, ValueError):
        return _DEFAULT_CANDLES
    return max(_MIN_CANDLES, min(_MAX_CANDLES, n))


def _clamp_cores(cores) -> int:
    """Normalize a requested core count: 0 (or invalid) = Auto, else 1..CPU count."""
    try:
        n = int(cores)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return min(n, _system_cpu_count())


optimizer_manager = OptimizerManager()