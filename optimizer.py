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

Optuna's multivariate Tree-structured Parzen Estimator (TPE) is also available
for searches with an explicit trial budget. It learns promising combinations of
the same integer inputs using the same backtest objective as DE.

**Parallelism.** Each backtest is pure, GIL-bound Python/NumPy work, so threads
cannot run them simultaneously. Instead we fan each DE *generation* out across a
pool of worker processes — one per CPU core — so a 16-core machine runs ~16
backtests at once. SciPy drives this for us when handed ``workers=pool.map`` plus
``updating="deferred"``. See ``OptimizerJob._run_de_parallel``.

The parent process persists job inputs and results in SQLite; interrupted
searches are retained for inspection and require a new run.
"""

import asyncio
import math
import multiprocessing as mp
import os
import sys
import time
import uuid
import secrets
import threading
import warnings
from dataclasses import fields, asdict
from typing import Literal, get_args
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.optimize import differential_evolution

from backtester import STRATEGY_LABELS, TIMEFRAMES, _VALID_TIMEFRAMES, ResearchPersistence
from bot import STRATEGY_MAP
from config import Config
from data import DataFetcher
from indicators import Indicators
from execution_model import evaluate_research, execution_config, required_warmup, WARMUP_CANDLES
from strategy import AdvRsiParams, StochRsiExtremeParams, TrendMomentumParams, PARAMS_CLASS

exchange = "binance"  # default exchange for optimizer; can be overridden in Config

# ── Search-space definition ──────────────────────────────────────────────────────
# Each strategy's tunable indicator inputs and their integer search bounds
# (inclusive). Every field of the strategy's params dataclass is optimized; the
# default value is read from the dataclass so the UI can show "default → tuned".

# param_name -> (low, high). Broad defaults are inclusive integer ranges;
# indicator periods are in candles and oscillator thresholds stay within 1–99.
PARAM_BOUNDS: dict[str, dict[str, tuple[int, int]]] = {
    "AdvRSI": {
        "rsi_length": (6, 100),
        "stoch_length": (6, 100),
        "k_smooth": (4, 100),
        "d_smooth": (4, 100),
        "rsi_fast_length": (6, 100),
        "rsi_ma_length": (2, 100),
        "oversold": (1, 49),
        "overbought": (51, 99),
        "exit_oversold": (1, 49),
        "exit_overbought": (51, 99),
        "entry_wait_candles": (0, 100),
    },
    "ExtremeReversal": {
        "rsi_length": (2, 100),
        "stoch_length": (2, 100),
        "k_smooth": (1, 100),
        "d_smooth": (1, 100),
        "rsi_fast_length": (2, 100),
        "rsi_ma_length": (2, 100),
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
        "k_smooth": (1, 100),
        "d_smooth": (1, 100),
        "ema_trend_length": (10, 100),
        "macd_fast": (2, 99),  # repaired slow period must fit within the 100-candle cap
        "macd_slow": (21, 100),
        "macd_signal": (2, 100),
        "oversold": (1, 50),
        "overbought": (50, 99),
    },
}

INDEPENDENT_STRATEGY = "AdvRSIIndependent"
INDEPENDENT_SIDES = {"long": "AdvRSILong", "short": "AdvRSIShort"}
_INDEPENDENT_RSI_BOUNDS = {
    "rsi_length": (2, 100),
    "stoch_length": (2, 100),
    "k_smooth": (2, 100),
    "d_smooth": (2, 100),
    "rsi_fast_length": (2, 50),
    "rsi_ma_length": (2, 40),
    "oversold": (1, 35),
    "overbought": (65, 99),
    "exit_oversold": (1, 35),
    "exit_overbought": (65, 99),
    "entry_wait_candles": (0, 10),
}
for _side, _name in INDEPENDENT_SIDES.items():
    _removed = "overbought" if _side == "long" else "oversold"
    PARAM_BOUNDS[_name] = {key: value for key, value in _INDEPENDENT_RSI_BOUNDS.items() if key != _removed}


def optimizer_strategy_spec(name):
    if name == INDEPENDENT_STRATEGY:
        return {"value": name, "label": "Advanced RSI Independent (Long + Short)", "paired": True,
                "params": [dict(param, name=f"{side}_{param['name']}", side=side,
                                label=f"{side.title()} · {param['name']}")
                           for side, strategy in INDEPENDENT_SIDES.items()
                           for param in optimizer_strategy_spec(strategy)["params"]]}
    return {"value": name, "label": STRATEGY_LABELS.get(name, name),
            "params": [{"name": key, "low": low, "high": high, "default": _defaults_for(name).get(key)}
                       for key, (low, high) in PARAM_BOUNDS[name].items()]}

# Candle-count limits for the fetch.
_MIN_CANDLES = 200
_MAX_CANDLES = 30000
_DEFAULT_CANDLES = 1000

# Differential-Evolution budget. Total backtests ≈ popsize · n_params · (maxiter+1).
_DE_POPSIZE = 20
_DE_MAXITER = 30
_DE_TOL = 0.01
_DE_SEED = None  # None generates and records a concrete seed for each job.
DEStrategy = Literal[
    "best1bin", "best1exp", "rand1bin", "rand1exp", "rand2bin", "rand2exp",
    "randtobest1bin", "randtobest1exp", "currenttobest1bin", "currenttobest1exp",
    "best2bin", "best2exp",
]
DE_STRATEGIES = get_args(DEStrategy)
DEFAULT_DE_STRATEGY: DEStrategy = "best2bin"
_TPE_DEFAULT_TRIALS = 1000
_TPE_MIN_TRIALS = 10
_TPE_MAX_TRIALS = 100000
_OPTIMIZER_METHODS = ("de", "tpe")

# Cores used when the user leaves the selector on "Auto" (capped to the machine's
# CPU count, and overridable with the OPTIMIZER_WORKERS env var).
_DEFAULT_WORKERS = 14

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


def _balanced_eligible(summary: dict, min_win_rate: float, min_closed_trades: int) -> bool:
    """Only closed trades establish the win-rate and sample-size floor."""
    return (summary.get("trade_count", 0) >= min_closed_trades
            and summary.get("win_rate", 0.0) >= min_win_rate)


def _compute_score(summary: dict, objective: str, credit: float, min_price_change: float = 5.0,
                    min_win_rate: float = 50.0, min_closed_trades: int = 30) -> float:
    """Return the value DE minimizes for the given objective (lower = better)."""
    pnl = summary.get("total_pnl", 0.0)
    wr = summary.get("win_rate", 0.0)
    if objective == "winrate":
        # Maximize win rate; penalize zero-trade solutions.
        tc = summary.get("trade_count", 0)
        return -wr if tc > 0 else 100.0
    if objective == "balanced":
        # Ineligible candidates get a graded penalty so DE can move toward the
        # feasible region. The monotone transform below preserves the exact
        # ordering of uncapped net P&L while keeping every eligible score below
        # every ineligible score, including when all eligible runs lose money.
        if not _balanced_eligible(summary, min_win_rate, min_closed_trades):
            trades_short = max(0, min_closed_trades - summary.get("trade_count", 0))
            wr_short = max(0.0, min_win_rate - wr)
            return 1.0 + trades_short / min_closed_trades + wr_short / 100.0
        return -(2.0 / math.pi) * math.atan(pnl / credit)
    if objective == "large_move":
        # Maximize the % of trades where |price change| >= threshold.
        changes = summary.get("price_changes", [])
        tc = len(changes)
        if tc == 0:
            return 100.0
        return -(sum(1 for c in changes if abs(c) >= min_price_change) / tc * 100)
    # Default: maximize profit.
    return -pnl


def evaluate(df: pd.DataFrame, strategy_name: str, params, credit: float, leverage: int,
             warmup_df: pd.DataFrame | None = None, execution=None, start_idx=None) -> dict:
    """Evaluate net equity; held-out evaluation starts flat after warmup."""
    if warmup_df is not None:
        combined = pd.concat([warmup_df, df])
        combined.attrs = dict(df.attrs)
        start_idx = len(warmup_df)
    else:
        combined = df
    return evaluate_research(combined, strategy_name, credit, leverage, params,
                             execution, start_idx)["summary"]


def evaluate_selection(df, strategy_name, params, credit, leverage, execution=None, start_idx=None):
    """Score causal forward folds, starting flat with prior bars as warmup."""
    folds = df.attrs.get("optimizer_validation_folds") if df is not None else None
    if not folds:
        return evaluate(df, strategy_name, params, credit, leverage, execution=execution, start_idx=start_idx)
    summaries = [evaluate(df.iloc[:end].copy(), strategy_name, params, credit, leverage,
                          execution=execution, start_idx=start) for start, end in folds]
    count = sum(item.get("trade_count", 0) for item in summaries)
    return {"total_pnl": sum(item.get("total_pnl", 0.) for item in summaries),
            "trade_count": count,
            "win_rate": sum(item.get("win_rate", 0.) * item.get("trade_count", 0) for item in summaries) / count if count else 0.,
            "price_changes": [change for item in summaries for change in item.get("price_changes", [])],
            "candle_count": sum(end-start for start, end in folds),
            "fold_summaries": summaries, "evaluation": "forward_validation"}


def _fetch_df(symbol: str, timeframe: str, candles: int) -> pd.DataFrame:
    cfg = Config()
    cfg.exchange = exchange
    cfg.symbol = symbol
    cfg.interval = timeframe
    cfg.limit = candles + WARMUP_CANDLES
    df = DataFetcher(cfg).fetch()
    df.attrs["data_source"] = exchange
    return df


def _fetch_transfer_df(symbol, timeframe, candles):
    """Public Nobitex candles are independent of the search's source."""
    if os.getenv("TRADER_OFFLINE", "false").lower() == "true":
        raise RuntimeError("Nobitex transfer data unavailable in offline mode")
    cfg = Config()
    cfg.exchange, cfg.symbol, cfg.interval, cfg.limit = "nobitex", symbol, timeframe, candles
    frame = DataFetcher(cfg).fetch()
    frame.attrs["data_source"] = "nobitex"
    return frame


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


def _disable_windows_eco_qos(kernel32, process) -> bool:
    """Mark a CPU-bound optimizer worker as HighQoS instead of EcoQoS."""
    import ctypes
    from ctypes import wintypes

    class ProcessPowerThrottlingState(ctypes.Structure):
        _fields_ = [
            ("Version", wintypes.ULONG),
            ("ControlMask", wintypes.ULONG),
            ("StateMask", wintypes.ULONG),
        ]

    # ProcessPowerThrottling = 4.  Taking control of execution-speed
    # throttling while leaving its state off explicitly requests HighQoS.
    process_power_throttling = 4
    execution_speed = 0x1
    state = ProcessPowerThrottlingState(1, execution_speed, 0)
    kernel32.SetProcessInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel32.SetProcessInformation.restype = wintypes.BOOL
    return bool(kernel32.SetProcessInformation(
        process,
        process_power_throttling,
        ctypes.byref(state),
        ctypes.sizeof(state),
    ))


def _expand_cpu_affinity() -> None:
    """Give this worker full affinity and HighQoS for full P+E core use.

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
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.argtypes = []
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.GetProcessAffinityMask.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.POINTER(ctypes.c_size_t),
            ]
            kernel32.GetProcessAffinityMask.restype = wintypes.BOOL
            kernel32.SetProcessAffinityMask.argtypes = [
                wintypes.HANDLE, ctypes.c_size_t,
            ]
            kernel32.SetProcessAffinityMask.restype = wintypes.BOOL
            kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.SetPriorityClass.restype = wintypes.BOOL
            proc = kernel32.GetCurrentProcess()
            # 1. Expand affinity to the full system mask (all P+E cores)
            proc_mask = ctypes.c_size_t()
            sys_mask = ctypes.c_size_t()
            if kernel32.GetProcessAffinityMask(proc, ctypes.byref(proc_mask),
                                               ctypes.byref(sys_mask)):
                kernel32.SetProcessAffinityMask(proc, sys_mask.value)
            # 2. Opt out of EcoQoS, which can constrain a background process to
            # the efficient cores on a heterogeneous Windows CPU.
            _disable_windows_eco_qos(kernel32, proc)
            # 3. Raise priority so the search is treated as foreground work.
            ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
            kernel32.SetPriorityClass(proc, ABOVE_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass
    else:
        try:
            os.sched_setaffinity(0, range(_system_cpu_count()))
        except (AttributeError, OSError):
            pass


def _worker_init(df, strategy_name, credit, leverage, names, objective, min_price_change,
                  min_win_rate=50.0, min_closed_trades=30, execution=None, start_idx=None) -> None:
    """Pool initializer: runs once per worker process, caching the shared context."""
    _expand_cpu_affinity()
    _WORKER.update(df=df, strategy=strategy_name, credit=credit,
                   leverage=leverage, names=names, objective=objective,
                   min_price_change=min_price_change, min_win_rate=min_win_rate,
                   min_closed_trades=min_closed_trades, execution=execution, start_idx=start_idx)


def _worker_objective(vector) -> float:
    """Backtest one DE candidate inside a worker; return the value DE minimizes.

    Must be a module-level function (not a closure) so it can be pickled and sent
    to the worker processes. It reads everything but the candidate vector from the
    per-process ``_WORKER`` cache populated by ``_worker_init``.
    """
    params = _build_params(_WORKER["strategy"], _WORKER["names"], vector)
    summary = evaluate_selection(_WORKER["df"], _WORKER["strategy"], params,
                       _WORKER["credit"], _WORKER["leverage"], execution=_WORKER.get("execution"), start_idx=_WORKER.get("start_idx"))
    return _compute_score(summary, _WORKER["objective"], _WORKER["credit"], _WORKER["min_price_change"],
                          _WORKER.get("min_win_rate", 50.0), _WORKER.get("min_closed_trades", 30))


def _finite_result(value) -> bool:
    """Reject nonfinite research metrics before they reach Optuna or JSON."""
    if isinstance(value, dict):
        return all(_finite_result(item) for item in value.values())
    if isinstance(value, (list, tuple, np.ndarray)):
        return all(_finite_result(item) for item in value)
    if isinstance(value, (int, float, np.number)):
        return math.isfinite(float(value))
    return True


def _evaluate_tpe_candidate(df, strategy, credit, leverage, names, vector, objective,
                            min_price_change, min_win_rate, min_closed_trades,
                            execution=None, start_idx=None) -> dict:
    """One candidate result, including failures, suitable for parent-owned tell."""
    params_view = None
    try:
        params = _build_params(strategy, names, vector)
        params_view = asdict(params)
        summary = evaluate_selection(df, strategy, params, credit, leverage,
                           execution=execution, start_idx=start_idx)
        score = float(_compute_score(summary, objective, credit, min_price_change,
                                     min_win_rate, min_closed_trades))
        if not math.isfinite(score) or not _finite_result(summary):
            raise ValueError("Backtest produced nonfinite metrics")
        return {"params": params_view, "summary": summary, "score": score, "error": None}
    except Exception as exc:
        return {"params": params_view, "summary": None, "score": None, "error": str(exc)}


def _worker_tpe_evaluate(vector) -> dict:
    """Picklable TPE evaluator using the same spawn-worker context as DE."""
    return _evaluate_tpe_candidate(
        _WORKER["df"], _WORKER["strategy"], _WORKER["credit"], _WORKER["leverage"],
        _WORKER["names"], vector, _WORKER["objective"], _WORKER["min_price_change"],
        _WORKER["min_win_rate"], _WORKER["min_closed_trades"],
        _WORKER.get("execution"), _WORKER.get("start_idx"),
    )


def _validate_search(method, n_trials):
    if method not in _OPTIMIZER_METHODS:
        raise ValueError("method must be 'de' or 'tpe'")
    if (isinstance(n_trials, bool) or not isinstance(n_trials, (int, np.integer))
            or not _TPE_MIN_TRIALS <= n_trials <= _TPE_MAX_TRIALS):
        raise ValueError(f"n_trials must be an integer from {_TPE_MIN_TRIALS} to {_TPE_MAX_TRIALS}")
    return method, int(n_trials)


def _validate_de_strategy(de_strategy):
    if not isinstance(de_strategy, str) or de_strategy not in DE_STRATEGIES:
        raise ValueError("de_strategy must be a built-in SciPy Differential Evolution strategy")
    return de_strategy


def _resolve_workers() -> int:
    """Number of worker processes to use for the DE search.

    Defaults to ``_DEFAULT_WORKERS`` cores (capped to the machine's CPU count).
    Override with the ``OPTIMIZER_WORKERS`` env var: a positive integer caps the
    count, ``-1`` means use every core.
    """
    cpu = _configured_worker_cap()
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


def _configured_worker_cap() -> int:
    cpu = _system_cpu_count()
    try:
        limit = int(os.getenv("OPTIMIZER_MAX_CORES_PER_JOB", ""))
    except ValueError:
        return cpu
    return min(cpu, limit) if limit > 0 else cpu


# ── Background job ───────────────────────────────────────────────────────────────

class OptimizerJob:
    """A single DE or multivariate TPE search over one strategy's inputs."""

    def __init__(self, symbol: str, strategy: str, timeframe: str, candles: int,
                 credit: float, leverage: int, cores: int = 0,
                 maxiter: int = _DE_MAXITER,
                 custom_bounds: dict | None = None,
                 objective: str = "profit",
                 min_price_change: float = 5.0,
                 min_win_rate: float = 50.0,
                 min_closed_trades: int = 30,
                 train_pct: float | None = None, execution=None, random_seed=None,
                 method: str = "de", n_trials: int = _TPE_DEFAULT_TRIALS,
                 de_strategy: DEStrategy = DEFAULT_DE_STRATEGY,
                 evaluation_mode: str | None = None):
        self.id = uuid.uuid4().hex[:8]
        self.symbol = symbol
        self.strategy = strategy
        self.timeframe = timeframe
        self.candles = candles
        self.credit = credit
        self.leverage = leverage
        self.method, self.n_trials = _validate_search(method, n_trials)
        self.de_strategy = _validate_de_strategy(de_strategy)
        self.execution = asdict(execution_config(execution))
        self.random_seed = int(random_seed) if random_seed is not None else (secrets.randbits(32) if _DE_SEED is None else _DE_SEED)
        if not 0 <= self.random_seed < 2**32:
            raise ValueError("random_seed must be an unsigned 32-bit integer")
        self.evaluation_start = None
        self.requested_cores = cores     # 0 = Auto; else the user-picked core count
        self.objective = objective
        self.min_price_change = float(min_price_change) if min_price_change else 5.0
        self.min_win_rate = float(min_win_rate)
        self.min_closed_trades = int(min_closed_trades)
        if not 0 < self.min_win_rate <= 100 or not 10 <= self.min_closed_trades <= 10000:
            raise ValueError("Balanced goal requires win rate in (0, 100] and at least 10 closed trades")
        self.custom_bounds = custom_bounds or {}

        self.status = "pending"          # pending | running | pausing | paused | done | cancelled | error
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
        self.training_summary: dict | None = None  # same evaluation as best_trades and chart_data
        self.baseline: dict | None = None      # result with the strategy's default inputs
        self.candle_count = 0
        self.history: list[dict] = []    # per-generation best snapshots
        self.trials: list[dict] = []     # completed TPE candidates for inspection
        self.best_trades: list[dict] | None = None  # trade log from the best run
        self.chart_data: dict | None = None          # OHLCV + indicator series for the chart
        self._charted_params: dict | None = None

        if evaluation_mode is None:
            evaluation_mode = "chronological" if train_pct is not None else "in_sample"
        if evaluation_mode not in ("chronological", "in_sample"):
            raise ValueError("evaluation_mode must be 'chronological' or 'in_sample'")
        if train_pct is not None and (not math.isfinite(float(train_pct)) or not 50 <= float(train_pct) <= 90):
            raise ValueError("train_pct must be between 50 and 90")
        if evaluation_mode == "in_sample" and train_pct is not None:
            raise ValueError("In-sample exploration cannot include a held-out train percentage")
        self.evaluation_mode = evaluation_mode
        self.train_pct = (70. if train_pct is None else float(train_pct)) if evaluation_mode == "chronological" else None
        self.train_candle_count = 0
        self.test_candle_count = 0
        self.test_summary: dict | None = None
        self.test_baseline: dict | None = None
        self.test_trades: list[dict] | None = None
        self.test_chart_data: dict | None = None
        self.validation_summary = None
        self.validation_baseline = None
        self.validation_folds = []
        self.frozen_params = None
        self.evaluation_metadata = {}
        self.promotion = {"eligible": False, "status": "pending", "reason": "Research is incomplete", "target_source": "nobitex"}

        self._cancel = False
        self._pause_requested = False
        self._pause_condition = threading.Condition()
        self._pause_started_at: float | None = None
        self.paused_seconds = 0.0
        self._task: asyncio.Task | None = None
        self._persist = lambda: None

    def cancel(self) -> None:
        with self._pause_condition:
            self._cancel = True
            if self._pause_started_at is not None:
                self.paused_seconds += time.time() - self._pause_started_at
                self._pause_started_at = None
            self._pause_condition.notify_all()
            self._persist()

    def pause(self) -> bool:
        with self._pause_condition:
            if self.status not in ("pending", "running") or self._cancel:
                return False
            self._pause_requested = True
            self.status = "pausing"
            self._persist()
            return True

    def resume(self) -> bool:
        with self._pause_condition:
            if self.status not in ("pausing", "paused") or self._cancel:
                return False
            self._pause_requested = False
            if self._pause_started_at is not None:
                self.paused_seconds += time.time() - self._pause_started_at
                self._pause_started_at = None
            self.status = "running"
            self._pause_condition.notify_all()
            self._persist()
            return True

    def _wait_if_paused(self) -> None:
        with self._pause_condition:
            while self._pause_requested and not self._cancel:
                if self.status != "paused":
                    self.status = "paused"
                    self._pause_started_at = time.time()
                    self._persist()
                self._pause_condition.wait()

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

    def _run_search(self, df: pd.DataFrame) -> None:
        if self.method == "tpe":
            self._run_tpe(df)
        else:
            self._run_de(df)

    def _run_tpe(self, df: pd.DataFrame) -> None:
        """Ask/tell in the parent, with bounded batches of spawn-worker backtests."""
        try:
            import optuna
        except ImportError as exc:
            raise RuntimeError("TPE requires Optuna; install the project requirements") from exc

        self._wait_if_paused()
        if self._cancel:
            return
        self.workers = (min(self.requested_cores, _configured_worker_cap())
                        if self.requested_cores and self.requested_cores >= 1 else _resolve_workers())
        self.workers = min(self.workers, self.n_trials)
        bounds = self._effective_bounds
        names = list(bounds)
        # These explicitly selected Optuna options are experimental in the pinned
        # release; suppress only its configuration notices, never trial failures.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", optuna.exceptions.ExperimentalWarning)
            sampler = optuna.samplers.TPESampler(multivariate=True, seed=self.random_seed,
                                                 constant_liar=True)
        study = optuna.create_study(direction="minimize", sampler=sampler)
        best_score = math.inf
        pool = None
        try:
            if self.workers > 1:
                pool = mp.get_context("spawn").Pool(
                    processes=self.workers, initializer=_worker_init,
                    initargs=(df, self.strategy, self.credit, self.leverage, names, self.objective,
                              self.min_price_change, self.min_win_rate, self.min_closed_trades,
                              self.execution, self.evaluation_start),
                )
            while self.evaluations < self.n_trials:
                self._wait_if_paused()
                if self._cancel:
                    break
                batch_size = min(self.workers, self.n_trials - self.evaluations)
                batch = []
                for _ in range(batch_size):
                    trial = study.ask()
                    vector = [trial.suggest_int(name, int(bounds[name][0]), int(bounds[name][1]))
                              for name in names]
                    batch.append((trial, vector))
                if pool is not None:
                    results = pool.map(_worker_tpe_evaluate, [vector for _, vector in batch])
                else:
                    results = [_evaluate_tpe_candidate(
                        df, self.strategy, self.credit, self.leverage, names, vector, self.objective,
                        self.min_price_change, self.min_win_rate, self.min_closed_trades,
                        self.execution, self.evaluation_start,
                    ) for _, vector in batch]
                for (trial, _), result in zip(batch, results):
                    score, summary = result["score"], result["summary"]
                    if result["error"] is not None:
                        study.tell(trial, state=optuna.trial.TrialState.FAIL)
                        state = "fail"
                    else:
                        study.tell(trial, score)
                        state = "complete"
                        eligible = (self.objective != "balanced" or _balanced_eligible(
                            summary, self.min_win_rate, self.min_closed_trades))
                        if eligible and score < best_score:
                            best_score = score
                            self.best_pnl = summary["total_pnl"]
                            self.best_params = result["params"]
                            self.best_summary = summary
                    self.evaluations += 1
                    self.iteration = self.evaluations
                    self.trials.append({
                        "number": trial.number, "state": state, "score": score,
                        "params": result["params"], "sampled_params": dict(trial.params),
                        # Per-trade changes are scored above; snapshots retain
                        # aggregate metrics without duplicating candle-length data.
                        "summary": {key: value for key, value in summary.items()
                                    if key not in ("price_changes", "metadata")} if summary else None,
                        "error": result["error"],
                    })
                    self.history.append({
                        "iteration": self.iteration, "trial": trial.number, "state": state,
                        "pnl": self.best_pnl,
                        "win_rate": self.best_summary.get("win_rate") if self.best_summary else None,
                        "trade_count": self.best_summary.get("trade_count") if self.best_summary else None,
                        "delta": None,
                    })
                self._refresh_best_chart(df)
                self._persist()
            if (not self._cancel and self.evaluations >= self.n_trials
                    and not any(trial["state"] == "complete" for trial in self.trials)):
                raise RuntimeError(f"All {self.n_trials} TPE trials failed; check the trial errors and search bounds.")
        finally:
            if pool is not None:
                pool.terminate()
                pool.join()

    # ── DE plumbing ──────────────────────────────────────────────────────────
    def _de_kwargs(self, bounds, integrality, callback) -> dict:
        """The DE settings shared by the serial and parallel search paths."""
        return dict(
            bounds=bounds,
            integrality=integrality,
            popsize=_DE_POPSIZE,
            maxiter=self.maxiter,
            tol=_DE_TOL,
            rng=self.random_seed,
            polish=False,        # L-BFGS polish ignores integrality; skip it
            mutation=(0.5, 1.0),
            recombination=0.7,
            callback=callback,
            init="sobol",        # space-filling start for better global coverage
            strategy=self.de_strategy,
        )

    def _run_de(self, df: pd.DataFrame) -> None:
        """Pick a search path: parallel across cores, else single-core."""
        self._wait_if_paused()
        if self._cancel:
            return
        # A specific core count picked in the UI wins; 0 means "Auto" (defer to the
        # CPU-count / OPTIMIZER_WORKERS default).
        if self.requested_cores and self.requested_cores >= 1:
            self.workers = min(self.requested_cores, _configured_worker_cap())
        else:
            self.workers = _resolve_workers()
        if self.workers <= 1:
            self._run_de_serial(df)
            return
        self._run_de_parallel(df, self.workers)

    def _run_de_serial(self, df: pd.DataFrame) -> None:
        """Single-core search: one backtest at a time in this process."""
        eff = self._effective_bounds
        names = list(eff.keys())
        bounds = [eff[n] for n in names]
        integrality = np.ones(len(bounds), dtype=bool)

        best = {"score": np.inf, "pnl": -np.inf, "params": None, "summary": None}

        def objective(vector) -> float:
            params = _build_params(self.strategy, names, vector)
            summary = evaluate_selection(df, self.strategy, params, self.credit, self.leverage, execution=self.execution, start_idx=self.evaluation_start)
            score = _compute_score(summary, self.objective, self.credit, self.min_price_change, self.min_win_rate, self.min_closed_trades)
            self.evaluations += 1
            eligible = self.objective != "balanced" or _balanced_eligible(summary, self.min_win_rate, self.min_closed_trades)
            if eligible and score < best["score"]:
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
            self._refresh_best_chart(df)
            self.history.append({
                "iteration": self.iteration,
                "pnl": best["pnl"] if best["params"] is not None else None,
                "win_rate": best["summary"].get("win_rate") if best["summary"] else None,
                "trade_count": best["summary"].get("trade_count") if best["summary"] else None,
                "delta": self.current_delta,
            })
            self._persist()
            self._wait_if_paused()
            return self._cancel   # signal DE to stop after this generation

        differential_evolution(objective, **self._de_kwargs(bounds, integrality, callback))

        # Final best (in case the last improvement landed after the last callback).
        self.best_pnl = best["pnl"] if best["params"] is not None else None
        self.best_params = best["params"]
        self.best_summary = best["summary"]
        self._refresh_best_chart(df)

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
            summary = evaluate_selection(df, self.strategy, params, self.credit, self.leverage, execution=self.execution, start_idx=self.evaluation_start)
            score = _compute_score(summary, self.objective, self.credit, self.min_price_change, self.min_win_rate, self.min_closed_trades)
            eligible = self.objective != "balanced" or _balanced_eligible(summary, self.min_win_rate, self.min_closed_trades)
            if eligible and (best["score"] is None or score < best["score"]):
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
            self._refresh_best_chart(df)
            self.history.append({
                "iteration": self.iteration,
                "pnl": best["pnl"],
                "win_rate": best["summary"].get("win_rate") if best["summary"] else None,
                "trade_count": best["summary"].get("trade_count") if best["summary"] else None,
                "delta": self.current_delta,
            })
            self._persist()
            self._wait_if_paused()
            return self._cancel   # signal DE to stop after this generation

        # "spawn" is the only start method on Windows and avoids fork-safety issues
        # elsewhere; the candle data + context are handed to each worker just once.
        ctx = mp.get_context("spawn")
        pool = ctx.Pool(
            processes=workers,
            initializer=_worker_init,
            initargs=(df, self.strategy, self.credit, self.leverage, names, self.objective, self.min_price_change,
                      self.min_win_rate, self.min_closed_trades, self.execution, self.evaluation_start),
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
        self._refresh_best_chart(df)

    async def run(self, shared_df=None, evaluation_start=None) -> None:
        self.status = "pausing" if self._pause_requested else "running"
        self._persist()
        try:
            # Every candidate sees the same evaluation candles, even when its
            # indicator periods differ. Reserve enough history for all bounds.
            upper = {key: bounds[1] for key, bounds in self._effective_bounds.items()}
            warmup = required_warmup(upper)
            df = shared_df if shared_df is not None else await asyncio.to_thread(
                _fetch_df, self.symbol, self.timeframe, self.candles + warmup-WARMUP_CANDLES)
            if df is None or df.empty or len(df) < 50:
                self.status = "error"
                self.error = "insufficient candle data"
                return
            if self._cancel:
                self.status = "cancelled"
                return
            if len(df) <= warmup:
                raise ValueError("Insufficient candle data after indicator warmup")
            self.evaluation_start = evaluation_start if evaluation_start is not None else max(warmup, len(df)-self.candles)
            if self.evaluation_start < warmup:
                raise ValueError("Insufficient shared indicator warmup")
            self.candle_count = len(df)-self.evaluation_start
            self.train_candle_count = self.candle_count

            # Reserve the final holdout before any candidate is evaluated.
            train_df = df
            test_df: pd.DataFrame | None = None
            if self.train_pct is None:
                train_df = df.copy()
                train_df.attrs.pop("optimizer_validation_folds", None)
            if self.train_pct is not None:
                train_count = int(self.candle_count * self.train_pct / 100)
                train_n = self.evaluation_start + train_count
                if train_count < 50:
                    self.status = "error"
                    self.error = (
                        f"Training set too small ({train_count} candles < 50 minimum). "
                        "Increase candle count or the train %."
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
                self.train_candle_count = train_count
                self.test_candle_count = len(test_df)

                initial = self.evaluation_start + train_count // 2
                edges = [initial + (train_n-initial) * number // 3 for number in range(4)]
                folds = [(start, end) for start, end in zip(edges[:-1], edges[1:]) if end > start]
                train_df.attrs["optimizer_validation_folds"] = folds
                self.validation_folds = [{"start": df.index[start].isoformat(), "end": df.index[end-1].isoformat(),
                                          "start_idx": start, "end_idx": end,
                                          "warmup_end": df.index[start-1].isoformat(), "starts_flat": True}
                                         for start, end in folds]

            self.evaluation_metadata = {
                "version": "optimizer-validation-v1", "evaluation_mode": self.evaluation_mode,
                "source": df.attrs.get("data_source", exchange), "random_seed": self.random_seed,
                "execution": dict(self.execution), "objective": self.objective,
                "selection_rule": "best objective across three forward validation folds; final test never selects parameters"
                    if self.train_pct is not None else "best in-sample objective; exploration only",
                "training_window": {"start": df.index[self.evaluation_start].isoformat(), "end": train_df.index[-1].isoformat()},
                "validation_windows": list(self.validation_folds),
                "test_window": {"start": test_df.index[0].isoformat(), "end": test_df.index[-1].isoformat()} if test_df is not None else None,
                "budget": {"method": self.method, "maxiter": self.maxiter, "n_trials": self.n_trials,
                           "population_multiplier": _DE_POPSIZE, "parameter_count": len(self._effective_bounds)},
            }

            # Baseline: the strategy's shipped default inputs, for comparison.
            self.baseline = await asyncio.to_thread(
                evaluate, train_df, self.strategy, PARAMS_CLASS[self.strategy](),
                self.credit, self.leverage, execution=self.execution, start_idx=self.evaluation_start,
            )

            await asyncio.to_thread(self._run_search, train_df)

            if not self._cancel and self.best_params:
                self.frozen_params = dict(self.best_params)
                self.evaluation_metadata["frozen_params"] = dict(self.frozen_params)
                self.evaluation_metadata["budget"]["candidate_evaluations"] = self.evaluations
                if test_df is not None:
                    self.validation_summary = await asyncio.to_thread(
                        evaluate_selection, train_df, self.strategy, PARAMS_CLASS[self.strategy](**self.frozen_params),
                        self.credit, self.leverage, execution=self.execution, start_idx=self.evaluation_start)
                    self.validation_baseline = await asyncio.to_thread(
                        evaluate_selection, train_df, self.strategy, PARAMS_CLASS[self.strategy](),
                        self.credit, self.leverage, execution=self.execution, start_idx=self.evaluation_start)
                await asyncio.to_thread(self._refresh_best_chart, train_df)
                if test_df is not None:
                    await asyncio.to_thread(self._compute_test_results, df, train_n)
                    await asyncio.to_thread(self._compute_transfer_results, df, train_n)
                else:
                    self.promotion = {"eligible": False, "status": "in_sample", "target_source": "nobitex",
                                      "reason": "In-sample exploration cannot be promoted to real trading"}

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
        finally:
            self._persist()

    def _refresh_best_chart(self, df: pd.DataFrame) -> None:
        """Publish a new chart only when the best parameter set changes."""
        if self.best_params and self.best_params != self._charted_params:
            self._compute_best_trades(df)

    def _compute_best_trades(self, df: pd.DataFrame) -> None:
        """Re-run the best strategy and publish trades and chart data for polling."""
        if not self.best_params:
            return
        charted_params = dict(self.best_params)
        chart_start = (self.evaluation_start or 0) if self.train_pct is None else 0
        indicators = Indicators(df)
        params = PARAMS_CLASS[self.strategy](**charted_params)
        result = evaluate_research(df, self.strategy, self.credit, self.leverage, params, self.execution, self.evaluation_start)
        trades = result["trades"]

        # ── Chart data ───────────────────────────────────────────────────────
        _tehran = ZoneInfo("Asia/Tehran")

        def _to_series(s):
            return [None if pd.isna(v) else round(float(v), 6) for v in s.iloc[chart_start:]]

        # Build Tehran-time → Unix-UTC lookup so trade markers can be placed
        time_map: dict[str, int] = {}
        candles: list[dict] = []
        for ts, row in df.iloc[chart_start:].iterrows():
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
        for r in trades:
            r["_start_ts"] = time_map.get(r.get("Start Time", ""))
            r["_end_ts"] = time_map.get(r.get("End Time", ""))

        # Per-strategy indicator series
        ind: dict = {}
        if self.strategy in ("AdvRSI", "ExtremeReversal", "AdvRSILong", "AdvRSIShort"):
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
                "oversold":  getattr(params, "oversold", None),
                "overbought": getattr(params, "overbought", None),
                "exit_oversold": getattr(params, "exit_oversold", None),
                "exit_overbought": getattr(params, "exit_overbought", None),
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

        revision = (self.chart_data or {}).get("revision", 0) + 1
        self.best_trades = trades
        self.training_summary = result["summary"]
        self.chart_data = {"candles": candles, "indicators": ind,
                           "strategy": self.strategy, "revision": revision}
        self._charted_params = charted_params

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

        best_params_obj = PARAMS_CLASS[self.strategy](**(self.frozen_params or self.best_params))

        # Summary metrics — each evaluate() call prepends train_df for warmup
        self.test_summary  = evaluate(test_df, self.strategy, best_params_obj,
                                      self.credit, self.leverage, warmup_df=train_df, execution=self.execution)
        self.test_baseline = evaluate(test_df, self.strategy, PARAMS_CLASS[self.strategy](),
                                      self.credit, self.leverage, warmup_df=train_df, execution=self.execution)

        # Trade log — run strategy over full_df so indicators have warmup history
        indicators = Indicators(full_df)
        self.test_trades = evaluate_research(full_df, self.strategy, self.credit, self.leverage,
                                             best_params_obj, self.execution, train_n)["trades"]

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
        if self.strategy in ("AdvRSI", "ExtremeReversal", "AdvRSILong", "AdvRSIShort"):
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
                "oversold":   getattr(best_params_obj, "oversold", None),
                "overbought": getattr(best_params_obj, "overbought", None),
                "exit_oversold": getattr(best_params_obj, "exit_oversold", None),
                "exit_overbought": getattr(best_params_obj, "exit_overbought", None),
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

    def _compute_transfer_results(self, full_df, train_n):
        """Evaluate the frozen selection on exactly aligned target-source bars."""
        policy = {"objective": self.objective, "min_closed_trades": self.min_closed_trades,
                  "min_win_rate": self.min_win_rate, "positive_net_pnl": True,
                  "rule": "forward validation, final source test and Nobitex test each beat defaults and meet sample/win-rate floors"}
        report = {"eligible": False, "status": "unavailable", "target_source": "nobitex", "policy": policy,
                  "window": {"start": full_df.index[train_n].isoformat(), "end": full_df.index[-1].isoformat()},
                  "frozen_params": dict(self.frozen_params or {})}
        self.promotion = report
        try:
            if self.evaluation_mode != "chronological" or not self.frozen_params:
                raise ValueError("Chronological frozen selection is required")
            if self.best_params != self.frozen_params:
                raise ValueError("Selected parameters changed after freezing")
            if full_df.attrs.get("data_source") == "nobitex":
                target = full_df.copy()
            else:
                target = _fetch_transfer_df(self.symbol, self.timeframe, len(full_df))
            if target is None or target.empty:
                raise ValueError("Nobitex transfer candles are unavailable")
            if target.attrs.get("data_source") != "nobitex":
                raise ValueError("Transfer candle source is not Nobitex")
            target = target.copy()
            target.index = pd.to_datetime(target.index, utc=True)
            expected = pd.to_datetime(full_df.index, utc=True)
            if target.index.has_duplicates or not target.index.is_monotonic_increasing:
                raise ValueError("Nobitex transfer candles have invalid timestamps")
            aligned = target.reindex(expected)
            aligned.attrs = {**target.attrs, "data_source": "nobitex"}
            if aligned.isna().any().any():
                raise ValueError("Nobitex transfer window or indicator warmup is incomplete")
            params = PARAMS_CLASS[self.strategy](**self.frozen_params)
            chosen = evaluate(aligned, self.strategy, params, self.credit, self.leverage,
                              execution=self.execution, start_idx=train_n)
            baseline = evaluate(aligned, self.strategy, PARAMS_CLASS[self.strategy](), self.credit, self.leverage,
                                execution=self.execution, start_idx=train_n)
            if not _finite_result(chosen) or not _finite_result(baseline):
                raise ValueError("Nobitex transfer metrics are nonfinite")
            report.update(summary=chosen, baseline=baseline, status="rejected")
            failures = []
            for name, selected, defaults in (("forward validation", self.validation_summary, self.validation_baseline),
                                             ("final source test", self.test_summary, self.test_baseline),
                                             ("Nobitex test", chosen, baseline)):
                if not selected or not defaults or not _finite_result(selected) or not _finite_result(defaults):
                    failures.append(f"{name}: valid metrics unavailable")
                    continue
                if not _balanced_eligible(selected, self.min_win_rate, self.min_closed_trades):
                    failures.append(f"{name}: closed-trade or win-rate floor not met")
                if selected.get("total_pnl", 0.) <= 0:
                    failures.append(f"{name}: positive net P&L required")
                scores = [_compute_score(item, self.objective, self.credit, self.min_price_change,
                                         self.min_win_rate, self.min_closed_trades) for item in (selected, defaults)]
                if scores[0] >= scores[1]:
                    failures.append(f"{name}: selected parameters did not beat defaults")
            report.update(eligible=not failures, status="validated" if not failures else "rejected",
                          reason="Chronological validation and Nobitex transfer checks passed" if not failures else "; ".join(failures))
        except Exception as exc:
            # Public-source failures never rewrite or discard a finished draft.
            report["reason"] = f"Nobitex transfer unavailable: {type(exc).__name__}: {exc}"

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
            "execution": self.execution,
            "random_seed": self.random_seed,
            "evaluation_start": self.evaluation_start,
            "custom_bounds": self.custom_bounds,
            "search_bounds": self._effective_bounds,
            "min_price_change": self.min_price_change,
            "min_win_rate": self.min_win_rate,
            "min_closed_trades": self.min_closed_trades,
            "no_eligible_result": self.objective == "balanced" and self.status == "done" and self.best_params is None,
            "requested_cores": self.requested_cores,
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at,
            "symbol": self.symbol.upper(),
            "strategy": self.strategy,
            "strategy_label": STRATEGY_LABELS.get(self.strategy, self.strategy),
            "timeframe": self.timeframe,
            "candles": self.candles,
            "candle_count": self.candle_count,
            "credit": self.credit,
            "leverage": self.leverage,
            "objective": self.objective,
            "method": self.method,
            "n_trials": self.n_trials,
            "de_strategy": self.de_strategy,
            "iteration": self.iteration,
            "maxiter": self.maxiter,
            "workers": self.workers,
            "paused_seconds": self.paused_seconds,
            "pause_supported": True,
            "evaluations": self.evaluations,
            "current_delta": self.current_delta,
            "delta_tol": _DE_TOL,
            "best_pnl": self.best_pnl,
            "best_params": self.best_params,
            "best_summary": self.best_summary,
            "training_summary": self.training_summary,
            "best_trades": self.best_trades,
            "chart_data": self.chart_data,
            "params": self._params_view(),
            "baseline": self.baseline,
            "history": self.history,  # full run, so the chart can plot start-to-finish
            "trials": self.trials,
            "evaluation_mode": self.evaluation_mode,
            "evaluation_metadata": self.evaluation_metadata,
            "validation_summary": self.validation_summary,
            "validation_baseline": self.validation_baseline,
            "validation_folds": self.validation_folds,
            "frozen_params": self.frozen_params,
            "promotion": self.promotion,
            # Tester-mode fields
            "train_pct": self.train_pct,
            "train_candle_count": self.train_candle_count,
            "test_candle_count": self.test_candle_count,
            "test_summary": self.test_summary,
            "test_baseline": self.test_baseline,
            "test_trades": self.test_trades,
            "test_chart_data": self.test_chart_data,
        }


class IndependentOptimizerJob(OptimizerJob):
    """Two independent searches sharing only market data and evaluation boundaries."""

    def __init__(self, symbol, timeframe, candles, long_credit, short_credit, leverage, cores=0,
                 maxiter=_DE_MAXITER, custom_bounds=None, **kwargs):
        if any(not math.isfinite(amount) or amount <= 0 for amount in (long_credit, short_credit)):
            raise ValueError("Each side's credit must be finite and positive")
        self.long_credit, self.short_credit = float(long_credit), float(short_credit)
        super().__init__(symbol, "AdvRSILong", timeframe, candles, self.long_credit + self.short_credit, leverage,
                         cores, maxiter=maxiter, **kwargs)
        self.strategy = INDEPENDENT_STRATEGY
        self.custom_bounds = custom_bounds or {}
        self.active_side = None
        options = dict(kwargs, random_seed=self.random_seed)
        self.sides = {}
        for side, strategy in INDEPENDENT_SIDES.items():
            side_credit = self.long_credit if side == "long" else self.short_credit
            bounds = {key[len(side)+1:]: value for key, value in self.custom_bounds.items()
                      if key.startswith(side + "_")}
            if side == "short":
                options["random_seed"] = (self.random_seed + 1) % 2**32
            job = OptimizerJob(symbol, strategy, timeframe, candles, side_credit, leverage,
                               cores, maxiter=maxiter, custom_bounds=bounds, **options)
            job._persist = self._persist_side
            self.sides[side] = job

    @property
    def _effective_bounds(self):
        return {f"{side}_{key}": bounds for side, job in self.sides.items()
                for key, bounds in job._effective_bounds.items()}

    def _persist_side(self):
        if self.active_side and self._pause_requested:
            active = self.sides[self.active_side]
            if active.status == "paused":
                self.status = "paused"
                if self._pause_started_at is None:
                    self._pause_started_at = time.time()
        self._persist()

    def pause(self):
        if not super().pause():
            return False
        for job in self.sides.values():
            job.pause()
        return True

    def resume(self):
        if not super().resume():
            return False
        for job in self.sides.values():
            job.resume()
        return True

    def cancel(self):
        super().cancel()
        for job in self.sides.values():
            job.cancel()

    async def run(self):
        self.status = "pausing" if self._pause_requested else "running"
        self._persist()
        try:
            await asyncio.to_thread(self._wait_if_paused)
            if self._cancel:
                self.status = "cancelled"
                return
            warmup = max(required_warmup({key: value[1] for key, value in job._effective_bounds.items()})
                         for job in self.sides.values())
            df = await asyncio.to_thread(_fetch_df, self.symbol, self.timeframe, self.candles + warmup-WARMUP_CANDLES)
            if df is None or df.empty or len(df) <= warmup:
                raise ValueError("Insufficient candle data after indicator warmup")
            self.evaluation_start = max(warmup, len(df)-self.candles)
            self.candle_count = len(df)-self.evaluation_start
            for side, job in self.sides.items():
                await asyncio.to_thread(self._wait_if_paused)
                if self._cancel:
                    break
                self.active_side = side
                self._persist()
                await job.run(shared_df=df, evaluation_start=self.evaluation_start)
                if job.status == "error":
                    raise ValueError(f"{side.title()} search: {job.error}")
            self.status = "cancelled" if self._cancel else "done"
        except asyncio.CancelledError:
            self.cancel()
            self.status = "cancelled"
            raise
        except Exception as exc:
            self.status, self.error = "error", str(exc)
        finally:
            for job in self.sides.values():
                if job.status in ("pending", "pausing", "paused", "running"):
                    job.status = "cancelled" if self._cancel else "error"
                    job.error = self.error
            self._persist()

    def to_dict(self):
        result = super().to_dict()
        sides = {side: job.to_dict() for side, job in self.sides.items()}
        ready = all(job["best_summary"] is not None for job in sides.values())
        combined_pnl = sum(job["best_summary"]["total_pnl"] for job in sides.values()) if ready else None
        combined_summary = None
        if ready:
            count = sum(job["best_summary"]["trade_count"] for job in sides.values())
            win_rate = sum(job["best_summary"]["win_rate"] * job["best_summary"]["trade_count"]
                           for job in sides.values()) / count if count else 0.
            combined_summary = {"total_pnl": combined_pnl, "trade_count": count, "win_rate": win_rate}
        result.update(
            strategy_label="Advanced RSI Independent (Long + Short)", paired=True,
            long_credit=self.long_credit, short_credit=self.short_credit, active_side=self.active_side, sides=sides,
            iteration=sum(job["iteration"] for job in sides.values()), maxiter=self.maxiter*2,
            total_trials=self.n_trials*2 if self.method == "tpe" else None,
            evaluations=sum(job["evaluations"] for job in sides.values()),
            workers=self.sides[self.active_side].workers if self.active_side else 1,
            train_candle_count=max(job["train_candle_count"] for job in sides.values()),
            test_candle_count=max(job["test_candle_count"] for job in sides.values()),
            frozen_params={side: job["frozen_params"] for side, job in sides.items()},
            evaluation_metadata={"version": "optimizer-validation-v1", "evaluation_mode": self.evaluation_mode,
                                 "source": next(iter(sides.values()))["evaluation_metadata"].get("source", exchange),
                                 "sides": {side: job["evaluation_metadata"] for side, job in sides.items()}},
            promotion={"eligible": all(job["promotion"]["eligible"] for job in sides.values()),
                       "status": "validated" if all(job["promotion"]["eligible"] for job in sides.values()) else "rejected",
                       "target_source": "nobitex", "sides": {side: job["promotion"] for side, job in sides.items()}},
            best_pnl=combined_pnl, best_summary=combined_summary,
            current_delta=self.sides[self.active_side].current_delta if self.active_side else None,
            no_eligible_result=self.status == "done" and all(job["best_params"] is None for job in sides.values()),
            combined_pnl=combined_pnl,
            combined_test_pnl=sum(job["test_summary"]["total_pnl"] for job in sides.values())
                if all(job["test_summary"] is not None for job in sides.values()) else None,
        )
        return result


# ── Manager / registry ───────────────────────────────────────────────────────────

def _restore_training_summary(snapshot):
    """Recover pre-fix reporting from saved OHLC, without modifying storage."""
    if ("training_summary" in snapshot
            or snapshot.get("evaluation_metadata", {}).get("version") != "optimizer-validation-v1"
            or not snapshot.get("best_params")):
        return
    # Cache success or failure in the in-memory snapshot; never replay on each poll.
    snapshot["training_summary"] = None
    try:
        bars = (snapshot.get("chart_data") or {}).get("candles")
        if not bars or snapshot.get("evaluation_start") is None:
            raise ValueError("Saved training candles or evaluation boundary are unavailable")
        df = pd.DataFrame([{key.title(): bar[key] for key in ("open", "high", "low", "close")}
                           for bar in bars],
                          index=pd.to_datetime([bar["time"] for bar in bars], unit="s", utc=True))
        df.attrs["data_source"] = snapshot["evaluation_metadata"].get("source", "provided")
        result = evaluate_research(df, snapshot["strategy"], snapshot["credit"], snapshot["leverage"],
                                  PARAMS_CLASS[snapshot["strategy"]](**snapshot["best_params"]),
                                  snapshot.get("execution"), snapshot["evaluation_start"])
        saved = snapshot.get("best_trades")
        matches = saved is not None and len(saved) == len(result["trades"])
        if matches:
            for actual, previous in zip(result["trades"], saved):
                if (any(actual[key] != previous.get(key) for key in ("Position", "Start Time", "End Time"))
                        or any(not math.isclose(actual[key], float(previous[key]), rel_tol=1e-9, abs_tol=1e-8)
                               for key in ("Start Price", "End Price", "P&L ($)"))):
                    matches = False
                    break
        if not matches:
            raise ValueError("Replayed training trades differ from the saved trade log")
        result["summary"]["metadata"]["reconstructed_from_saved_chart"] = True
        snapshot["training_summary"] = result["summary"]
        if snapshot.get("validation_summary"):
            snapshot["best_summary"] = snapshot["validation_summary"]
            snapshot["best_pnl"] = snapshot["validation_summary"]["total_pnl"]
    except Exception as exc:
        snapshot["reporting_error"] = f"Training metrics could not be recovered: {exc}"


class OptimizerManager(ResearchPersistence):
    def _ensure_store(self):
        store = super()._ensure_store()
        for job in self._jobs.values():
            if not hasattr(job, "snapshot"):
                continue
            for result in [job.snapshot, *job.snapshot.get("sides", {}).values()]:
                _restore_training_summary(result)
                if "evaluation_mode" not in result:
                    result["evaluation_mode"] = "legacy_holdout" if result.get("train_pct") is not None else "legacy_in_sample"
                    result["promotion"] = {"eligible": False, "status": "legacy", "target_source": "nobitex",
                                           "reason": "Rerun with forward validation and Nobitex transfer before real promotion"}
        return store

    namespace = "optimizer"

    def __init__(self, store=None):
        self._injected_store = store
        self._loaded_path = None
        self._jobs: dict[str, OptimizerJob] = {}
        self._prefs: dict = self._load_prefs()

    # ── Preferences persistence ──────────────────────────────────────────────
    def _load_prefs(self) -> dict:
        defaults = {
            "symbol": "", "strategy": "", "timeframe": "240",
            "candles": _DEFAULT_CANDLES, "credit": 100.0, "leverage": 5,
            "min_price_change": 5.0,
            "min_win_rate": 50.0,
            "min_closed_trades": 30,
            "cores": 0,       # 0 = Auto
            "maxiter": _DE_MAXITER,
            "method": "de", "n_trials": _TPE_DEFAULT_TRIALS,
            "de_strategy": DEFAULT_DE_STRATEGY,
            "objective": "profit",
            "custom_bounds": None,
            "long_credit": 100.0, "short_credit": 100.0,
        }
        return defaults

    def _save_prefs(self) -> None:
        self._ensure_store().put("prefs", self.namespace, self._prefs)

    def save_prefs(self, symbol, strategy, timeframe, candles, credit, leverage,
                   cores=0, maxiter=_DE_MAXITER, objective="profit",
                   custom_bounds=None, min_price_change=5.0, min_win_rate=50.0,
                   min_closed_trades=30, long_credit=100.0, short_credit=100.0,
                   method="de", n_trials=_TPE_DEFAULT_TRIALS,
                   de_strategy=DEFAULT_DE_STRATEGY, evaluation_mode="in_sample") -> None:
        method, n_trials = _validate_search(method, n_trials)
        de_strategy = _validate_de_strategy(de_strategy)
        if evaluation_mode not in ("chronological", "in_sample"):
            raise ValueError("evaluation_mode must be 'chronological' or 'in_sample'")
        self._ensure_store()
        self._prefs = {
            "symbol": (symbol or "").lower().replace("/", "").strip(),
            "strategy": strategy if strategy in STRATEGY_MAP or strategy == INDEPENDENT_STRATEGY else "",
            "timeframe": timeframe if timeframe in _VALID_TIMEFRAMES else "240",
            "candles": _clamp_candles(candles),
            "credit": float(credit),
            "leverage": int(leverage),
            "cores": _clamp_cores(cores),
            "maxiter": max(5, min(5000, int(maxiter))) if maxiter else _DE_MAXITER,
            "method": method, "n_trials": n_trials,
            "de_strategy": de_strategy,
            "evaluation_mode": evaluation_mode,
            "objective": objective if objective in ("profit", "winrate", "balanced", "large_move") else "profit",
            "custom_bounds": custom_bounds if isinstance(custom_bounds, dict) else None,
            "min_price_change": max(0.1, float(min_price_change)) if min_price_change else 5.0,
            "min_win_rate": float(min_win_rate),
            "min_closed_trades": int(min_closed_trades),
            "long_credit": float(long_credit), "short_credit": float(short_credit),
        }
        self._save_prefs()

    def get_config(self) -> dict:
        self._ensure_store()
        return {
            **{key: value for key, value in self._prefs.items()
               if key not in ("balanced_cap", "balanced_weight", "long_fraction")},
            "available_strategies": [optimizer_strategy_spec(k) for k in STRATEGY_MAP if k in PARAM_BOUNDS]
                + [optimizer_strategy_spec(INDEPENDENT_STRATEGY)],
            "available_timeframes": TIMEFRAMES,
            "min_candles": _MIN_CANDLES,
            "max_candles": _MAX_CANDLES,
            "default_candles": _DEFAULT_CANDLES,
            "max_cores": _configured_worker_cap(),          # upper bound for the cores selector
            "default_cores": _resolve_workers(),     # what "Auto" resolves to right now
            "default_maxiter": _DE_MAXITER,
            "default_de_strategy": DEFAULT_DE_STRATEGY,
            "available_de_strategies": [{"value": value, "label": value} for value in DE_STRATEGIES],
            "default_n_trials": _TPE_DEFAULT_TRIALS,
            "min_n_trials": _TPE_MIN_TRIALS, "max_n_trials": _TPE_MAX_TRIALS,
            "available_methods": [{"value": "de", "label": "Differential Evolution"},
                                  {"value": "tpe", "label": "Multivariate TPE"}],
        }

    # ── Jobs ─────────────────────────────────────────────────────────────────
    def start_job(self, symbol, strategy, timeframe, candles, credit, leverage,
                  cores=0, maxiter=_DE_MAXITER, objective="profit",
                  custom_bounds=None, min_price_change=5.0, min_win_rate=50.0,
                  min_closed_trades=30, train_pct=None, execution=None, random_seed=None,
                  long_credit=100.0, short_credit=100.0,
                  method="de", n_trials=_TPE_DEFAULT_TRIALS,
                  de_strategy=DEFAULT_DE_STRATEGY, evaluation_mode=None) -> OptimizerJob:
        de_strategy = _validate_de_strategy(de_strategy)
        self._ensure_store()
        self._prune()
        job_class = IndependentOptimizerJob if strategy == INDEPENDENT_STRATEGY else OptimizerJob
        args = [symbol.lower().replace("/", "").strip()]
        if strategy == INDEPENDENT_STRATEGY:
            args.extend([timeframe, _clamp_candles(candles), float(long_credit), float(short_credit),
                         int(leverage), _clamp_cores(cores)])
        else:
            args.extend([strategy, timeframe, _clamp_candles(candles), float(credit), int(leverage), _clamp_cores(cores)])
        job = job_class(
            *args,
            maxiter=maxiter, custom_bounds=custom_bounds, objective=objective,
            min_price_change=min_price_change, min_win_rate=min_win_rate,
            min_closed_trades=min_closed_trades, train_pct=train_pct, execution=execution, random_seed=random_seed,
            method=method, n_trials=n_trials, de_strategy=de_strategy,
            evaluation_mode=evaluation_mode,
        )
        self._attach(job)
        job._task = asyncio.create_task(job.run())
        return job

    def get_job(self, job_id: str) -> OptimizerJob | None:
        self._ensure_store()
        return self._jobs.get(job_id)

    def _prune(self) -> None:
        if len(self._jobs) < _MAX_JOBS_KEPT:
            return
        finished = sorted(
            (j for j in self._jobs.values() if j.status in ("done", "cancelled", "error", "interrupted")),
            key=lambda j: j.created_at,
        )
        while len(self._jobs) >= _MAX_JOBS_KEPT and finished:
            key = finished.pop(0).id
            self._ensure_store().delete(self.namespace + "_jobs", key)
            self._jobs.pop(key, None)


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
    return min(n, _configured_worker_cap())


optimizer_manager = OptimizerManager()

