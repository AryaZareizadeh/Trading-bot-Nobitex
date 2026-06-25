from abc import ABC, abstractmethod
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd

from config import Config
from indicators import Indicators

_TEHRAN = ZoneInfo("Asia/Tehran")


def _fmt_time(ts: pd.Timestamp) -> str:
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(_TEHRAN).strftime("%Y-%m-%d %H:%M:%S")


# ── Per-strategy indicator parameters ───────────────────────────────────────────
# Each strategy owns its indicator inputs, so two strategies can use the same
# indicator (RSI, Stoch-RSI, EMA, MACD) with different parameters. The shared
# Indicators provider caches each (indicator, params) combination independently.


@dataclass
class AdvRsiParams:
    rsi_length: int = 40
    stoch_length: int = 36
    k_smooth: int = 7
    d_smooth: int = 5
    rsi_fast_length: int = 14
    rsi_ma_length: int = 7
    oversold: int = 22
    overbought: int = 78


@dataclass
class StochRsiExtremeParams:
    """ExtremeReversal's own indicator inputs plus the extreme-zone entry thresholds.

    Defines its own StochRSI and RSI parameters (rather than sharing AdvRSI's), so
    ExtremeReversal can be tuned independently while the shared Indicators provider
    still reuses any series whose parameters happen to match another strategy's.
    """
    # StochRSI / RSI indicator inputs (own copy, independent of AdvRsiParams)
    rsi_length: int = 40
    stoch_length: int = 36
    k_smooth: int = 7
    d_smooth: int = 5
    rsi_fast_length: int = 9
    rsi_ma_length: int = 7
    oversold: int = 22
    overbought: int = 78
    # Deep-extreme entry gates
    long_kd_max: float = 7     # StochRSI K and D must both be below this for a long
    long_rsi_max: float = 30   # fast RSI must be below this for a long
    short_kd_min: float = 93   # StochRSI K and D must both be above this for a short
    short_rsi_min: float = 70  # fast RSI must be above this for a short


@dataclass
class TrendMomentumParams:
    rsi_length: int = 40
    stoch_length: int = 36
    k_smooth: int = 7
    d_smooth: int = 5
    ema_trend_length: int = 50
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    oversold: int = 22
    overbought: int = 78


class Strategy(ABC):
    #: Default parameter dataclass for this strategy; overridden per subclass.
    params_class: type | None = None

    def __init__(self, config: Config, params=None):
        self.config = config
        self.open_positions: list[dict] = []
        if params is None and self.params_class is not None:
            params = self.params_class()
        self.params = params

    @abstractmethod
    def run(self, df: pd.DataFrame, indicators: Indicators, live_start_idx: int = 0) -> pd.DataFrame:
        ...

    def liquidation_price(self, direction: str, entry_price: float) -> float:
        """Price at which commitment ratio equals liquidation_ratio.

        Long  CR = (asset_value + collateral) / borrowed  → P_liq = P0 * (r - 1/L)
        Short CR = (sale_proceeds + collateral) / debt    → P_liq = P0 * (L+1) / (r*L)
        """
        L = self.config.leverage
        r = self.config.liquidation_ratio
        if direction == "Long":
            return entry_price * (r - 1.0 / L)
        return entry_price * (L + 1.0) / (r * L)

    def _check_liquidation(
        self, direction: str, entry_idx: int, current_idx: int, df: pd.DataFrame
    ) -> float | None:
        """Return the liquidation price if the current close crosses it, else None."""
        entry_price = df["Close"].iloc[entry_idx]
        liq_p = self.liquidation_price(direction, entry_price)
        current_price = df["Close"].iloc[current_idx]
        if direction == "Long" and current_price <= liq_p:
            return liq_p
        if direction == "Short" and current_price >= liq_p:
            return liq_p
        return None

    def _make_trade_record(
        self,
        trade_idx: int,
        direction: str,
        df: pd.DataFrame,
        entry_idx: int,
        exit_idx: int,
        balance: float,
        liquidated: bool = False,
        exit_price: float | None = None,
    ) -> tuple[dict, float]:
        start_price = df["Close"].iloc[entry_idx]
        end_price = exit_price if exit_price is not None else df["Close"].iloc[exit_idx]
        pct = (end_price - start_price) / start_price
        effective_size = max(0.0, min(self.config.trade_size + balance, self.config.trade_size))
        pnl = self.config.leverage * effective_size * (pct if direction == "Long" else -pct)
        L, C = self.config.leverage, effective_size
        if direction == "Long":
            cr = end_price / start_price + 1.0 / L
        else:
            cr = (L + 1.0) * start_price / (L * end_price)
        record = {
            "Trade #": trade_idx,
            "Position": direction,
            "Start Time": _fmt_time(df.index[entry_idx]),
            "End Time": _fmt_time(df.index[exit_idx]),
            "Candle Count": exit_idx - entry_idx + 1,
            "Start Price": f"{start_price:.2f}",
            "End Price": f"{end_price:.2f}",
            "Price Change": f"{pct * 100:.2f}%",
            "Commit Ratio": f"{cr:.4f}",
            "P&L ($)": f"{pnl:.2f}",
            "Balance ($)": f"{balance + pnl:.2f}",
            "Liquidated": liquidated,
        }
        return record, pnl


class _StochRsiSwingStrategy(Strategy):
    """Shared engine for Stoch-RSI swing strategies.

    Allows multiple concurrent positions and applies a common exit policy:
    liquidation, StochRSI-D leaving the extreme band, a mid-zone RSI stall, or
    StochRSI-K crossing back through the opposite band. Subclasses define entry
    only, via `_entry_signal`; stateful entry logic resets in `_init_entry_state`.
    """

    params_class = AdvRsiParams

    def _init_entry_state(self) -> None:
        """Reset per-run entry state. Override if entry detection is stateful."""

    def _entry_signal(self, i, k, d, rsi, rsi_ma, kd_diff) -> str | None:
        """Return 'long', 'short', or None for candle i. Implemented by subclasses."""
        raise NotImplementedError

    def run(self, df: pd.DataFrame, indicators: Indicators, live_start_idx: int = 0) -> pd.DataFrame:
        p = self.params
        srsi = indicators.stoch_rsi(p.rsi_length, p.stoch_length, p.k_smooth, p.d_smooth)
        k, d = srsi.k, srsi.d
        rsi, rsi_ma = indicators.rsi_with_ma(p.rsi_fast_length, p.rsi_ma_length)
        kd_diff = (k - d).fillna(0)
        oversold, overbought = p.oversold, p.overbought
        open_longs: list[int] = []
        open_shorts: list[int] = []
        trade_idx = 0
        balance = 0.0
        log = []
        self._init_entry_state()

        for i in range(live_start_idx, len(df)):
            ki, di = k.iloc[i], d.iloc[i]
            ri = rsi.iloc[i]
            diff = kd_diff.iloc[i]
            prev_d = d.iloc[i - 1] if i > 0 else di
            prev_k = k.iloc[i - 1] if i > 0 else ki

            if open_longs:
                surviving_longs = []
                for entry_idx in open_longs:
                    liq_p = self._check_liquidation("Long", entry_idx, i, df)
                    if liq_p is not None:
                        record, pnl = self._make_trade_record(trade_idx, "Long", df, entry_idx, i, balance, liquidated=True, exit_price=liq_p)
                        balance += pnl
                        log.append(record)
                        trade_idx += 1
                    else:
                        surviving_longs.append(entry_idx)
                open_longs = surviving_longs

                if open_longs:
                    d_dropped = prev_d >= overbought and di < overbought
                    mid_long = ki < overbought and diff < -0.08 * ki and pd.notna(ri) and 45 < ri < 55
                    k_crossed_below_oversold = prev_k >= oversold and ki < oversold
                    if d_dropped or mid_long or k_crossed_below_oversold:
                        for entry_idx in open_longs:
                            record, pnl = self._make_trade_record(trade_idx, "Long", df, entry_idx, i, balance)
                            balance += pnl
                            log.append(record)
                            trade_idx += 1
                        open_longs = []

            if open_shorts:
                surviving_shorts = []
                for entry_idx in open_shorts:
                    liq_p = self._check_liquidation("Short", entry_idx, i, df)
                    if liq_p is not None:
                        record, pnl = self._make_trade_record(trade_idx, "Short", df, entry_idx, i, balance, liquidated=True, exit_price=liq_p)
                        balance += pnl
                        log.append(record)
                        trade_idx += 1
                    else:
                        surviving_shorts.append(entry_idx)
                open_shorts = surviving_shorts

                if open_shorts:
                    d_rose = prev_d <= oversold and di > oversold
                    mid_short = ki > oversold and diff > 0.08 * ki and pd.notna(ri) and 45 < ri < 55
                    k_crossed_above_overbought = prev_k <= overbought and ki > overbought
                    if d_rose or mid_short or k_crossed_above_overbought:
                        for entry_idx in open_shorts:
                            record, pnl = self._make_trade_record(trade_idx, "Short", df, entry_idx, i, balance)
                            balance += pnl
                            log.append(record)
                            trade_idx += 1
                        open_shorts = []

            signal = self._entry_signal(i, k, d, rsi, rsi_ma, kd_diff)
            if signal == "long":
                open_longs.append(i)
            elif signal == "short":
                open_shorts.append(i)

        self.open_positions = [
            {"direction": "Long", "entry_price": float(df["Close"].iloc[idx]), "entry_time": str(df.index[idx])}
            for idx in open_longs
        ] + [
            {"direction": "Short", "entry_price": float(df["Close"].iloc[idx]), "entry_time": str(df.index[idx])}
            for idx in open_shorts
        ]

        last_i = len(df) - 1
        for entry_idx in open_longs:
            record, pnl = self._make_trade_record(trade_idx, "Long", df, entry_idx, last_i, balance)
            balance += pnl
            log.append(record)
            trade_idx += 1
        for entry_idx in open_shorts:
            record, pnl = self._make_trade_record(trade_idx, "Short", df, entry_idx, last_i, balance)
            balance += pnl
            log.append(record)
            trade_idx += 1

        return pd.DataFrame(log)


class AdvRSICombinedStrategy(_StochRsiSwingStrategy):
    """Long: sRSI-K crosses above oversold with RSI > MA and MA < 50.
    Short: sRSI-K crosses below overbought with RSI < MA and MA > 50.
    Long exit: KD diff < -10, sRSI-D drops below overbought, RSI near 50 with sRSI-K < overbought and KD diff < 0, or sRSI-K crosses below oversold from within the band.
    Short exit: KD diff > 10, sRSI-D rises above oversold, RSI near 50 with sRSI-K > oversold and KD diff > 0, or sRSI-K crosses above overbought from within the band.
    Entry requires the band to be held for 5 candles before the crossing back.
    Multiple concurrent positions are allowed."""

    KD_EXIT_THRESHOLD = 10
    params_class = AdvRsiParams

    def _init_entry_state(self) -> None:
        self._waiting_long = False
        self._waiting_short = False
        self._oversold_since: int | None = None
        self._overbought_since: int | None = None

    def _entry_signal(self, i, k, d, rsi, rsi_ma, kd_diff) -> str | None:
        p = self.params
        oversold, overbought = p.oversold, p.overbought
        ki = k.iloc[i]
        ri, ri_ma = rsi.iloc[i], rsi_ma.iloc[i]

        if ki < oversold:
            if self._oversold_since is None:
                self._oversold_since = i
            self._overbought_since = None
            self._waiting_short = False
            if i - self._oversold_since >= 5:
                self._waiting_long = True
        elif ki > overbought:
            if self._overbought_since is None:
                self._overbought_since = i
            self._oversold_since = None
            self._waiting_long = False
            if i - self._overbought_since >= 5:
                self._waiting_short = True
        else:
            self._oversold_since = None
            self._overbought_since = None

        signal = None
        if self._waiting_long and ki >= oversold:
            if pd.notna(ri) and pd.notna(ri_ma) and ri > ri_ma and ri_ma < 50:
                signal = "long"
            self._waiting_long = False
        elif self._waiting_short and ki <= overbought:
            if pd.notna(ri) and pd.notna(ri_ma) and ri < ri_ma and ri_ma > 50:
                signal = "short"
            self._waiting_short = False
        return signal


class ExtremeReversalStrategy(_StochRsiSwingStrategy):
    """Deep-extreme mean-reversion entries, with the AdvRSI exit policy.

    Long entry: StochRSI K and D both below `long_kd_max` while fast RSI is below
        `long_rsi_max` and below its moving average (deeply oversold and still falling).
    Short entry: StochRSI K and D both above `short_kd_min` while fast RSI is above
        `short_rsi_min` and above its moving average (deeply overbought and still rising).
    Entries are edge-triggered: one position opens when the extreme condition is first
    met, and re-arms only after price leaves the extreme zone (so a single deep dip does
    not stack a position on every candle).
    Exits, liquidation and multi-position handling are inherited from AdvRSICombinedStrategy."""

    params_class = StochRsiExtremeParams

    def _init_entry_state(self) -> None:
        self._prev_long_cond = False
        self._prev_short_cond = False

    def _entry_signal(self, i, k, d, rsi, rsi_ma, kd_diff) -> str | None:
        p = self.params
        ki, di = k.iloc[i], d.iloc[i]
        ri, ri_ma = rsi.iloc[i], rsi_ma.iloc[i]
        rsi_ok = pd.notna(ri) and pd.notna(ri_ma)

        long_cond = (
            ki < p.long_kd_max and di < p.long_kd_max
            and rsi_ok and ri < p.long_rsi_max and ri < ri_ma
        )
        short_cond = (
            ki > p.short_kd_min and di > p.short_kd_min
            and rsi_ok and ri > p.short_rsi_min and ri > ri_ma
        )

        signal = None
        if long_cond and not self._prev_long_cond:
            signal = "long"
        elif short_cond and not self._prev_short_cond:
            signal = "short"

        self._prev_long_cond = long_cond
        self._prev_short_cond = short_cond
        return signal


class TrendFilteredMomentumStrategy(Strategy):
    """Trend-regime-filtered momentum strategy.

    Entry: StochRSI exits extreme zone + K/D aligned + price above/below EMA-50 + MACD histogram agrees.
    Exit: K/D reversal after touching target zone, OR EMA-50 trend break, OR MACD histogram zero-line flip.

    The EMA-50 filter prevents trading against the dominant trend; MACD ensures macro momentum
    agrees before entry and signals the exit when that momentum exhausts.
    """

    params_class = TrendMomentumParams

    def run(self, df: pd.DataFrame, indicators: Indicators, live_start_idx: int = 0) -> pd.DataFrame:
        p = self.params
        srsi = indicators.stoch_rsi(p.rsi_length, p.stoch_length, p.k_smooth, p.d_smooth)
        k, d = srsi.k, srsi.d
        ema = indicators.ema(p.ema_trend_length)
        macd_hist = indicators.macd(p.macd_fast, p.macd_slow, p.macd_signal).hist
        close = df["Close"]

        oversold = p.oversold
        overbought = p.overbought

        position = 0
        entry_idx = None
        trade_idx = 0
        balance = 0.0
        log = []
        waiting_long = False
        waiting_short = False
        touched_extreme = False  # whether the open position has reached its target zone

        for i in range(live_start_idx, len(df)):
            ki, di = k.iloc[i], d.iloc[i]
            close_i = close.iloc[i]
            ema_i = ema.iloc[i]
            hist_i = macd_hist.iloc[i]
            prev_hist = macd_hist.iloc[i - 1] if i > 0 else hist_i

            has_all = pd.notna(ema_i) and pd.notna(hist_i) and pd.notna(prev_hist)

            # ── Exit logic ────────────────────────────────────────────────
            if position == 1:
                liq_p = self._check_liquidation("Long", entry_idx, i, df)
                if liq_p is not None:
                    record, pnl = self._make_trade_record(trade_idx, "Long", df, entry_idx, i, balance, liquidated=True, exit_price=liq_p)
                    balance += pnl
                    log.append(record)
                    trade_idx += 1
                    position, entry_idx, touched_extreme = 0, None, False
                else:
                    if ki >= overbought:
                        touched_extreme = True
                    kd_exit    = touched_extreme and ki < di
                    trend_exit = has_all and close_i < ema_i
                    macd_flip  = has_all and prev_hist >= 0 and hist_i < 0
                    if kd_exit or trend_exit or macd_flip:
                        record, pnl = self._make_trade_record(trade_idx, "Long", df, entry_idx, i, balance)
                        balance += pnl
                        log.append(record)
                        trade_idx += 1
                        position, entry_idx, touched_extreme = 0, None, False

            elif position == -1:
                liq_p = self._check_liquidation("Short", entry_idx, i, df)
                if liq_p is not None:
                    record, pnl = self._make_trade_record(trade_idx, "Short", df, entry_idx, i, balance, liquidated=True, exit_price=liq_p)
                    balance += pnl
                    log.append(record)
                    trade_idx += 1
                    position, entry_idx, touched_extreme = 0, None, False
                else:
                    if ki <= oversold:
                        touched_extreme = True
                    kd_exit    = touched_extreme and ki > di
                    trend_exit = has_all and close_i > ema_i
                    macd_flip  = has_all and prev_hist <= 0 and hist_i > 0
                    if kd_exit or trend_exit or macd_flip:
                        record, pnl = self._make_trade_record(trade_idx, "Short", df, entry_idx, i, balance)
                        balance += pnl
                        log.append(record)
                        trade_idx += 1
                        position, entry_idx, touched_extreme = 0, None, False

            # ── Entry logic ───────────────────────────────────────────────
            if position == 0:
                if ki < oversold and di < oversold:
                    waiting_long, waiting_short = True, False
                elif ki > overbought and di > overbought:
                    waiting_short, waiting_long = True, False

                if waiting_long and ki >= oversold:
                    if has_all and ki > di and close_i > ema_i and hist_i > 0:
                        position, entry_idx, touched_extreme = 1, i, False
                    waiting_long = False
                elif waiting_short and ki <= overbought:
                    if has_all and ki < di and close_i < ema_i and hist_i < 0:
                        position, entry_idx, touched_extreme = -1, i, False
                    waiting_short = False

        self.open_positions = []
        if position != 0 and entry_idx is not None:
            direction = "Long" if position == 1 else "Short"
            self.open_positions = [{
                "direction": direction,
                "entry_price": float(df["Close"].iloc[entry_idx]),
                "entry_time": str(df.index[entry_idx]),
            }]
            record, pnl = self._make_trade_record(trade_idx, direction, df, entry_idx, len(df) - 1, balance)
            balance += pnl
            log.append(record)

        return pd.DataFrame(log)
