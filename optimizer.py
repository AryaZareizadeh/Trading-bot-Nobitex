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
import time
import uuid
from dataclasses import fields
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import differential_evolution

from backtester import STRATEGY_LABELS, TIMEFRAMES, _VALID_TIMEFRAMES, _summarize
from bot import STRATEGY_MAP
from config import Config
from data import DataFetcher
from indicators import Indicators
from strategy import AdvRsiParams, StochRsiExtremeParams, TrendMomentumParams

# ── Search-space definition ──────────────────────────────────────────────────────
# Each strategy's tunable indicator inputs and their integer search bounds
# (inclusive). Every field of the strategy's params dataclass is optimized; the
# default value is read from the dataclass so the UI can show "default → tuned".
PARAMS_CLASS: dict[str, type] = {
    "AdvRSI": AdvRsiParams,
    "ExtremeReversal": StochRsiExtremeParams,
    "TrendMomentum": TrendMomentumParams,
}

# param_name -> (low, high). Bounds are inclusive integer ranges.
PARAM_BOUNDS: dict[str, dict[str, tuple[int, int]]] = {
    "AdvRSI": {
        "rsi_length": (5, 60),
        "stoch_length": (5, 60),
        "k_smooth": (1, 15),
        "d_smooth": (1, 15),
        "rsi_fast_length": (5, 30),
        "rsi_ma_length": (2, 20),
        "oversold": (5, 40),
        "overbought": (60, 95),
    },
    "ExtremeReversal": {
        "rsi_length": (5, 60),
        "stoch_length": (5, 60),
        "k_smooth": (1, 15),
        "d_smooth": (1, 15),
        "rsi_fast_length": (5, 30),
        "rsi_ma_length": (2, 20),
        "oversold": (5, 40),
        "overbought": (60, 95),
        "long_kd_max": (1, 25),
        "long_rsi_max": (15, 45),
        "short_kd_min": (75, 99),
        "short_rsi_min": (55, 85),
    },
    "TrendMomentum": {
        "rsi_length": (5, 60),
        "stoch_length": (5, 60),
        "k_smooth": (1, 15),
        "d_smooth": (1, 15),
        "ema_trend_length": (10, 200),
        "macd_fast": (5, 20),
        "macd_slow": (21, 50),
        "macd_signal": (3, 20),
        "oversold": (5, 40),
        "overbought": (60, 95),
    },
}

# Candle-count limits for the fetch.
_MIN_CANDLES = 200
_MAX_CANDLES = 5000
_DEFAULT_CANDLES = 1000

# Differential-Evolution budget. Total backtests ≈ popsize · n_params · (maxiter+1).
_DE_POPSIZE = 15
_DE_MAXITER = 30
_DE_TOL = 0.01
_DE_SEED = 42

# Cores used when the user leaves the selector on "Auto" (capped to the machine's
# CPU count, and overridable with the OPTIMIZER_WORKERS env var).
_DEFAULT_WORKERS = 8

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


def evaluate(df: pd.DataFrame, strategy_name: str, params, credit: float, leverage: int) -> dict:
    """Backtest one parameter set over a pre-fetched DataFrame; return its summary."""
    cfg = Config()
    cfg.leverage = leverage
    cfg.trade_size = credit
    cfg.initial_capital = credit

    indicators = Indicators(df)
    strategy = STRATEGY_MAP[strategy_name](cfg, params)
    trades_df = strategy.run(df, indicators, live_start_idx=0)
    return _summarize(trades_df, len(strategy.open_positions), len(df))


def _fetch_df(symbol: str, timeframe: str, candles: int) -> pd.DataFrame:
    cfg = Config()
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


def _worker_init(df, strategy_name, credit, leverage, names) -> None:
    """Pool initializer: runs once per worker process, caching the shared context."""
    _WORKER.update(df=df, strategy=strategy_name, credit=credit,
                   leverage=leverage, names=names)


def _worker_objective(vector) -> float:
    """Backtest one DE candidate inside a worker; return the value DE minimizes.

    Must be a module-level function (not a closure) so it can be pickled and sent
    to the worker processes. It reads everything but the candidate vector from the
    per-process ``_WORKER`` cache populated by ``_worker_init``.
    """
    params = _build_params(_WORKER["strategy"], _WORKER["names"], vector)
    summary = evaluate(_WORKER["df"], _WORKER["strategy"], params,
                       _WORKER["credit"], _WORKER["leverage"])
    return -summary["total_pnl"]   # DE minimizes, so negate to maximize profit


def _resolve_workers() -> int:
    """Number of worker processes to use for the DE search.

    Defaults to ``_DEFAULT_WORKERS`` cores (capped to the machine's CPU count).
    Override with the ``OPTIMIZER_WORKERS`` env var: a positive integer caps the
    count, ``-1`` means use every core.
    """
    cpu = os.cpu_count() or 1
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
                 credit: float, leverage: int, cores: int = 0):
        self.id = uuid.uuid4().hex[:8]
        self.symbol = symbol
        self.strategy = strategy
        self.timeframe = timeframe
        self.candles = candles
        self.credit = credit
        self.leverage = leverage
        self.requested_cores = cores     # 0 = Auto; else the user-picked core count

        self.status = "pending"          # pending | running | done | cancelled | error
        self.error: str | None = None
        self.created_at = time.time()

        self.maxiter = _DE_MAXITER
        self.workers = 1                 # CPU cores used by the search (set when DE starts)
        self.iteration = 0
        self.evaluations = 0
        self.best_pnl: float | None = None
        self.best_params: dict | None = None
        self.best_summary: dict | None = None
        self.baseline: dict | None = None      # result with the strategy's default inputs
        self.candle_count = 0

        self._cancel = False
        self._task: asyncio.Task | None = None

    def cancel(self) -> None:
        self._cancel = True

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
            self.workers = min(self.requested_cores, os.cpu_count() or 1)
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
        names = list(PARAM_BOUNDS[self.strategy].keys())
        bounds = [PARAM_BOUNDS[self.strategy][n] for n in names]
        integrality = np.ones(len(bounds), dtype=bool)

        best = {"pnl": -np.inf, "params": None, "summary": None}

        def objective(vector) -> float:
            params = _build_params(self.strategy, names, vector)
            summary = evaluate(df, self.strategy, params, self.credit, self.leverage)
            pnl = summary["total_pnl"]
            self.evaluations += 1
            if pnl > best["pnl"]:
                best["pnl"] = pnl
                best["params"] = {f.name: getattr(params, f.name) for f in fields(params)}
                best["summary"] = summary
            # DE minimizes, so negate to maximize profit.
            return -pnl

        def callback(intermediate_result=None, convergence=None):
            self.iteration += 1
            self.best_pnl = None if best["params"] is None else best["pnl"]
            self.best_params = best["params"]
            self.best_summary = best["summary"]
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
        names = list(PARAM_BOUNDS[self.strategy].keys())
        bounds = [PARAM_BOUNDS[self.strategy][n] for n in names]
        integrality = np.ones(len(bounds), dtype=bool)
        # SciPy's population size for this many parameters; one generation runs this
        # many backtests, which lets us report an approximate running total.
        pop_per_gen = max(5, _DE_POPSIZE * len(bounds))

        best = {"pnl": None, "params": None, "summary": None}

        def record(vector) -> None:
            """Re-score one DE vector in the parent and keep it if it's the best."""
            params = _build_params(self.strategy, names, vector)
            summary = evaluate(df, self.strategy, params, self.credit, self.leverage)
            pnl = summary["total_pnl"]
            if best["pnl"] is None or pnl > best["pnl"]:
                best["pnl"] = pnl
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
            self.evaluations = pop_per_gen * (self.iteration + 1)   # approximate
            self.best_pnl = best["pnl"]
            self.best_params = best["params"]
            self.best_summary = best["summary"]
            if self._cancel:
                return True   # signal DE to stop (takes effect after this generation)
            return False

        # "spawn" is the only start method on Windows and avoids fork-safety issues
        # elsewhere; the candle data + context are handed to each worker just once.
        ctx = mp.get_context("spawn")
        pool = ctx.Pool(
            processes=workers,
            initializer=_worker_init,
            initargs=(df, self.strategy, self.credit, self.leverage, names),
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

            # Baseline: the strategy's shipped default inputs, for comparison.
            self.baseline = await asyncio.to_thread(
                evaluate, df, self.strategy, PARAMS_CLASS[self.strategy](),
                self.credit, self.leverage,
            )

            await asyncio.to_thread(self._run_de, df)

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

    def _params_view(self) -> list[dict]:
        """Best params paired with their defaults and bounds, for display."""
        if not self.best_params:
            return []
        defaults = _defaults_for(self.strategy)
        bounds = PARAM_BOUNDS[self.strategy]
        out = []
        for name in bounds:
            out.append({
                "name": name,
                "default": defaults.get(name),
                "value": self.best_params.get(name),
                "low": bounds[name][0],
                "high": bounds[name][1],
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
            "iteration": self.iteration,
            "maxiter": self.maxiter,
            "workers": self.workers,
            "evaluations": self.evaluations,
            "best_pnl": self.best_pnl,
            "best_summary": self.best_summary,
            "params": self._params_view(),
            "baseline": self.baseline,
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
            "cores": 0,   # 0 = Auto (resolved from CPU count / OPTIMIZER_WORKERS)
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

    def save_prefs(self, symbol, strategy, timeframe, candles, credit, leverage, cores=0) -> None:
        self._prefs = {
            "symbol": (symbol or "").lower().replace("/", "").strip(),
            "strategy": strategy if strategy in STRATEGY_MAP else "",
            "timeframe": timeframe if timeframe in _VALID_TIMEFRAMES else "240",
            "candles": _clamp_candles(candles),
            "credit": float(credit),
            "leverage": int(leverage),
            "cores": _clamp_cores(cores),
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
            "max_cores": os.cpu_count() or 1,        # upper bound for the cores selector
            "default_cores": _resolve_workers(),     # what "Auto" resolves to right now
        }

    # ── Jobs ─────────────────────────────────────────────────────────────────
    def start_job(self, symbol, strategy, timeframe, candles, credit, leverage, cores=0) -> OptimizerJob:
        self._prune()
        job = OptimizerJob(
            symbol.lower().replace("/", "").strip(), strategy, timeframe,
            _clamp_candles(candles), float(credit), int(leverage), _clamp_cores(cores),
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
    return min(n, os.cpu_count() or 1)


optimizer_manager = OptimizerManager()
