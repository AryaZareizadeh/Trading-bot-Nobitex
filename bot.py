import asyncio
import time
import uuid
import json
import os
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from config import Config
from data import DataFetcher
from candle_store import candle_close_time
from indicators import Indicators
from price_ai import STRATEGY_NAME as PRICE_AI_NAME, train_price_model, evaluate_price_model, train_and_evaluate
from agentic_ai import (STRATEGY_NAME as AGENTIC_AI_NAME, validate_timeframes as validate_ai_timeframes,
                        validate_execution as validate_agent_execution, train_agent, evaluate_agent,
                        probability_evidence, feature_matrix, decide as agent_decide, backtest as agent_backtest,
                        SECONDS as AGENT_SECONDS)
from strategy import (AdvRSICombinedStrategy, ExtremeReversalStrategy,
                      TrendFilteredMomentumStrategy, PARAMS_CLASS,
                      AdvRSILongStrategy, AdvRSIShortStrategy)

from settings import load_environment
load_environment()

try:
    from notifier import notify_if_relevant as _notify
except ImportError:
    def _notify(*_args, **_kwargs): pass

RESOLUTION_SECONDS: dict[str, int] = {
    "1": 60, "5": 300, "15": 900, "30": 1800,
    "60": 3600, "180": 10800, "240": 14400,
    "D": 86400, "W": 604800, "M": 2592000,
}

STRATEGY_MAP = {
    "AdvRSI": AdvRSICombinedStrategy,
    "AdvRSILong": AdvRSILongStrategy,
    "AdvRSIShort": AdvRSIShortStrategy,
    "TrendMomentum": TrendFilteredMomentumStrategy,
    "ExtremeReversal": ExtremeReversalStrategy,
}

_ORDER_MAX_ATTEMPTS = 5
_ORDER_TIMEOUT_BY_TF: dict[str, int] = {
    "1": 300, "5": 300, "15": 300, "30": 300,   # 5 min
    "60": 1800, "180": 1800, "240": 1800,         # 30 min
    "D": 5400,                                     # 90 min
}
_CLOSE_ORDER_TIMEOUT_BY_TF: dict[str, int] = {
    "1": 120, "5": 120, "15": 120, "30": 120,   # 2 min
    "60": 300, "180": 300, "240": 300,            # 5 min
    "D": 600,                                     # 10 min
}
_FILL_POLL_INTERVAL = 5       # seconds between fill-status checks
_ORDER_RECONCILE_INTERVAL = 30  # unresolved orders must progress between candle closes
_CANDLE_TRANSPORT_ERRORS = (requests.ConnectionError, requests.Timeout,
                            requests.exceptions.ChunkedEncodingError)
_POSITION_LOOKUP_RETRIES = 3  # retries when position not visible immediately after fill
_POSITION_LOOKUP_DELAY = 2    # seconds between each position lookup retry
_DUPLICATE_ORDER_SLEEP = 12   # seconds to sleep after DuplicateOrder (past Nobitex's 10s dedup window)
_WALLET_CACHE_SECONDS = 1800  # don't re-check the margin wallet more than once per 30 min

# Errors where retrying the same order will never help
_FATAL_ORDER_ERRORS = frozenset({
    "InsufficientBalance", "SmallOrder", "AmountUnavailable",
    "LeverageTooHigh", "LeverageUnavailable", "TradingUnavailable",
    "MarketClosed", "UnsupportedMarginSrc", "MarginClosed",
    "ExceedDlegationLimit",  # bot already capped to limit; immediate retry is futile (API typo: missing 'e')
})

ALEXNET_NAME = "AlexNet"


def resolve_alexnet_artifact(store, model_dir, model_id, symbol, timeframe):
    """Resolve a ready tenant-owned checkpoint, never an operator-supplied path."""
    if not isinstance(model_id, str) or re.fullmatch(r"[0-9a-f]{32}", model_id) is None:
        raise ValueError("AlexNet requires a saved model ID from the Model Trainer tab")
    record = store.get("trained_models", model_id) if store is not None else None
    if not record or record.get("status") != "ready":
        raise ValueError("Saved AlexNet model is unavailable; select a completed model in Model Trainer")
    if record.get("id") != model_id or record.get("architecture") != "alexnet1d-v1":
        raise ValueError("Saved AlexNet registry identity or architecture is invalid")
    if str(record.get("symbol", "")).lower() != symbol.lower() or str(record.get("timeframe", "")) != timeframe:
        raise ValueError("Saved AlexNet model must match the bot symbol and timeframe")
    if record.get("validation_passed") is not True:
        raise ValueError("AlexNet validation must beat the chronological baseline before use in a bot")
    if record.get("source") != "nobitex":
        raise ValueError("AlexNet bots require a model trained on Nobitex candles")
    directory = Path(model_dir).resolve()
    path = (directory / f"{model_id}.pt").resolve()
    if path.parent != directory or not path.is_file():
        raise ValueError("Saved AlexNet checkpoint is missing; train and select a new model")
    return record, path


def _format_price(price: float) -> str:
    """Format price with enough decimal places to never round sub-cent coins to zero."""
    if price >= 0.01:
        return f"{price:.2f}"
    elif price >= 0.000001:
        return f"{price:.8f}"
    else:
        return f"{price:.12f}"


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
    custom_params: Optional[dict] = None   # optimized indicator inputs to use instead of defaults
    execution: Optional[dict] = None
    ai_timeframes: Optional[list[str]] = None
    long_credit: Optional[float] = None
    short_credit: Optional[float] = None
    ai_model_id: Optional[str] = None


class Bot:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        if cfg.mode not in ("simulation", "real"):
            raise ValueError(f"Invalid mode: {cfg.mode!r}")
        if (cfg.strategy_name not in STRATEGY_MAP and cfg.strategy_name not in (PRICE_AI_NAME, AGENTIC_AI_NAME, ALEXNET_NAME)) or cfg.timeframe not in RESOLUTION_SECONDS:
            raise ValueError("Unknown strategy or timeframe")
        if cfg.strategy_name == PRICE_AI_NAME:
            if cfg.mode != "simulation":
                raise ValueError("PriceAI is available in simulation mode only")
            if cfg.timeframe not in ("60", "240"):
                raise ValueError("PriceAI supports 1-hour and 4-hour candles")
            from price_ai import validate_price_ai_execution
            validate_price_ai_execution(cfg.execution)
        if cfg.strategy_name == AGENTIC_AI_NAME:
            cfg.ai_timeframes = validate_ai_timeframes(cfg.timeframe, cfg.ai_timeframes)
            validate_agent_execution(cfg.execution)
            if cfg.leverage > 5:
                raise ValueError("AgenticAI leverage is limited to 5x")
        if cfg.strategy_name == ALEXNET_NAME:
            if cfg.mode != "simulation":
                raise ValueError("AlexNet is available in simulation mode only")
            if not isinstance(cfg.ai_model_id, str) or re.fullmatch(r"[0-9a-f]{32}", cfg.ai_model_id) is None:
                raise ValueError("AlexNet requires a saved model ID from the Model Trainer tab")
            if cfg.timeframe not in ("15", "60", "240", "D"):
                raise ValueError("AlexNet supports 15-minute, 1-hour, 4-hour and daily candles")
            from price_ai import validate_price_ai_execution
            try:
                validate_price_ai_execution(cfg.execution)
            except ValueError as exc:
                raise ValueError(str(exc).replace("PriceAI", ALEXNET_NAME)) from exc
        if not (cfg.credit > 0 and 1 <= cfg.leverage <= 100):
            raise ValueError("Credit and leverage must be positive and within limits")
        self._store = None
        self._model_dir = None
        self._account = None
        self._last_processed_ts = None
        self.status = BotStatus.IDLE
        self.df: Optional[pd.DataFrame] = None
        self.open_positions: list[dict] = []
        self.completed_trades: list[dict] = []
        self.total_pnl: float = 0.0
        self.balance: float = cfg.credit
        self.current_price: Optional[float] = None
        self.last_updated: Optional[datetime] = None
        self.error: Optional[str] = None
        self.created_at: Optional[datetime] = datetime.now(timezone.utc)
        self.history_epoch = uuid.uuid4().hex
        self._task: Optional[asyncio.Task] = None
        self._stop_event: asyncio.Event = asyncio.Event()
        self._lifecycle_lock: asyncio.Lock = asyncio.Lock()
        self._live_from_ts: Optional[pd.Timestamp] = None

        # Performance metrics
        self.win_rate: float = 0.0
        self.max_drawdown: float = 0.0
        self.ai_model_metadata: dict = {}
        self.ai_decisions: list[dict] = []
        self._ai_model = None
        self._ai_frames: dict[str, pd.DataFrame] = {}
        self._agent_signal: Optional[str] = None
        self.balance_history: list[float] = []   # balance at each completed trade, last 60 pts
        self.equity_curve: list[dict] = []
        self.closed_trade_count: int = 0         # trades that have actually closed (excludes pending open-position records)

        # Activity log (newest appended, capped at 200)
        self.activity_log: list[dict] = []

        # Real-trading state (unused in simulation mode)
        self._exchange_client = None
        self._real_state: str = "none"
        self._exchange_position_id: Optional[int] = None
        self._real_positions: list[dict] = []
        self._pending_order: Optional[dict] = None
        self._wallet_cache: Optional[dict] = None
        self._wallet_cache_ts: float = 0.0
        # Actual owned fill and incremental exit progress. Retain it until the
        # exchange confirms settlement, including while a close is uncertain.
        self._seed_position: Optional[dict] = None

    def snapshot(self):
        fields = ("open_positions", "completed_trades", "total_pnl", "balance", "current_price",
                  "win_rate", "max_drawdown", "balance_history", "equity_curve", "closed_trade_count", "activity_log",
                  "_real_state", "_exchange_position_id", "_real_positions", "_pending_order",
                  "_seed_position", "_account", "error", "ai_model_metadata", "ai_decisions", "history_epoch")
        raw = {name: getattr(self, name) for name in fields}
        raw["bot_id"] = self.id
        raw["market"] = self.cfg.symbol.lower()
        raw["desired_status"] = self.status.value
        for name in ("_live_from_ts", "_last_processed_ts", "last_updated", "created_at"):
            value = getattr(self, name)
            raw[name] = str(value) if value is not None else None
        def pack_frame(frame):
            return {"index": [ts.isoformat() for ts in frame.index], "columns": list(frame.columns),
                    "data": frame.to_numpy().tolist(), "attrs": frame.attrs}
        raw["candles"] = pack_frame(self.df) if self.df is not None else None
        raw["ai_candles"] = {tf: pack_frame(frame) for tf, frame in self._ai_frames.items()
                             if tf != self.cfg.timeframe} if self.cfg.strategy_name == AGENTIC_AI_NAME and self.cfg.mode == "simulation" else {}
        numeric_paths = []
        def durable(value, path=()):
            if isinstance(value, float):
                numeric_paths.append(list(path))
                return str(value)
            if isinstance(value, list):
                return [durable(v, (*path, i)) for i, v in enumerate(value)]
            if isinstance(value, dict):
                return {k: durable(v, (*path, k)) for k, v in value.items()}
            return value
        saved = durable(raw)
        saved["numeric_paths"] = numeric_paths
        return saved

    def restore(self, raw):
        import copy
        raw = copy.deepcopy(raw)
        self.history_epoch = raw.get('history_epoch', 'legacy')
        if not raw.get('created_at'):
            self.created_at = None
        for path in raw.pop("numeric_paths", []):
            parent = raw
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = float(parent[path[-1]])
        for name in ("open_positions", "completed_trades", "activity_log", "_real_state",
                     "_exchange_position_id", "_real_positions", "_pending_order", "_seed_position", "_account", "error"):
            if name in raw:
                setattr(self, name, raw[name])
        for name in ("total_pnl", "balance", "win_rate", "max_drawdown"):
            setattr(self, name, float(raw.get(name, getattr(self, name))))
        self.current_price = float(raw["current_price"]) if raw.get("current_price") is not None else None
        self.closed_trade_count = int(raw.get("closed_trade_count", 0))
        self.ai_model_metadata = raw.get("ai_model_metadata", {})
        self.ai_decisions = raw.get("ai_decisions", [])
        self._ai_model = None
        self._ai_frames = {}
        self._agent_signal = None
        self.balance_history = [float(v) for v in raw.get("balance_history", [])]
        self.equity_curve = raw.get("equity_curve", [])
        for pos in self.open_positions:
            pos["entry_price"] = float(pos["entry_price"])
        if self._seed_position:
            self._seed_position["entry_price"] = float(self._seed_position["entry_price"])
        for name in ("_live_from_ts", "_last_processed_ts"):
            setattr(self, name, pd.Timestamp(raw[name]) if raw.get(name) else None)
        for name in ("last_updated", "created_at"):
            if raw.get(name):
                setattr(self, name, datetime.fromisoformat(raw[name]))
        if raw.get("candles"):
            candles = raw["candles"]
            self.df = pd.DataFrame(candles["data"], columns=candles["columns"], index=pd.to_datetime(candles["index"])).astype(float)
            self.df.index.name = "Date"
            self.df.attrs = candles.get("attrs", {})
        if self.cfg.strategy_name == AGENTIC_AI_NAME and self.cfg.mode == "simulation":
            for tf, candles in raw.get("ai_candles", {}).items():
                frame = pd.DataFrame(candles["data"], columns=candles["columns"],
                                     index=pd.to_datetime(candles["index"])).astype(float)
                frame.index.name = "Date"
                frame.attrs = candles.get("attrs", {})
                self._ai_frames[tf] = frame
        self.status = BotStatus.IDLE

    def _persist(self):
        if self._store is not None:
            from bot_history import save_simulation_history
            key, snapshot = self.runtime_record()
            with self._store.connection() as conn:
                save_simulation_history(getattr(self, 'parent', self), conn)
                self._store.write(conn, "runtime", key, snapshot)

    def runtime_record(self):
        return self.id, self.snapshot()

    def execution_bots(self):
        return [self]

    def has_owned_position(self):
        return any(bot._exchange_position_id is not None for bot in self.execution_bots())

    def can_share_position(self, position):
        return False

    def _account_key(self):
        if getattr(self, "_tenant_account_id", None):
            environment = "testnet" if self._testnet_factory and self._testnet_factory() else "production"
            account = environment + ":" + self._tenant_account_id
            if self._account and self._account != account:
                raise RuntimeError("Account/environment changed; reconcile existing bot state first")
            self._account = account
            return account
        environment = "testnet" if os.getenv("NOBITEX_TESTNET", "false").lower() == "true" else "production"
        account = environment + ":" + os.getenv("TRADER_ACCOUNT_ID", "default")
        if self._account and self._account != account:
            raise RuntimeError("Account/environment changed; reconcile existing bot state first")
        self._account = account
        return account

    def unresolved_intents(self):
        if self._store is None:
            return []
        return [value for value in self._store.list("orders").values()
                if value["bot_id"] == self.id and value["state"] not in ("resolved", "rejected")]

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log_event(self, event: str, detail: str = "") -> None:
        from trading_audit import audit_record, record_event
        record = audit_record(self.id, event, detail)
        record_event(self._store, record)
        self.activity_log.append({key: record[key] for key in ("time", "event", "detail")})
        if len(self.activity_log) > 200:
            self.activity_log = self.activity_log[-200:]
        _notify(self.cfg.mode, record["event"], self.cfg.name or self.id, record["detail"])

    # ── Reconfiguration ───────────────────────────────────────────────────────

    def reconfigure(self, new_cfg: BotConfig, *, log_event=True) -> None:
        """Reset bot to a fresh state with a new config. Call only when stopped."""
        self.cfg = new_cfg
        self.history_epoch = uuid.uuid4().hex
        self.df = None
        self.open_positions = []
        self.completed_trades = []
        self.total_pnl = 0.0
        self.balance = new_cfg.credit
        self.current_price = None
        self.last_updated = None
        self.error = None
        self._live_from_ts = None
        self._last_processed_ts = None
        self.win_rate = 0.0
        self.max_drawdown = 0.0
        self.ai_model_metadata = {}
        self.ai_decisions = []
        self._ai_model = None
        self._ai_frames = {}
        self._agent_signal = None
        self.balance_history = []
        self.equity_curve = []
        self.closed_trade_count = 0
        self._exchange_client = None
        self._real_state = "none"
        self._exchange_position_id = None
        self._real_positions = []
        self._pending_order = None
        self._wallet_cache = None
        self._wallet_cache_ts = 0.0
        self._seed_position = None
        self.status = BotStatus.IDLE
        if log_event:
            self._log_event(
                "reconfigured",
                f"{new_cfg.symbol} | {new_cfg.strategy_name} | {new_cfg.timeframe} | {new_cfg.leverage}x | {new_cfg.mode}",
            )

    @property
    def id(self) -> str:
        return self.cfg.id

    def _build_custom_params(self):
        """Build the strategy's params dataclass from cfg.custom_params, or None to use defaults."""
        if not self.cfg.custom_params or self.cfg.strategy_name not in PARAMS_CLASS:
            return None
        try:
            return PARAMS_CLASS[self.cfg.strategy_name](**self.cfg.custom_params)
        except Exception as exc:
            self._log_event("warning", f"custom_params failed to load ({exc}); using strategy defaults")
            return None

    # ── Real-trading helpers ───────────────────────────────────────────────────

    def _parse_symbol(self) -> tuple[str, str]:
        sym = self.cfg.symbol.lower()
        if sym.endswith("usdt"):
            return sym[:-4], "usdt"
        raise ValueError(f"Cannot infer currency pair from symbol {sym!r}")

    def _strategy_signal(self) -> str:
        if self.cfg.strategy_name == AGENTIC_AI_NAME:
            return self._agent_signal or "none"
        # A reconnected position's real entry_time predates this run's
        # _live_from_ts by design (we don't replay the offline gap — see
        # _sync_exchange), so it needs to be recognized directly rather than
        # via the entry >= _live_from_ts check below. Its exit request remains
        # attached to the owned fill until the exchange confirms settlement.
        if self._seed_position is not None:
            if self._seed_position.get("exit_requested"):
                return "none"
            direction = self._seed_position["direction"]
            if any(p["direction"] == direction for p in self.open_positions):
                return "long" if direction == "Long" else "short"

        if self._live_from_ts is None:
            return "none"
        for pos in self.open_positions:
            entry = pd.Timestamp(pos["entry_time"])
            if entry >= self._live_from_ts:
                return "long" if pos["direction"] == "Long" else "short"
        return "none"

    def _normalize_positions(self, raw: list[dict]) -> list[dict]:
        """Convert only this bot's owned exchange position for display."""
        try:
            src, dst = self._parse_symbol()
        except ValueError:
            return []
        result = []
        for p in raw:
            if self._exchange_position_id is None or str(p.get("id")) != str(self._exchange_position_id):
                continue
            if p.get("srcCurrency") != src or p.get("dstCurrency") != dst:
                continue
            if p.get("status") != "Open":
                continue
            direction = "Long" if p.get("side") == "buy" else "Short"
            result.append({
                "direction": direction,
                "entry_price": float(p.get("entryPrice") or 0),
                "entry_time": p.get("openedAt", ""),
                "expiration_date": p.get("expirationDate"),
                "unrealized_pnl": float(p.get("unrealizedPNL") or 0),
                "unrealized_pnl_pct": float(p.get("unrealizedPNLPercent") or 0),
                "liquidation_price": float(p.get("liquidationPrice") or 0),
                "mark_price": float(p.get("markPrice") or 0),
                "margin_ratio": float(p.get("marginRatio") or 0),
                "collateral": float(p.get("collateral") or 0),
                "delegated_amount": float(p.get("delegatedAmount") or 0),
                "exchange_id": p.get("id"),
            })
        return result

    def _fetch_delegation_limit(
        self, client, symbol: str, side: str, leverage: int
    ) -> Optional[float]:
        """Remaining delegation capacity for this side+leverage, or None if unknown.

        Unit is dst currency for buy orders, src currency for sell orders.
        Rate-limited to 12/min, so this is fetched once per order placement.
        """
        try:
            lim_data = client.delegation_limit(symbol)
            for entry in lim_data.get(side, []):
                if abs(float(entry.get("leverage", 0)) - leverage) < 0.01:
                    limit_val = float(entry.get("limit", 0))
                    unit = symbol[-4:].upper() if side == "buy" else symbol[:-4].upper()
                    self._log_event(
                        "info", f"Delegation limit for {side} {leverage}x: {limit_val} {unit}"
                    )
                    return limit_val
            self._log_event(
                "warning", f"No delegation limit entry found for {side} {leverage}x on {symbol}"
            )
        except Exception as exc:
            self._log_event("warning", f"Delegation limit fetch failed: {exc} — will use full amount")
        return None

    def _compute_order_amount(
        self, src: str, side: str, leverage: int, price: float, limit_val: Optional[float]
    ) -> str:
        """Order size in src-currency units, capped to the delegation limit."""
        amount = round(self.cfg.credit * leverage / price, 6)
        if limit_val is not None:
            # Convert limit to src-currency units so we can compare directly with amount.
            max_amount = round(limit_val / price, 6) if side == "buy" else round(limit_val, 6)
            if amount > max_amount:
                self._log_event(
                    "warning",
                    f"order amount {amount:.6f} {src} exceeds delegation limit"
                    f" — capping to {max_amount:.6f} {src}",
                )
                amount = max_amount
        return f"{amount:.6f}"

    def _find_position_by_symbol(
        self, client, src: str, dst: str, side: str, existing_ids: set[int]
    ) -> Optional[dict]:
        """Locate the open position for this market+side after an order fills.

        Matches by srcCurrency/dstCurrency/side rather than relying on a brand-new
        position id: Nobitex may merge the fill into an existing position in the same
        market, in which case no new id appears (the original "not confirmed" bug).
        Prefers a freshly-created id but falls back to any matching open position.
        positions("active") is rate-limited (30/min), so this is called once per fill.
        """
        try:
            positions = client.positions("active")
        except Exception as exc:
            self._log_event("warning", f"Position lookup failed: {exc}")
            return None
        matches = [
            p for p in positions
            if p.get("srcCurrency") == src
            and p.get("dstCurrency") == dst
            and p.get("side") == side
            and p.get("status") == "Open"
        ]
        if not matches:
            return None
        for p in matches:
            if p.get("id") not in existing_ids:
                return p
        return matches[0]

    async def _place_single_order(self, client, src, dst, side, leverage):
        from exchange_execution import OrderExecutor
        symbol = f"{src}{dst}".upper()
        limit = await asyncio.to_thread(self._fetch_delegation_limit, client, symbol, side, leverage)
        price = float(await asyncio.to_thread(client.live_price, symbol))
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Live opening price must be finite and positive")
        amount = self._compute_order_amount(src, side, leverage, price, limit)
        return await OrderExecutor(self, client).open(src, dst, side, amount, _format_price(price), leverage,
                                                     _ORDER_TIMEOUT_BY_TF.get(self.cfg.timeframe, 300))

    def _ensure_exchange_client(self) -> bool:
        """Lazily construct the Nobitex client. Returns True if one is available."""
        if self._exchange_client is not None:
            return True
        try:
            if getattr(self, "_client_factory", None) is not None:
                self._exchange_client = self._client_factory()
                return True
            import os
            from nobitex_client import NobitexClient
            testnet = os.getenv("NOBITEX_TESTNET", "false").lower() == "true"
            self._exchange_client = NobitexClient(testnet=testnet)
            return True
        except Exception as exc:
            self.error = f"Exchange client init failed: {exc}"
            return False

    async def _poll_order_until_filled(
        self, client, order_id: int, order_timeout: int
    ) -> tuple[str, float]:
        """Poll a close order until Done/Canceled/timeout. Returns (outcome, matched_amount).
        outcome ∈ {"done", "canceled", "timeout"}
        On CancelledError: re-raises WITHOUT canceling the order (leave close on exchange).
        On timeout: cancels the unfilled order so next sync can recompute remaining cleanly.
        """
        started_at = time.monotonic()
        matched = 0.0

        while True:
            await asyncio.sleep(_FILL_POLL_INTERVAL)
            elapsed = time.monotonic() - started_at

            try:
                info = await asyncio.to_thread(client.order_status, order_id)
            except asyncio.CancelledError:
                raise  # bot stopping — do NOT cancel the close order
            except Exception as exc:
                self._log_event("warning", f"Close order #{order_id} status check failed: {exc}")
                if elapsed >= order_timeout:
                    break  # fall through to timeout/cancel path
                continue

            matched = float(info.get("matchedAmount") or 0)
            status = info.get("status", "Active")

            if status == "Done":
                self._log_event("order_filled", f"Close order #{order_id} fully filled")
                return "done", matched

            if status == "Canceled":
                self._log_event("warning", f"Close order #{order_id} cancelled by exchange (matched={matched})")
                return "canceled", matched

            if elapsed >= order_timeout:
                break

        # Timeout — cancel the unfilled remainder so liabilityInOrder drops to 0
        try:
            await asyncio.to_thread(client.cancel_order, order_id)
            self._log_event("warning", f"Close order #{order_id} timed out — cancelled for clean retry")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_event("warning", f"Cancel of timed-out close order #{order_id} failed: {exc}")
        return "timeout", matched

    def _get_margin_wallet(self, client) -> dict:
        """Return the margin wallet, cached for _WALLET_CACHE_SECONDS.

        The wallet only needs checking before placing a new order, and its
        balance doesn't move quickly enough to justify hitting the API every
        sync — so reuse the last result until it's more than 30 min old.
        """
        now = time.time()
        if self._wallet_cache is not None and (now - self._wallet_cache_ts) < _WALLET_CACHE_SECONDS:
            return self._wallet_cache
        wallet = client.wallets("margin")
        self._wallet_cache = wallet
        self._wallet_cache_ts = now
        return wallet

    async def _close_exchange_position(self, client, price_str):
        from exchange_execution import OrderExecutor
        pos_id = self._exchange_position_id
        try:
            pos = await asyncio.to_thread(client.position, pos_id)
            self._validate_owned_position(pos, pos_id)
            if pos.get("status") != "Open":
                from position_settlement import settled_position
                settled = await settled_position(client, pos)
                if settled is None:
                    self._log_event("info", "Exchange position awaits final settlement")
                    return False
                self._exchange_position_id = None
                self._real_state = "none"
                self._seed_position = None
                self._real_positions = []
                with self._store.connection() as conn:
                    self._store.write(conn, "closed_positions", f"{self._account_key()}:{pos_id}",
                                      {"bot_id": self.id, "position": settled})
                    key, snapshot = self.runtime_record()
                    self._store.write(conn, "runtime", key, snapshot)
                return True
            remaining = float(pos.get("liability") or 0) - float(pos.get("liabilityInOrder") or 0)
            if remaining <= 0:
                self._log_event("info", "Close is pending or liability is unavailable; reconciliation required")
                return False
            try:
                price_str = _format_price(await asyncio.to_thread(client.live_price, self.cfg.symbol.upper()))
            except Exception:
                pass
            done = await OrderExecutor(self, client).close(pos_id, f"{remaining:.10f}", price_str,
                                                          _CLOSE_ORDER_TIMEOUT_BY_TF.get(self.cfg.timeframe, 120))
            if done:
                self._log_event("position_closed", f"Position {pos_id} closed")
            return done
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"Close reconciliation required: {type(exc).__name__}"
            self._persist()
            return False

    async def _sync_exchange(self):
        if self.cfg.mode != "real" or not self._ensure_exchange_client():
            return
        from exchange_execution import OrderExecutor
        client = self._exchange_client
        try:
            executor = OrderExecutor(self, client)
            if not await executor.reconcile():
                self.error = "Unresolved exchange order: reconcile before trading"
                self._persist()
                return
            active = await asyncio.to_thread(client.positions, "active")
            self._real_positions = self._normalize_positions(active)
            tracked = None
            if self._exchange_position_id is not None:
                if self._store.owner(self._account_key(), self._exchange_position_id) != self.id:
                    raise RuntimeError("Recorded exchange position has no matching ownership")
                tracked = next((p for p in active if str(p.get("id")) == str(self._exchange_position_id)), None)
                if tracked is None or tracked.get("status") != "Open":
                    closed = await asyncio.to_thread(client.position, self._exchange_position_id)
                    self._validate_owned_position(closed, self._exchange_position_id)
                    if closed.get("status") == "Open":
                        raise RuntimeError("Exchange position visibility is inconsistent")
                    from position_settlement import settled_position
                    closed = await settled_position(client, closed)
                    if closed is None:
                        raise RuntimeError("Exchange position awaits final settlement")
                    settled_id = self._exchange_position_id
                    self._exchange_position_id = None
                    self._seed_position = None
                    self._real_state = "none"
                    # Do not reopen a manually closed position from a stale signal.
                    if self.df is not None:
                        self._live_from_ts = self._next_candle_open(self.df.index[-1])
                    self.open_positions = []
                    if self.cfg.strategy_name == AGENTIC_AI_NAME:
                        self._agent_signal = None
                    with self._store.connection() as conn:
                        self._store.write(conn, "closed_positions", f"{self._account_key()}:{settled_id}",
                                          {"bot_id": self.id, "position": closed})
                        key, snapshot = self.runtime_record()
                        self._store.write(conn, "runtime", key, snapshot)
                    return
                from position_limits import should_close_before_expiry
                if should_close_before_expiry(tracked):
                    self._log_event("position_expiring", f"Exchange position {self._exchange_position_id} is nearing expiration")
                    if not await self._close_exchange_position(client, _format_price(self.current_price)):
                        return
                    self._seed_position = None
                    self._agent_signal = None
                    self._real_state = "none"
                    self.open_positions = []
                    self._live_from_ts = self._next_candle_open(self.df.index[-1]) if self.df is not None else None
                    self._persist()
                    return
            if self.cfg.strategy_name == AGENTIC_AI_NAME and self._agent_signal is None:
                self._persist()
                return
            signal = self._strategy_signal()
            if signal == self._real_state:
                self._persist()
                return
            src, dst = self._parse_symbol()
            if self._exchange_position_id is not None:
                if not await self._close_exchange_position(client, _format_price(self.current_price)):
                    return
                self._seed_position = None
            if signal != "none":
                wallet = await asyncio.to_thread(client.wallets, "margin")
                usdt = wallet.get("USDT") or wallet.get("usdt") or {}
                if float(usdt.get("balance") or 0) - float(usdt.get("blocked") or 0) < self.cfg.credit:
                    raise RuntimeError("Insufficient free margin")
                await self._place_single_order(client, src, dst, "buy" if signal == "long" else "sell", self.cfg.leverage)
            else:
                self._real_state = "none"
            self.error = None
            self._persist()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"Exchange reconciliation required: {exc}"
            self._log_event("warning", self.error)
            self._persist()

    async def _reconcile_pending_orders(self):
        """Recover existing orders without evaluating or submitting a stale signal."""
        if self.cfg.mode != "real" or not self._ensure_exchange_client():
            return
        from exchange_execution import OrderExecutor
        try:
            if await OrderExecutor(self, self._exchange_client).reconcile():
                if self.error and self.error.startswith(("Unresolved exchange order:",
                                                        "Exchange reconciliation required:",
                                                        "Close reconciliation required:")):
                    self.error = None
            else:
                self.error = "Unresolved exchange order: reconcile before trading"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"Exchange reconciliation required: {exc}"
            self._log_event("warning", self.error)
        self._persist()

    def _validate_owned_position(self, position, position_id):
        src, dst = self._parse_symbol()
        expected_side = "buy" if self._real_state == "long" else "sell"
        if (str(position.get("id")) != str(position_id)
                or position.get("srcCurrency") != src or position.get("dstCurrency") != dst
                or position.get("side") != expected_side):
            raise RuntimeError("Exchange position does not match the recorded ownership")

    def _make_config(self) -> Config:
        c = Config()
        c.symbol = self.cfg.symbol
        c.interval = self.cfg.timeframe
        c.limit = 4200 if self.cfg.strategy_name == AGENTIC_AI_NAME else 1000
        if self.cfg.strategy_name == ALEXNET_NAME:
            c.limit = max(c.limit, int(self.ai_model_metadata.get("lookback", 96)) +
                          int(self.ai_model_metadata.get("horizon_candles", 4)) + 100)
        c.leverage = self.cfg.leverage
        c.trade_size = self.cfg.credit
        c.initial_capital = self.cfg.credit
        return c

    def _validate_saved_model(self):
        if self.cfg.strategy_name != ALEXNET_NAME:
            return None
        if self._model_dir is None:
            raise ValueError("AlexNet model storage is unavailable; create the bot through Model Trainer")
        return resolve_alexnet_artifact(self._store, self._model_dir, self.cfg.ai_model_id,
                                       self.cfg.symbol, self.cfg.timeframe)

    def _ensure_alexnet_model(self):
        record, path = self._validate_saved_model()
        if self._ai_model is None:
            from alexnet_model import load_checkpoint
            model = load_checkpoint(path)
            metadata = model.metadata
            if not isinstance(metadata, dict):
                raise ValueError("Saved AlexNet checkpoint metadata is invalid")
            if metadata.get("id") != record["id"] or metadata.get("architecture") != record["architecture"]:
                raise ValueError("Saved AlexNet checkpoint does not match its registered identity and architecture")
            if (str(metadata.get("symbol", "")).lower() != self.cfg.symbol.lower()
                    or str(metadata.get("timeframe", "")) != self.cfg.timeframe
                    or metadata.get("source") != "nobitex"):
                raise ValueError("Saved AlexNet checkpoint does not match its registered market and source")
            if metadata.get("validation_passed") is not True:
                raise ValueError("AlexNet checkpoint validation did not beat the chronological baseline")
            if (metadata.get("lookback") != record.get("lookback")
                    or metadata.get("horizon_candles") != record.get("horizon")):
                raise ValueError("Saved AlexNet checkpoint does not match its registered prediction settings")
            for name in ("training_window_end", "validation_window_end", "test_window_start"):
                if name in record and metadata.get(name) != record[name]:
                    raise ValueError("Saved AlexNet checkpoint does not match its registered training window")
            self._ai_model = model
            self.ai_model_metadata = {**metadata, "model_id": self.cfg.ai_model_id}
            self._log_event("ai_model_loaded", f"AlexNet | {self.cfg.ai_model_id} | saved checkpoint")
        return self._ai_model

    def _alexnet_closed_frame(self, frame):
        """Exchange indexes name candle opens; inference only observes completed bars."""
        times = pd.to_datetime(frame.index, utc=True)
        closed = times + pd.Timedelta(seconds=RESOLUTION_SECONDS[self.cfg.timeframe]) <= pd.Timestamp.now(tz="UTC")
        return frame.loc[closed].copy()

    def _alexnet_evaluation_boundary(self, frame, live_boundary=None):
        metadata = self._ensure_alexnet_model().metadata
        # Test candles never fit the network, scaler or selected checkpoint.
        # The first test row remains a legitimate held-out evaluation boundary.
        test_start = metadata.get("test_window_start")
        if test_start:
            earliest = pd.Timestamp(test_start)
        else:
            ends = [pd.Timestamp(metadata[name]) for name in
                    ("training_window_end", "validation_window_end") if metadata.get(name)]
            if not ends:
                raise ValueError("AlexNet checkpoint has no chronological evaluation boundary")
            earliest = max(pd.to_datetime(ends, utc=True)) + pd.Timedelta(seconds=RESOLUTION_SECONDS[self.cfg.timeframe])
        index = pd.to_datetime(frame.index, utc=True)
        boundary = int(index.searchsorted(pd.to_datetime(earliest, utc=True)))
        # Never start before the saved model's input history is complete.
        boundary = max(boundary, int(metadata["lookback"]) - 1)
        if live_boundary is not None:
            boundary = max(boundary, live_boundary)
        return min(boundary, len(frame))

    def _evaluate_alexnet(self, frame, start_idx):
        """Use the existing causal execution policy with saved CNN forecasts."""
        import numpy as np
        from alexnet_model import predict_alexnet
        from execution_model import simulate_signals
        from price_ai import (_cost_bps, MIN_EDGE_BUFFER_BPS, MAX_HOLD_CANDLES,
                              MAX_POSITION_LOSS_FRACTION)

        model = self._ensure_alexnet_model()
        forecasts = np.asarray(predict_alexnet(model, frame), dtype=float)
        if forecasts.shape != (len(frame),):
            raise ValueError("AlexNet predictions must align with the completed candle history")
        cost = _cost_bps(self.cfg.execution)
        threshold = cost + MIN_EDGE_BUFFER_BPS
        horizon = int(model.metadata["horizon_candles"])
        decisions = []

        def decide(i, positions):
            forecast = float(forecasts[i])
            action, reason, entry, exits = "hold", "forecast below entry threshold", None, []
            if positions:
                position = positions[0]
                direction = position["direction"]
                sign = 1 if direction == "Long" else -1
                signed_change = sign * (float(frame.Close.iloc[i]) / position["entry_price"] - 1)
                if signed_change * self.cfg.leverage <= -MAX_POSITION_LOSS_FRACTION:
                    reason = "position loss limit reached at candle close"
                elif i - position["entry_idx"] >= MAX_HOLD_CANDLES:
                    reason = "maximum holding period reached"
                elif math.isfinite(forecast) and sign * forecast < -cost / 2:
                    reason = "forecast turned against open position"
                else:
                    reason = "forecast supports holding open position"
                if reason != "forecast supports holding open position":
                    exits, action = [direction], "exit_" + direction.lower()
            elif not math.isfinite(forecast):
                reason = "insufficient completed history"
            elif forecast > threshold:
                entry, action, reason = "Long", "enter_long", "forecast exceeds estimated round-trip cost and buffer"
            elif forecast < -threshold:
                entry, action, reason = "Short", "enter_short", "forecast exceeds estimated round-trip cost and buffer"
            close = float(frame.Close.iloc[i])
            predicted = close * math.exp(forecast / 10_000) if math.isfinite(forecast) and abs(forecast) < 1_000_000 else None
            decisions.append({"candle_time": str(frame.index[i]), "action": action,
                              "reason": reason, "forecast_return_bps": round(forecast, 4) if math.isfinite(forecast) else None,
                              "entry_threshold_bps": round(threshold, 4), "model_version": model.metadata["model_version"],
                              "model_id": self.cfg.ai_model_id, "horizon_candles": horizon,
                              "predicted_price": predicted if predicted is not None and math.isfinite(predicted) else None})
            return entry, exits

        result = simulate_signals(frame, decide, self.cfg.credit, self.cfg.leverage,
                                  self.cfg.execution, start_idx)
        result["decisions"] = decisions
        result["metadata"].update(strategy=ALEXNET_NAME, evaluation="saved_model_held_out",
                                  model=self.ai_model_metadata)
        return result

    def _run_agent_strategy(self) -> None:
        primary = self.cfg.timeframe
        selected = self.cfg.ai_timeframes
        self._ai_frames[primary] = self.df
        boundary = int(self.df.index.searchsorted(self._live_from_ts)) if self._live_from_ts is not None else len(self.df)
        if self._ai_model is None:
            self._ai_model = train_agent(self._ai_frames, primary, selected, self.cfg.execution, boundary)
            self.ai_model_metadata = self._ai_model.metadata
            self._log_event("ai_model_trained", f"{self.ai_model_metadata['model_version']} | {boundary} primary candles")
        if self.cfg.mode == "simulation":
            if self._last_processed_ts is not None and self._last_processed_ts == self.df.index[-1]:
                return
            result = evaluate_agent(self._ai_frames, primary, selected, self._ai_model,
                                    self.cfg.credit, self.cfg.leverage, self.cfg.execution, boundary)
            summary = result["summary"]
            self.ai_decisions = result["decisions"][-200:]
            self.completed_trades = result["trades"]
            self.open_positions = result["open_positions"]
            self.closed_trade_count = summary["trade_count"]
            self.balance = self.cfg.credit + summary["realized_pnl"]
            self.total_pnl = summary["total_pnl"]
            self.win_rate = summary["win_rate"]
            self.max_drawdown = summary["max_drawdown_pct"]
            self.balance_history = [v["equity"] for v in result["equity_curve"]][-60:]
            self.equity_curve = result["equity_curve"]
            self._last_processed_ts = self.df.index[-1]
        else:
            if self.df.index[-1] < self._live_from_ts or self._last_processed_ts == self.df.index[-1]:
                return
            self._agent_signal = None  # a failed evaluation must never reuse an old order signal
            if not self.ai_model_metadata.get("validation_passed", False):
                self.error = "AgenticAI validation did not beat chronological baseline; trading paused"
                self._last_processed_ts = self.df.index[-1]
                self._persist()
                return
            row = feature_matrix(self._ai_frames, primary, selected).iloc[-1]
            evidence = probability_evidence(self._ai_model, row)
            position = self._real_state if self._exchange_position_id is not None else "none"
            leveraged_return = None
            if position != "none":
                owned = next((p for p in self._real_positions if str(p.get("exchange_id")) == str(self._exchange_position_id)), None)
                if owned is None or float(owned.get("entry_price") or 0) <= 0:
                    raise RuntimeError("Owned position price is unavailable; agent decision paused")
                sign = 1 if position == "long" else -1
                leveraged_return = sign * (float(self.df.Close.iloc[-1]) / float(owned["entry_price"]) - 1) * self.cfg.leverage
            decision = agent_decide(evidence, position, leveraged_return)
            decision["candle_time"] = str(self.df.index[-1])
            self.ai_decisions = (self.ai_decisions + [decision])[-200:]
            self._agent_signal = decision["signal"]
            self._last_processed_ts = self.df.index[-1]
            confidence = f"{decision['confidence']:.1%}" if decision["confidence"] is not None else "no position confidence"
            self._log_event("ai_decision", f"{decision['action']} | {confidence} | {decision['reason']}")
        self.current_price = float(self.df.Close.iloc[-1])
        self._persist()

    def _run_strategy(self) -> None:
        if self.cfg.strategy_name == ALEXNET_NAME and self.df is not None:
            self.df = self._alexnet_closed_frame(self.df)
        if self.df is None or len(self.df) < 50:
            return

        if self.cfg.strategy_name == AGENTIC_AI_NAME:
            self._run_agent_strategy()
            return

        if self.cfg.mode == "simulation":
            if self._last_processed_ts is not None and self._last_processed_ts == self.df.index[-1]:
                return
            from execution_model import evaluate_research
            boundary = int(self.df.index.searchsorted(self._live_from_ts)) if self._live_from_ts is not None else None
            if self.cfg.strategy_name == ALEXNET_NAME:
                if boundary is None:
                    raise RuntimeError("AlexNet simulation needs an explicit start boundary")
                boundary = self._alexnet_evaluation_boundary(self.df, boundary)
                result = self._evaluate_alexnet(self.df, boundary)
                self.ai_decisions = result["decisions"][-200:]
                if self.ai_decisions:
                    latest = self.ai_decisions[-1]
                    self._log_event("ai_decision", f"{latest['action']} | {latest['forecast_return_bps']} bps | {latest['reason']}")
            elif self.cfg.strategy_name == PRICE_AI_NAME:
                if boundary is None:
                    raise RuntimeError("PriceAI simulation needs a fixed training boundary")
                if self._ai_model is None:
                    self._ai_model = train_price_model(self.df, boundary)
                    self.ai_model_metadata = self._ai_model.metadata
                    self._log_event("ai_model_trained", f"{self.ai_model_metadata['model_version']} | {self.ai_model_metadata['training_rows']} examples")
                result = evaluate_price_model(self.df, self._ai_model, self.cfg.credit,
                                              self.cfg.leverage, self.cfg.execution, boundary)
                self.ai_decisions = result["decisions"][-200:]
                if self.ai_decisions:
                    latest = self.ai_decisions[-1]
                    forecast = latest["forecast_return_bps"]
                    self._log_event("ai_decision", f"{latest['action']} | {forecast} bps | {latest['reason']}")
            else:
                result = evaluate_research(self.df, self.cfg.strategy_name, self.cfg.credit, self.cfg.leverage,
                                           self.cfg.custom_params, self.cfg.execution, boundary)
            summary = result["summary"]
            self.completed_trades = result["trades"]
            self.open_positions = result["open_positions"]
            self.closed_trade_count = summary["trade_count"]
            self.balance = self.cfg.credit + summary["realized_pnl"]
            self.total_pnl = summary["total_pnl"]
            self.win_rate = summary["win_rate"]
            self.max_drawdown = summary["max_drawdown_pct"]
            self.balance_history = [v["equity"] for v in result["equity_curve"]][-60:]
            self.current_price = float(self.df.Close.iloc[-1])
            self.equity_curve = result["equity_curve"]
            self._last_processed_ts = self.df.index[-1]
            self._persist()
            return

        config = self._make_config()
        indicators = Indicators(self.df)
        strategy_cls = STRATEGY_MAP[self.cfg.strategy_name]
        custom_p = self._build_custom_params()
        strategy = strategy_cls(config, custom_p) if custom_p is not None else strategy_cls(config)

        live_start_idx = 0
        if self._live_from_ts is not None:
            live_start_idx = int(self.df.index.searchsorted(self._live_from_ts))

        # Capture state before running for change detection
        old_closed_count = self.closed_trade_count
        old_pos_keys = {(p["direction"], p["entry_time"]) for p in self.open_positions}

        seed_positions = [self._seed_position] if self._seed_position else None
        if self._seed_position and self._seed_position.get("exit_requested"):
            self.current_price = float(self.df.Close.iloc[-1])
            return
        previous_closed = self.completed_trades[:self.closed_trade_count] if seed_positions else []
        trades_df = strategy.run(self.df, indicators, live_start_idx, seed_positions=seed_positions)

        self.completed_trades = previous_closed + (trades_df.to_dict("records") if not trades_df.empty else [])
        self.open_positions = strategy.open_positions
        self.current_price = float(self.df["Close"].iloc[-1])

        # A modeled exit requests a close; it does not discard the owned fill.
        # Its reservation and actual price survive failed/partial close orders.
        if self._seed_position is not None:
            seed_ts = pd.Timestamp(self._seed_position["entry_time"])
            if seed_ts.tzinfo is not None:
                seed_ts = seed_ts.tz_convert("UTC").tz_localize(None)
            still_seeded = any(
                p["direction"] == self._seed_position["direction"]
                and pd.to_datetime(p["entry_time"], utc=True).tz_localize(None) == seed_ts
                for p in self.open_positions
            )
            if not still_seeded:
                self._seed_position["exit_requested"] = True

        # Each strategy appends one pending record per open position at the END of its log.
        # So the first `closed_trade_count` records are real closed trades; the rest are pending.
        pending_count = len(self.open_positions)
        if seed_positions:
            pending_count = sum(pd.to_datetime(pos["entry_time"], utc=True) <= pd.to_datetime(self.df.index[-1], utc=True)
                                for pos in self.open_positions)
        new_closed_count = len(self.completed_trades) - pending_count
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
        new_df = new_df.loc[new_df.index > self.df.index[-1]]
        if new_df.empty:
            return True
        first_new_ts = new_df.index[0].timestamp()

        expected_next_ts = self._next_candle_open(self.df.index[-1]).timestamp()

        if first_new_ts != expected_next_ts:
            return False

        attrs = self.df.attrs.copy()
        combined = pd.concat([self.df, new_df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        if not self._contiguous_candles(combined):
            return False
        self.df = combined
        self.df.attrs = attrs
        if self.cfg.mode == "real":
            self.df = self.df.tail(500)
        return True

    async def run_backtest(self):
        from execution_model import evaluate_research, WARMUP_CANDLES
        from position_limits import current_max_hold_days

        self.cfg.execution = {**(self.cfg.execution or {}),
                              "max_hold_days": await asyncio.to_thread(current_max_hold_days)}

        if self.cfg.strategy_name == AGENTIC_AI_NAME:
            frames = await self._fetch_agent_history(primary_limit=6000)
            result = await asyncio.to_thread(agent_backtest, frames, self.cfg.timeframe,
                self.cfg.ai_timeframes, self.cfg.credit, self.cfg.leverage, self.cfg.execution)
            return {**result["summary"], "trades": result["trades"],
                    "decisions": result["decisions"], "model": result["metadata"]["model"]}
        config = self._make_config()
        config.limit = 500 + WARMUP_CANDLES
        if self.cfg.strategy_name == ALEXNET_NAME:
            await asyncio.to_thread(self._ensure_alexnet_model)
            config.limit = max(config.limit, 500 + int(self.ai_model_metadata["lookback"]) +
                               int(self.ai_model_metadata["horizon_candles"]))
        df = await asyncio.to_thread(DataFetcher(config).fetch)
        if self.cfg.strategy_name == ALEXNET_NAME:
            df = self._alexnet_closed_frame(df)
            start_idx = self._alexnet_evaluation_boundary(df)
            if start_idx >= len(df):
                raise ValueError("AlexNet backtest has no held-out candles after its training and validation windows; wait for new completed candles")
            result = await asyncio.to_thread(self._evaluate_alexnet, df, start_idx)
            return {**result["summary"], "trades": result["trades"],
                    "decisions": result["decisions"], "model": result["metadata"]["model"]}
        if self.cfg.strategy_name == PRICE_AI_NAME:
            result = await asyncio.to_thread(train_and_evaluate, df, self.cfg.credit,
                                             self.cfg.leverage, self.cfg.execution)
            return {**result["summary"], "trades": result["trades"],
                    "decisions": result["decisions"], "model": result["metadata"]["model"]}
        result = await asyncio.to_thread(evaluate_research, df, self.cfg.strategy_name,
                                         self.cfg.credit, self.cfg.leverage, self.cfg.custom_params,
                                         self.cfg.execution, max(0, len(df)-500))
        return {**result["summary"], "trades": result["trades"]}

    def get_chart_data(self) -> dict:
        """Build the price + indicator chart for this bot's live strategy.

        Same {candles, indicators, strategy} shape the optimizer's post-run chart
        uses (see optimizer.py's _compute_best_trades), but computed from the
        bot's own in-memory candles and whatever params (default or optimized)
        it's actually trading with right now.
        """
        if self.df is None or len(self.df) < 2:
            return {"ok": False, "error": "No chart data yet — bot hasn't loaded candles."}

        _tehran = ZoneInfo("Asia/Tehran")
        df = self.df

        params = None
        if self.cfg.strategy_name not in (PRICE_AI_NAME, AGENTIC_AI_NAME, ALEXNET_NAME):
            config = self._make_config()
            indicators = Indicators(df)
            strategy_cls = STRATEGY_MAP[self.cfg.strategy_name]
            custom_p = self._build_custom_params()
            strategy = strategy_cls(config, custom_p) if custom_p is not None else strategy_cls(config)
            params = strategy.params

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

        # Annotate each trade record (closed + trailing open-position entries)
        # with unix timestamps for chart markers.
        trades = [dict(t) for t in self.completed_trades]
        for r in trades:
            r["_start_ts"] = time_map.get(r.get("Start Time", ""))
            r["_end_ts"] = time_map.get(r.get("End Time", ""))

        # Per-strategy indicator series — mirrors optimizer.py's chart-data build
        ind: dict = {}
        if self.cfg.strategy_name in ("AdvRSI", "ExtremeReversal", "AdvRSILong", "AdvRSIShort"):
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
        elif self.cfg.strategy_name == "TrendMomentum":
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

        return {
            "ok": True,
            "chart_revision": self._candle_state()["chart_revision"],
            "chart_data": {"candles": candles, "indicators": ind, "strategy": self.cfg.strategy_name},
            "trades": trades,
        }

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.status == BotStatus.RUNNING:
                return
            if self.cfg.strategy_name == ALEXNET_NAME:
                await asyncio.to_thread(self._ensure_alexnet_model)
            self._stop_event.clear()
            self.status = BotStatus.RUNNING
            self.error = None
            self._log_event("bot_started", f"Mode: {self.cfg.mode}")
            self._persist()
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if self.status == BotStatus.STOPPED and (self._task is None or self._task.done()):
                return
            self.status = BotStatus.STOPPED
            self._stop_event.set()
            if self._task and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            self.status = BotStatus.STOPPED
            self._log_event("bot_stopped")
            self._persist()

    def _next_candle_open(self, opened, *, timeframe=None, frame=None):
        frame = self.df if frame is None else frame
        attrs = frame.attrs if frame is not None else {}
        source = attrs.get("exchange", attrs.get("source", attrs.get("data_source", "nobitex")))
        opened = pd.Timestamp(opened)
        next_open = candle_close_time(timeframe or self.cfg.timeframe, int(opened.timestamp()), exchange=source)
        result = pd.Timestamp(next_open, unit="s", tz="UTC")
        return result.tz_convert(opened.tzinfo) if opened.tzinfo is not None else result.tz_localize(None)

    def _contiguous_candles(self, frame):
        return all(self._next_candle_open(previous, frame=frame) == current
                   for previous, current in zip(frame.index[:-1], frame.index[1:]))

    async def _refresh_data(self):
        import math
        config = self._make_config()
        if self.df is not None and not self.df.empty:
            gap_seconds = max(0, time.time() - self.df.index[-1].timestamp())
            if self.cfg.timeframe == "M":
                last = self.df.index[-1]
                now = pd.Timestamp.now(tz="UTC").tz_localize(None)
                gap_candles = max(0, (now.year - last.year) * 12 + now.month - last.month)
            else:
                gap_candles = math.ceil(gap_seconds / RESOLUTION_SECONDS[self.cfg.timeframe])
            config.limit = max(config.limit, gap_candles + 2)
        fresh = await asyncio.to_thread(DataFetcher(config).fetch)
        if fresh.empty:
            raise RuntimeError("No closed candles available")
        if self.cfg.mode == "simulation" and self.df is not None:
            combined = pd.concat([self.df, fresh])
            # Previously processed candles are immutable; retain the saved copy.
            combined = combined[~combined.index.duplicated(keep="first")].sort_index()
            combined.attrs = self.df.attrs.copy()
            if not self._contiguous_candles(combined):
                raise RuntimeError("Unrecoverable candle gap; saved simulation state preserved")
            self.df = combined
        else:
            self.df = fresh
        if self.cfg.strategy_name == AGENTIC_AI_NAME:
            old_frames = self._ai_frames
            self._ai_frames = await self._fetch_agent_history(base=self.df)
            if self.cfg.mode == "simulation":
                for tf, old in old_frames.items():
                    if tf == self.cfg.timeframe or tf not in self._ai_frames or old.empty:
                        continue
                    combined = pd.concat([old, self._ai_frames[tf]])
                    self._ai_frames[tf] = combined[~combined.index.duplicated(keep="first")].sort_index()

    async def _fetch_agent_history(self, base=None, primary_limit=4200):
        """Get exchange candles for all configured specialists without resampling."""
        frames = {self.cfg.timeframe: base} if base is not None else {}
        for tf in self.cfg.ai_timeframes:
            if tf == self.cfg.timeframe and base is not None:
                continue
            config = self._make_config()
            config.interval = tf
            config.limit = primary_limit if tf == self.cfg.timeframe else max(300,
                int(primary_limit * AGENT_SECONDS[self.cfg.timeframe] / AGENT_SECONDS[tf]) + 50)
            frames[tf] = await asyncio.to_thread(DataFetcher(config).fetch)
        return frames

    async def _refresh_agent_higher_frames(self):
        self._ai_frames[self.cfg.timeframe] = self.df
        for tf in self.cfg.ai_timeframes:
            if tf == self.cfg.timeframe:
                continue
            config = self._make_config()
            config.interval = tf
            latest = await asyncio.to_thread(DataFetcher(config).fetch_latest)
            previous = self._ai_frames.get(tf)
            if latest is None or latest.empty:
                raise RuntimeError(f"{tf} specialist has no completed candle")
            expected = self._next_candle_open(previous.index[-1], timeframe=tf, frame=previous) if previous is not None and not previous.empty else None
            if expected is None or latest.index[-1] > expected:
                config.limit = 1000
                refreshed = await asyncio.to_thread(DataFetcher(config).fetch)
                if self.cfg.mode == "simulation" and previous is not None and not previous.empty:
                    combined = pd.concat([previous, refreshed])
                    refreshed = combined[~combined.index.duplicated(keep="first")].sort_index()
                self._ai_frames[tf] = refreshed
            elif latest.index[-1] == expected:
                combined = pd.concat([previous, latest])
                self._ai_frames[tf] = combined if self.cfg.mode == "simulation" else combined.tail(1000)

    async def _run_loop(self):
        try:
            for attempt in range(10):
                try:
                    await self._refresh_data()
                    break
                except Exception:
                    if attempt == 9:
                        raise
                    await asyncio.sleep(min(2 ** attempt, 30))
            if self._live_from_ts is None or self.cfg.mode == "real":
                self._live_from_ts = self._next_candle_open(self.df.index[-1])
            self.current_price = float(self.df.Close.iloc[-1])
            self.last_updated = datetime.now(timezone.utc)
            if self.cfg.mode == "simulation":
                from position_limits import current_max_hold_days
                self.cfg.execution = {**(self.cfg.execution or {}),
                                      "max_hold_days": await asyncio.to_thread(current_max_hold_days)}
            self._run_strategy()
            await self._sync_exchange()
            self._persist()
            candle_retry_wait = 5
            candle_network_failed = False
            evaluated_candle = self.df.index[-1]
            while not self._stop_event.is_set():
                # Candle timestamps are opens; the NEXT candle closes two periods later.
                next_close = self._next_candle_open(self._next_candle_open(self.df.index[-1])).timestamp()
                from position_limits import exchange_expiration
                owned = next((p for p in self._real_positions if str(p.get("exchange_id")) == str(self._exchange_position_id)), None)
                expiration = exchange_expiration(owned.get("expiration_date")) if owned else None
                expiry_check = expiration.timestamp() - 3600 if expiration else None
                wake_at = min(next_close, expiry_check) if expiry_check is not None else next_close
                reconcile_pending = self.cfg.mode == "real" and bool(self.unresolved_intents())
                wait_seconds = max(candle_retry_wait, wake_at-time.time()+5)
                if candle_network_failed:
                    wait_seconds = min(wait_seconds, candle_retry_wait)
                if reconcile_pending:
                    wait_seconds = min(wait_seconds, _ORDER_RECONCILE_INTERVAL)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), wait_seconds)
                    break
                except asyncio.TimeoutError:
                    pass
                if reconcile_pending:
                    await self._reconcile_pending_orders()
                    if time.time() < wake_at + 5 and not candle_network_failed:
                        continue
                if expiry_check is not None and expiry_check <= next_close and time.time() >= expiry_check:
                    await self._sync_exchange()
                    self._persist()
                    if self._exchange_position_id is not None:
                        await asyncio.sleep(60)  # Avoid a tight retry loop on exchange failures.
                    continue
                fetcher = DataFetcher(self._make_config())
                try:
                    try:
                        fresh = await asyncio.to_thread(fetcher.fetch_latest)
                    except _CANDLE_TRANSPORT_ERRORS:
                        await self._refresh_data()
                    else:
                        if not self._validate_and_append(fresh):
                            await self._refresh_data()
                    if self.df.index[-1] > evaluated_candle and self.cfg.strategy_name == AGENTIC_AI_NAME:
                        await self._refresh_agent_higher_frames()
                except _CANDLE_TRANSPORT_ERRORS as exc:
                    candle_network_failed = True
                    candle_retry_wait = min(30, candle_retry_wait * 2)
                    self.error = f"Candle data unavailable: {type(exc).__name__}: {exc}"
                    self._log_event("warning", f"{self.error}; retrying in {candle_retry_wait}s")
                    self._persist()
                    continue  # Retain owned positions and never trade from stale candles.
                if candle_network_failed:
                    if self.error and self.error.startswith("Candle data unavailable:"):
                        self.error = None
                    self._log_event("info", "Candle data connection recovered")
                    self._persist()
                candle_network_failed = False
                if self.df.index[-1] <= evaluated_candle:
                    candle_retry_wait = min(30, candle_retry_wait * 2)
                    continue  # The provider has not published a new closed candle yet.
                candle_retry_wait = 5
                # A failed signal evaluation must never trigger an old exchange signal.
                if self.cfg.mode == "simulation":
                    from position_limits import current_max_hold_days
                    self.cfg.execution = {**(self.cfg.execution or {}),
                                          "max_hold_days": await asyncio.to_thread(current_max_hold_days)}
                self._run_strategy()
                await self._sync_exchange()
                evaluated_candle = self.df.index[-1]
                self.last_updated = datetime.now(timezone.utc)
                self._persist()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.status = BotStatus.ERROR
            self.error = str(exc)
            self._log_event("error", str(exc))
            self._log_event("bot_down", "Runtime failed; bot requires attention")
        finally:
            if self.status == BotStatus.RUNNING:
                self.status = BotStatus.STOPPED
                self._log_event("bot_down", "Runtime ended")
            self._persist()

    def _candle_state(self) -> dict:
        frame = self.df
        if frame is None or frame.empty:
            return {"last_candle_open": None, "next_candle_close": None,
                    "chart_revision": None, "candle_update_pending": False}
        opened = frame.index[-1]
        closed = self._next_candle_open(opened, frame=frame)
        upcoming = self._next_candle_open(closed, frame=frame)
        now = time.time()
        pending = upcoming.timestamp() <= now
        if self.cfg.timeframe not in {"D", "W", "M"} and pending:
            duration = RESOLUTION_SECONDS[self.cfg.timeframe]
            upcoming += pd.Timedelta(seconds=(int((now - upcoming.timestamp()) // duration) + 1) * duration)
        else:
            while upcoming.timestamp() <= now:
                upcoming = self._next_candle_open(upcoming, frame=frame)
        # Candle arrival can precede an awaited model/exchange update. Publish a
        # second revision when evaluation completes so markers catch up too.
        updated = self.last_updated.isoformat() if self.last_updated else ''
        revision = (f"{self.cfg.timeframe}:{frame.index[0].timestamp()}:{opened.timestamp()}:{len(frame)}:"
                    f"{self._last_processed_ts}:{updated}:{len(self.completed_trades)}:{len(self.open_positions)}")
        return {"last_candle_open": opened.timestamp(), "chart_revision": revision,
                "next_candle_close": upcoming.timestamp() if self.status == BotStatus.RUNNING else None,
                "candle_update_pending": pending and self.status == BotStatus.RUNNING}

    def to_state_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.cfg.name or self.id,
            "mode": self.cfg.mode,
            "symbol": self.cfg.symbol.upper(),
            "strategy": self.cfg.strategy_name,
            "timeframe": self.cfg.timeframe,
            "ai_timeframes": self.cfg.ai_timeframes if self.cfg.strategy_name == AGENTIC_AI_NAME else None,
            "ai_model_id": self.cfg.ai_model_id if self.cfg.strategy_name == ALEXNET_NAME else None,
            "credit": self.cfg.credit,
            "leverage": self.cfg.leverage,
            "execution": self.cfg.execution,
            "pnl_source": "strategy_estimate" if self.cfg.mode == "real" else "simulation_net_equity",
            "status": self.status.value,
            "open_positions": self._real_positions if self.cfg.mode == "real" else self.open_positions,
            "current_price": self.current_price,
            "total_pnl": round(self.total_pnl, 2),
            "balance": round(self.balance, 2),
            "trade_count": len(self.completed_trades),
            "closed_trade_count": self.closed_trade_count,
            "candle_count": len(self.df) if self.df is not None else 0,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "error": self.error,
            # Metrics
            "win_rate": self.win_rate,
            "max_drawdown": self.max_drawdown,
            "balance_history": self.balance_history[-30:],
            "ai_model": self.ai_model_metadata if self.cfg.strategy_name in (PRICE_AI_NAME, AGENTIC_AI_NAME, ALEXNET_NAME) else None,
            "last_ai_decision": self.ai_decisions[-1] if self.ai_decisions else None,
        }
        if self.cfg.mode == "real":
            d["exchange_state"] = self._real_state
            d["exchange_position_id"] = self._exchange_position_id
        d["pending_order"] = self._pending_order
        d["activity_log"] = list(reversed(self.activity_log[-20:]))
        d.update(self._candle_state())
        return d

    def to_detail_dict(self) -> dict:
        d = self.to_state_dict()
        d["trades"] = self.completed_trades
        if self.cfg.strategy_name in (PRICE_AI_NAME, AGENTIC_AI_NAME, ALEXNET_NAME):
            d["ai_decisions"] = self.ai_decisions
        d["activity_log"] = list(reversed(self.activity_log))
        d["unresolved_orders"] = self.unresolved_intents()
        from bot_history import detail_history
        d.update(detail_history(self))
        return d
