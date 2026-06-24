from abc import ABC, abstractmethod
from zoneinfo import ZoneInfo

import pandas as pd

from config import Config
from indicators import IndicatorSet

_TEHRAN = ZoneInfo("Asia/Tehran")


def _fmt_time(ts: pd.Timestamp) -> str:
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(_TEHRAN).strftime("%Y-%m-%d %H:%M:%S")


class Strategy(ABC):
    def __init__(self, config: Config):
        self.config = config
        self.open_positions: list[dict] = []

    @abstractmethod
    def run(self, df: pd.DataFrame, indicators: IndicatorSet, live_start_idx: int = 0) -> pd.DataFrame:
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


class SRSIStrategy(Strategy):
    """Simple Stochastic RSI: enter on K/D zone breakout, exit on K/D crossover."""

    def run(self, df: pd.DataFrame, indicators: IndicatorSet, live_start_idx: int = 0) -> pd.DataFrame:
        k, d = indicators.stoch_k, indicators.stoch_d
        oversold, overbought = self.config.stoch_oversold, self.config.stoch_overbought

        position = 0
        entry_idx = None
        trade_idx = 0
        balance = 0.0
        log = []
        waiting_long = False
        waiting_short = False

        for i in range(live_start_idx, len(df)):
            ki, di = k.iloc[i], d.iloc[i]

            if position == 0:
                if ki < oversold and di < oversold and ki > di:
                    waiting_long, waiting_short = True, False
                elif ki > overbought and di > overbought and ki < di:
                    waiting_short, waiting_long = True, False

                if waiting_long and ki > oversold:
                    position, entry_idx, waiting_long = 1, i, False
                elif waiting_short and ki < overbought:
                    position, entry_idx, waiting_short = -1, i, False

            elif position == 1:
                liq_p = self._check_liquidation("Long", entry_idx, i, df)
                if liq_p is not None:
                    record, pnl = self._make_trade_record(trade_idx, "Long", df, entry_idx, i, balance, liquidated=True, exit_price=liq_p)
                    balance += pnl
                    log.append(record)
                    trade_idx += 1
                    position, entry_idx = 0, None
                elif ki < di:
                    record, pnl = self._make_trade_record(trade_idx, "Long", df, entry_idx, i, balance)
                    balance += pnl
                    log.append(record)
                    trade_idx += 1
                    position, entry_idx = 0, None

            elif position == -1:
                liq_p = self._check_liquidation("Short", entry_idx, i, df)
                if liq_p is not None:
                    record, pnl = self._make_trade_record(trade_idx, "Short", df, entry_idx, i, balance, liquidated=True, exit_price=liq_p)
                    balance += pnl
                    log.append(record)
                    trade_idx += 1
                    position, entry_idx = 0, None
                    waiting_short = False
                elif ki > di:
                    record, pnl = self._make_trade_record(trade_idx, "Short", df, entry_idx, i, balance)
                    balance += pnl
                    log.append(record)
                    trade_idx += 1
                    position, entry_idx = 0, None
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


class ComboStrategy(Strategy):
    """Combined RSI + Stochastic RSI with improved exit conditions."""

    def run(self, df: pd.DataFrame, indicators: IndicatorSet, live_start_idx: int = 0) -> pd.DataFrame:
        k, d = indicators.stoch_k, indicators.stoch_d
        rsi, rsi_ma = indicators.rsi, indicators.rsi_ma
        oversold, overbought = self.config.stoch_oversold, self.config.stoch_overbought

        position = 0
        entry_idx = None
        trade_idx = 0
        balance = 0.0
        log = []
        waiting_long = False
        waiting_short = False
        was_overbought = False
        was_oversold = False

        for i in range(live_start_idx, len(df)):
            ki, di = k.iloc[i], d.iloc[i]
            ri, ri_ma = rsi.iloc[i], rsi_ma.iloc[i]

            if position == 0:
                if ki < oversold and di < oversold:
                    waiting_long, waiting_short = True, False
                elif ki > overbought and di > overbought:
                    waiting_short, waiting_long = True, False

                if waiting_long and ki > oversold:
                    if pd.notna(ri) and pd.notna(ri_ma) and ri > ri_ma:
                        position, entry_idx, was_overbought = 1, i, False
                    waiting_long = False
                elif waiting_short and ki < overbought:
                    if pd.notna(ri) and pd.notna(ri_ma) and ri < ri_ma:
                        position, entry_idx, was_oversold = -1, i, False
                    waiting_short = False

            elif position == 1:
                liq_p = self._check_liquidation("Long", entry_idx, i, df)
                if liq_p is not None:
                    record, pnl = self._make_trade_record(trade_idx, "Long", df, entry_idx, i, balance, liquidated=True, exit_price=liq_p)
                    balance += pnl
                    log.append(record)
                    trade_idx += 1
                    position, entry_idx, was_overbought = 0, None, False
                else:
                    if ki >= overbought:
                        was_overbought = True
                    if (ki < overbought and di > ki) or (was_overbought and ki < overbought):
                        record, pnl = self._make_trade_record(trade_idx, "Long", df, entry_idx, i, balance)
                        balance += pnl
                        log.append(record)
                        trade_idx += 1
                        position, entry_idx, was_overbought = 0, None, False

            elif position == -1:
                liq_p = self._check_liquidation("Short", entry_idx, i, df)
                if liq_p is not None:
                    record, pnl = self._make_trade_record(trade_idx, "Short", df, entry_idx, i, balance, liquidated=True, exit_price=liq_p)
                    balance += pnl
                    log.append(record)
                    trade_idx += 1
                    position, entry_idx, was_oversold = 0, None, False
                else:
                    if ki <= oversold:
                        was_oversold = True
                    if (ki > oversold and ki > di) or (was_oversold and ki > oversold):
                        record, pnl = self._make_trade_record(trade_idx, "Short", df, entry_idx, i, balance)
                        balance += pnl
                        log.append(record)
                        trade_idx += 1
                        position, entry_idx, was_oversold = 0, None, False

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


class AdvRSICombinedStrategy(Strategy):
    """Long: sRSI-K crosses above oversold with RSI > MA and MA < 50.
    Short: sRSI-K crosses below overbought with RSI < MA and MA > 50.
    Long exit: KD diff < -10, sRSI-D drops below overbought, RSI near 50 with sRSI-K < overbought and KD diff < 0, or sRSI-K crosses below oversold from within the band.
    Short exit: KD diff > 10, sRSI-D rises above oversold, RSI near 50 with sRSI-K > oversold and KD diff > 0, or sRSI-K crosses above overbought from within the band.
    Multiple concurrent positions are allowed."""

    KD_EXIT_THRESHOLD = 10

    def run(self, df: pd.DataFrame, indicators: IndicatorSet, live_start_idx: int = 0) -> pd.DataFrame:
        k, d = indicators.stoch_k, indicators.stoch_d
        rsi, rsi_ma = indicators.rsi, indicators.rsi_ma
        kd_diff = indicators.kd_diff
        oversold, overbought = self.config.stoch_oversold, self.config.stoch_overbought
        open_longs: list[int] = []
        open_shorts: list[int] = []
        trade_idx = 0
        balance = 0.0
        log = []
        waiting_long = False
        waiting_short = False
        oversold_since: int | None = None
        overbought_since: int | None = None

        for i in range(live_start_idx, len(df)):
            ki, di = k.iloc[i], d.iloc[i]
            ri, ri_ma = rsi.iloc[i], rsi_ma.iloc[i]
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

            if ki < oversold:
                if oversold_since is None:
                    oversold_since = i
                overbought_since = None
                waiting_short = False
                if i - oversold_since >= 5:
                    waiting_long = True
            elif ki > overbought:
                if overbought_since is None:
                    overbought_since = i
                oversold_since = None
                waiting_long = False
                if i - overbought_since >= 5:
                    waiting_short = True
            else:
                oversold_since = None
                overbought_since = None

            if waiting_long and ki >= oversold:
                if pd.notna(ri) and pd.notna(ri_ma) and ri > ri_ma and ri_ma < 50:
                    open_longs.append(i)
                waiting_long = False
            elif waiting_short and ki <= overbought:
                if pd.notna(ri) and pd.notna(ri_ma) and ri < ri_ma and ri_ma > 50:
                    open_shorts.append(i)
                waiting_short = False

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


class ClaudeSuggestedStrategy(Strategy):
    """Trend-regime-filtered momentum strategy.

    Entry: StochRSI exits extreme zone + K/D aligned + price above/below EMA-50 + MACD histogram agrees.
    Exit: K/D reversal after touching target zone, OR EMA-50 trend break, OR MACD histogram zero-line flip.

    The EMA-50 filter prevents trading against the dominant trend; MACD ensures macro momentum
    agrees before entry and signals the exit when that momentum exhausts.
    """

    def run(self, df: pd.DataFrame, indicators: IndicatorSet, live_start_idx: int = 0) -> pd.DataFrame:
        k, d = indicators.stoch_k, indicators.stoch_d
        ema = indicators.ema_trend
        macd_hist = indicators.macd_hist
        close = df["Close"]

        oversold = self.config.stoch_oversold
        overbought = self.config.stoch_overbought

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
