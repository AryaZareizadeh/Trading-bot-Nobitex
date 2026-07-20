import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from config import Config
from data import DataFetcher
from indicators import Indicators
from strategy import (AdvRSICombinedStrategy, ExtremeReversalStrategy,
                      TrendFilteredMomentumStrategy, PARAMS_CLASS)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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
        self._real_positions: list[dict] = []
        self._pending_order: Optional[dict] = None
        self._wallet_cache: Optional[dict] = None
        self._wallet_cache_ts: float = 0.0

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log_event(self, event: str, detail: str = "") -> None:
        self.activity_log.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "detail": detail,
        })
        if len(self.activity_log) > 200:
            self.activity_log = self.activity_log[-200:]
        _notify(self.cfg.mode, event, self.cfg.name or self.id, detail)

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
        self._real_positions = []
        self._pending_order = None
        self._wallet_cache = None
        self._wallet_cache_ts = 0.0
        self.status = BotStatus.IDLE
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
        if self._live_from_ts is None:
            return "none"
        for pos in self.open_positions:
            entry = pd.Timestamp(pos["entry_time"])
            if entry >= self._live_from_ts:
                return "long" if pos["direction"] == "Long" else "short"
        return "none"

    def _normalize_positions(self, raw: list[dict]) -> list[dict]:
        """Convert Nobitex active position objects to dashboard display format, filtered to this bot's symbol."""
        try:
            src, dst = self._parse_symbol()
        except ValueError:
            return []
        result = []
        for p in raw:
            if p.get("srcCurrency") != src or p.get("dstCurrency") != dst:
                continue
            if p.get("status") != "Open":
                continue
            direction = "Long" if p.get("side") == "buy" else "Short"
            result.append({
                "direction": direction,
                "entry_price": float(p.get("entryPrice") or 0),
                "entry_time": p.get("openedAt", ""),
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

    async def _place_single_order(
        self, client, src: str, dst: str, side: str, leverage: int
    ) -> list[dict]:
        """Place one order at the last candle close price. Retry placement up to
        _ORDER_MAX_ATTEMPTS times on API/network errors (exponential backoff, capped at 30 s).

        Poll for fill for up to _ORDER_TIMEOUT_BY_TF[timeframe] (5/30/90 min depending on timeframe):
          - Full fill or exchange Done  → success
          - >50% filled at timeout      → leave the order open, proceed with partial position
          - ≤50% filled at timeout      → cancel, proceed if any fill occurred

        Sets self._pending_order while waiting so the dashboard can show progress.
        """
        from nobitex_client import NobitexAPIError

        symbol = f"{src}{dst}".upper()
        existing_ids: set[int] = {p["id"] for p in client.positions("active")}
        limit_val = self._fetch_delegation_limit(client, symbol, side, leverage)

        price = float(self.df["Close"].iloc[-1])
        amount_str = self._compute_order_amount(src, side, leverage, price, limit_val)
        self._log_event("order_placed", f"Placing {side} {src}{dst} @ ${_format_price(price)}")

        # ── Place the order (placement retries only) ───────────────────────────
        order_id = None
        for attempt in range(1, _ORDER_MAX_ATTEMPTS + 1):
            try:
                order = client.open_margin_order(
                    src_currency=src,
                    dst_currency=dst,
                    side=side,
                    amount=amount_str,
                    price=_format_price(price),
                    leverage=leverage,
                    execution="market",
                )
                order_id = order.get("id")
                if order_id is not None:
                    self._log_event("info", f"Order #{order_id} placed @ ${_format_price(price)} (last candle close)")
                    break
                self._log_event("warning", f"Placement attempt {attempt}/{_ORDER_MAX_ATTEMPTS}: no order id in response")
            except NobitexAPIError as exc:
                if exc.code == "DuplicateOrder":
                    self._log_event("warning", f"Placement attempt {attempt}/{_ORDER_MAX_ATTEMPTS}: DuplicateOrder — sleeping {_DUPLICATE_ORDER_SLEEP}s")
                    await asyncio.sleep(_DUPLICATE_ORDER_SLEEP)
                    continue
                if exc.code in _FATAL_ORDER_ERRORS:
                    raise
                self._log_event("warning", f"Placement attempt {attempt}/{_ORDER_MAX_ATTEMPTS}: rejected [{exc.code}]")
            except Exception as exc:
                self._log_event("warning", f"Placement attempt {attempt}/{_ORDER_MAX_ATTEMPTS}: error: {exc}")

            if attempt < _ORDER_MAX_ATTEMPTS:
                await asyncio.sleep(min(2 ** (attempt - 1), 30))

        if order_id is None:
            raise RuntimeError(f"Order placement failed after {_ORDER_MAX_ATTEMPTS} attempts")

        self._pending_order = {
            "order_id": order_id,
            "side": side,
            "amount": float(amount_str),
            "price": price,
            "placed_at": datetime.now(timezone.utc).isoformat(),
            "matched_pct": 0.0,
        }

        # ── Poll for fill up to the timeframe-dependent timeout ────────────────
        order_timeout = _ORDER_TIMEOUT_BY_TF.get(self.cfg.timeframe, 300)
        timeout_label = f"{order_timeout // 60}-min"
        started_at = time.monotonic()
        order_amount = float(amount_str)
        matched = 0.0

        while True:
            await asyncio.sleep(_FILL_POLL_INTERVAL)
            elapsed = time.monotonic() - started_at

            try:
                info = client.order_status(order_id)
            except asyncio.CancelledError:
                try:
                    client.cancel_order(order_id)
                    self._log_event("warning", f"Bot stopped: cancelled pending order #{order_id}")
                except Exception:
                    pass
                self._pending_order = None
                raise
            except Exception as exc:
                self._log_event("warning", f"Order #{order_id} status check failed: {exc}")
                if elapsed >= order_timeout:
                    break
                continue

            matched = float(info.get("matchedAmount") or 0)
            status = info.get("status", "Active")
            matched_pct = (matched / order_amount * 100) if order_amount > 0 else 0.0
            self._pending_order["matched_pct"] = round(matched_pct, 1)

            if status == "Done":
                self._log_event("order_filled", f"Order #{order_id} fully filled")
                self._pending_order = None
                break

            if status == "Canceled":
                self._log_event("warning", f"Order #{order_id} cancelled by exchange ({matched_pct:.0f}% filled)")
                self._pending_order = None
                if matched == 0:
                    raise RuntimeError("Order cancelled by exchange with no fill")
                break  # partial fill — proceed to position lookup

            if elapsed >= order_timeout:
                if matched_pct > 50:
                    self._log_event("info", f"Order #{order_id}: {matched_pct:.0f}% filled at {timeout_label} timeout — leaving order open")
                    self._pending_order = None
                    break
                else:
                    try:
                        client.cancel_order(order_id)
                        self._log_event("warning", f"Order #{order_id}: {matched_pct:.0f}% filled at {timeout_label} timeout — cancelled")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._log_event("warning", f"Cancel failed: {exc}")
                    self._pending_order = None
                    if matched == 0:
                        raise RuntimeError("Order timed out with no fill")
                    break  # partial fill — proceed to position lookup

        # ── Locate the resulting position ──────────────────────────────────────
        if matched <= 0:
            raise RuntimeError("Order resolved with no fill")

        pos = None
        for lookup in range(1, _POSITION_LOOKUP_RETRIES + 1):
            if lookup > 1:
                await asyncio.sleep(_POSITION_LOOKUP_DELAY)
            pos = self._find_position_by_symbol(client, src, dst, side, existing_ids)
            if pos is not None:
                break
            self._log_event("warning", f"Position not yet visible (lookup {lookup}/{_POSITION_LOOKUP_RETRIES})")

        if pos is not None:
            self._exchange_position_id = pos["id"]
            self._log_event("order_filled", f"Position {pos['id']} confirmed @ ${_format_price(price)}")
        else:
            self._log_event("warning", "Order filled but position not found after lookups")

        return client.positions("active")

    def _ensure_exchange_client(self) -> bool:
        """Lazily construct the Nobitex client. Returns True if one is available."""
        if self._exchange_client is not None:
            return True
        try:
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
                info = client.order_status(order_id)
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
            client.cancel_order(order_id)
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

    async def _close_exchange_position(self, client, price_str: str) -> bool:
        """Submit and confirm a market close for the tracked position. Returns True on success.

        Polls the close order until it fills before returning True — this prevents the next
        _sync_exchange() from opening a new position while the old one is still settling.

        On failure: reset _exchange_position_id and _real_state so the next sync starts
        clean instead of looping forever on a dead id, then return False.
        If the close order was submitted but didn't fill, we do NOT reset state so the
        next sync can retry the close.
        """
        pos_id = self._exchange_position_id
        try:
            pos = client.position(pos_id)

            # Use liability minus any already-in-order amount (API max: 10 decimal places).
            # liability > delegatedAmount for shorts (includes accrued fees), so always prefer
            # liability to avoid leaving residual debt or getting ExceedLiability errors.
            liability           = float(pos.get("liability") or 0)
            liability_in_order  = float(pos.get("liabilityInOrder") or 0)
            remaining           = round(liability - liability_in_order, 10)

            if remaining <= 0:
                # A close order already covers the full liability — skip duplicate submission.
                self._log_event("info", f"Position {pos_id}: close already pending — skipping duplicate")
                return True

            # Fetch a live price to use as the slippage cap; fall back to the last
            # candle close (price_str) if the live fetch fails.
            src, dst = self._parse_symbol()
            symbol = f"{src}{dst}".upper()
            try:
                live = client.live_price(symbol)
                price_str = _format_price(live)
                self._log_event("info", f"Using live price {price_str} for close order")
            except Exception as exc:
                self._log_event("warning", f"live_price fetch failed ({exc}); using last candle price {price_str}")

            fallback  = f"{round(self.cfg.credit * self.cfg.leverage / float(price_str), 6):.6f}"
            close_amt = str(remaining) if liability > 0 else fallback

            close_order = client.close_position(
                pos_id, amount=close_amt, price=price_str, execution="market"
            )
            close_order_id = close_order.get("id")

            if close_order_id is None:
                self._log_event("warning", f"Close order for position {pos_id} returned no order id — will retry next sync")
                return False

            self._log_event("info", f"Close order #{close_order_id} submitted for position {pos_id}")

            order_timeout = _CLOSE_ORDER_TIMEOUT_BY_TF.get(self.cfg.timeframe, 120)
            outcome, matched = await self._poll_order_until_filled(client, close_order_id, order_timeout)
            if outcome != "done":
                self._log_event(
                    "warning",
                    f"Close order #{close_order_id} didn't fill ({outcome}, matched={matched}) — will retry next sync",
                )
                return False  # leave _exchange_position_id set; next sync retries

            self._exchange_position_id = None
            self._log_event("position_closed", f"Position {pos_id} closed")
            return True
        except asyncio.CancelledError:
            raise  # bot is stopping; don't reset state
        except Exception as exc:
            self.error = f"Close position {pos_id} failed: {exc}"
            self._log_event("warning", self.error)
            self._exchange_position_id = None
            self._real_state = "none"
            return False

    async def _sync_exchange(self) -> None:
        """Reconcile the live exchange with the strategy signal once per loop.

        On no change, just refresh the displayed positions. On a change, close any
        existing position, then open the new one (adopting an already-open matching
        position if one exists, to avoid double-opening after a failed close).
        """
        if self.cfg.mode != "real" or self.current_price is None:
            return
        if not self._ensure_exchange_client():
            return

        client = self._exchange_client
        signal = self._strategy_signal()

        try:
            if signal == self._real_state:
                active = client.positions("active")
                self._real_positions = self._normalize_positions(active)
                if self._exchange_position_id is not None:
                    tracked = next((p for p in active if p.get("id") == self._exchange_position_id), None)
                    if tracked is None or tracked.get("status") != "Open":
                        self._log_event(
                            "warning",
                            f"Tracked position {self._exchange_position_id} is no longer Open — resetting state",
                        )
                        self._exchange_position_id = None
                        self._real_state = "none"
                        # Fall through: _real_state != signal now, so the open/adopt path below runs.
                    else:
                        return  # position confirmed open; nothing to do
                else:
                    # No tracked position. Scan for an open exchange position this bot
                    # may have owned before the restart (not persisted across restarts).
                    src, dst = self._parse_symbol()
                    matching = [
                        p for p in active
                        if p.get("srcCurrency") == src
                        and p.get("dstCurrency") == dst
                        and p.get("status") == "Open"
                    ]
                    if not matching:
                        return
                    reconnect_pos = matching[0]
                    pos_side = reconnect_pos.get("side", "")
                    direction = "Long" if pos_side == "buy" else "Short"
                    state_val = "long" if pos_side == "buy" else "short"
                    # Prefer positions the current warmup backtest already knows about:
                    # back-date _live_from_ts so the matching entry becomes a live signal.
                    strategy_entries = [p for p in self.open_positions if p.get("direction") == direction]
                    if strategy_entries:
                        self._live_from_ts = min(pd.Timestamp(p["entry_time"]) for p in strategy_entries)
                        self._exchange_position_id = reconnect_pos["id"]
                        self._real_state = state_val
                        self._log_event(
                            "reconnected",
                            f"Reconnected to existing {direction} position {reconnect_pos['id']} after restart",
                        )
                        return

                    # Not visible in the current window — either its entry predates our
                    # fetched candles, or the strategy's own exit condition already fired
                    # for it while the server was down. Replay from its real exchange
                    # entry time forward (fetching more history if needed) to find out
                    # which, instead of guessing — and only close it if it should be.
                    resolved = await self._resolve_reconnected_position(reconnect_pos, direction)
                    self._exchange_position_id = reconnect_pos["id"]
                    self._real_state = state_val
                    if resolved is None:
                        # Couldn't verify (missing/unparseable entry time, or not enough
                        # history available) — adopt as live rather than risk closing a
                        # position we can't actually evaluate.
                        self._live_from_ts = self.df.index[-1] + pd.Timedelta(
                            seconds=RESOLUTION_SECONDS[self.cfg.timeframe]
                        )
                        self.open_positions = [{
                            "direction": direction,
                            "entry_price": float(reconnect_pos.get("entryPrice") or 0),
                            "entry_time": str(self._live_from_ts),
                        }]
                        self._log_event(
                            "warning",
                            f"Reconnected to {direction} position {reconnect_pos['id']} but couldn't verify "
                            f"its exit history — adopting as live without a confirmed entry time.",
                        )
                        return

                    still_open, entry_ts = resolved
                    if still_open:
                        self._live_from_ts = entry_ts
                        self._log_event(
                            "reconnected",
                            f"Reconnected to {direction} position {reconnect_pos['id']} (opened {entry_ts}) — "
                            f"no exit condition triggered while offline, resuming live tracking.",
                        )
                    else:
                        self._log_event(
                            "reconnected",
                            f"Reconnected to {direction} position {reconnect_pos['id']} (opened {entry_ts}) — "
                            f"exit conditions were met while offline; closing now.",
                        )
                        price_str = f"{self.current_price:.2f}"
                        await self._close_exchange_position(client, price_str)
                    return

            src, dst = self._parse_symbol()
            price_str = f"{self.current_price:.2f}"

            # ── Close the existing position before switching direction ──────────
            if self._real_state != "none" and self._exchange_position_id is not None:
                if not await self._close_exchange_position(client, price_str):
                    # State was reset; let the next loop recover cleanly.
                    self._real_positions = self._normalize_positions(client.positions("active"))
                    return

            # ── Open / go flat ─────────────────────────────────────────────────
            if signal != "none":
                side = "buy" if signal == "long" else "sell"
                # Reconcile: adopt an already-open matching position rather than
                # placing a duplicate (e.g. after a reset-to-none from a failed close).
                existing = self._find_position_by_symbol(client, src, dst, side, set())
                if existing is not None:
                    self._exchange_position_id = existing["id"]
                    self._log_event("info", f"Adopted existing exchange position {existing['id']} for {signal}")
                    all_positions = client.positions("active")
                else:
                    # Pre-order wallet check: ensure free margin covers the required credit.
                    # (cached — see _get_margin_wallet — so this doesn't hit the API every sync)
                    try:
                        w = self._get_margin_wallet(client)
                        usdt_w = w.get("USDT") or w.get("usdt") or {}
                        bal = float(usdt_w.get("balance", 0) or 0)
                        blocked = float(usdt_w.get("blocked", 0) or 0)
                        free_margin = bal - blocked
                        if free_margin < self.cfg.credit:
                            self._log_event(
                                "missed_signal",
                                f"Insufficient margin: {free_margin:.2f} USDT free < {self.cfg.credit:.2f} credit — skipping trade",
                            )
                            self._real_positions = self._normalize_positions(client.positions("active"))
                            return
                    except Exception as exc:
                        self._log_event("warning", f"Wallet check failed: {exc} — proceeding anyway")

                    all_positions = await self._place_single_order(
                        client, src, dst, side, self.cfg.leverage
                    )
            else:
                all_positions = client.positions("active")

            self._real_state = signal
            self.error = None
            self._real_positions = self._normalize_positions(all_positions)

        except Exception as exc:
            self.error = f"Exchange sync error: {exc}"

    # ── Config / strategy helpers ──────────────────────────────────────────────

    def _make_config(self) -> Config:
        c = Config()
        c.symbol = self.cfg.symbol
        c.interval = self.cfg.timeframe
        c.limit = 1000
        c.leverage = self.cfg.leverage
        c.trade_size = self.cfg.credit
        c.initial_capital = self.cfg.credit
        return c

    def _run_strategy(self) -> None:
        if self.df is None or len(self.df) < 50:
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

        indicators = Indicators(df)
        strategy_cls = STRATEGY_MAP[self.cfg.strategy_name]
        custom_p = self._build_custom_params()
        strategy = strategy_cls(config, custom_p) if custom_p is not None else strategy_cls(config)
        trades_df = strategy.run(df, indicators, live_start_idx=0)

        candle_count = len(df)
        empty_extra = {"long_count": 0, "short_count": 0, "long_pct": 0.0, "sum_profits": 0.0,
                       "sum_losses": 0.0, "profit_efficiency": None}
        if trades_df.empty:
            return {"profitable": None, "total_pnl": 0.0, "trade_count": 0, "win_rate": 0.0,
                    "candle_count": candle_count, **empty_extra}

        open_count = len(strategy.open_positions)
        closed_count = max(0, len(trades_df) - open_count)
        records = trades_df.to_dict("records")

        # Skip the first closed trade — indicators lack warmup data at the very start
        valid_records = records[1:closed_count]
        if not valid_records:
            return {"profitable": None, "total_pnl": 0.0, "trade_count": 0, "win_rate": 0.0,
                    "candle_count": candle_count, "trades": [], **empty_extra}

        closed_pnls = [float(str(r.get("P&L ($)", 0))) for r in valid_records]
        total_pnl = sum(closed_pnls)
        wins = sum(1 for p in closed_pnls if p > 0)
        win_rate = round(wins / len(closed_pnls) * 100, 1) if closed_pnls else 0.0

        sum_profits = round(sum(p for p in closed_pnls if p > 0), 2)
        sum_losses = round(sum(p for p in closed_pnls if p < 0), 2)  # negative (or 0 if none)
        gross_abs_pnl = sum_profits + abs(sum_losses)
        profit_efficiency = round(total_pnl / gross_abs_pnl * 100, 1) if gross_abs_pnl != 0 else None

        long_count = sum(1 for r in valid_records if str(r.get("Position", "")).strip().lower() == "long")
        short_count = sum(1 for r in valid_records if str(r.get("Position", "")).strip().lower() == "short")
        long_pct = round(long_count / len(valid_records) * 100, 1) if valid_records else 0.0

        return {
            "profitable": total_pnl > 0,
            "total_pnl": round(total_pnl, 2),
            "trade_count": len(valid_records),
            "win_rate": win_rate,
            "candle_count": candle_count,
            "trades": valid_records,
            "long_count": long_count,
            "short_count": short_count,
            "long_pct": long_pct,
            "sum_profits": sum_profits,
            "sum_losses": sum_losses,
            "profit_efficiency": profit_efficiency,
        }

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
        if self.cfg.strategy_name in ("AdvRSI", "ExtremeReversal"):
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
            "chart_data": {"candles": candles, "indicators": ind, "strategy": self.cfg.strategy_name},
            "trades": trades,
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
            for attempt in range(10):
                try:
                    self.df = await asyncio.to_thread(fetcher.fetch)
                    break
                except Exception as exc:
                    if attempt == 9:
                        raise RuntimeError(f"Initial data fetch failed: {exc}") from exc
                    await asyncio.sleep(min(2 ** attempt, 30))

            _res = RESOLUTION_SECONDS[self.cfg.timeframe]
            self._live_from_ts = self.df.index[-1] + pd.Timedelta(seconds=_res)
            self.current_price = float(self.df["Close"].iloc[-1])
            self.last_updated = datetime.now(timezone.utc)
            self.error = None
            self._log_event("data_loaded", f"Loaded {len(self.df)} warmup candles. Live trading starts at next candle close.")

            if self.cfg.mode == "real":
                try:
                    self._run_strategy()
                except Exception as exc:
                    self._log_event("warning", f"Startup strategy run failed: {exc}")
                await self._sync_exchange()

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
                for attempt in range(10):
                    try:
                        new_df = await asyncio.to_thread(fetcher.fetch_latest)
                        if new_df is not None and not new_df.empty:
                            break
                    except Exception:
                        pass
                    if attempt < 9:
                        await asyncio.sleep(min(2 ** attempt, 30))

                if new_df is None or new_df.empty:
                    try:
                        self.df = await asyncio.to_thread(fetcher.fetch)
                        _res = RESOLUTION_SECONDS[self.cfg.timeframe]
                        self._live_from_ts = self.df.index[-1] + pd.Timedelta(seconds=_res)
                        self.error = "Candle fetch failed; did full re-fetch for integrity."
                        self._log_event("warning", self.error)
                        self._log_event("warning", "live_from_ts advanced past re-fetched candles — no new entries until next close")
                    except Exception as exc:
                        self.error = f"Full re-fetch failed: {exc}"
                        self._log_event("error", self.error)
                        continue
                else:
                    ok = self._validate_and_append(new_df)
                    if not ok:
                        try:
                            self.df = await asyncio.to_thread(fetcher.fetch)
                            _res = RESOLUTION_SECONDS[self.cfg.timeframe]
                            self._live_from_ts = self.df.index[-1] + pd.Timedelta(seconds=_res)
                            self.error = "Data gap detected; re-fetched all candles."
                            self._log_event("warning", self.error)
                            self._log_event("warning", "live_from_ts advanced past re-fetched candles — no new entries until next close")
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
            "open_positions": self._real_positions if self.cfg.mode == "real" else self.open_positions,
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
        d["pending_order"] = self._pending_order
        d["activity_log"] = list(reversed(self.activity_log[-20:]))
        return d

    def to_detail_dict(self) -> dict:
        d = self.to_state_dict()
        d["trades"] = self.completed_trades
        d["activity_log"] = list(reversed(self.activity_log))
        return d