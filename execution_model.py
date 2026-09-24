"""Causal OHLC research simulator. Live exchange strategy execution is unchanged.

Signals observe candle closes. Orders fill at the next open. Intrabar liquidation
precedes close signals; a gap beyond the threshold fills at the worse open.
OHLC cannot establish the true mark price or intrabar path. Costs are explicit
scenario assumptions, not exchange fee quotes. No forced last-candle sale.
"""
from dataclasses import asdict, dataclass
import hashlib
import math

import numpy as np
import pandas as pd

from accounting import adverse_fill, gross_pnl, liquidation_price
from config import Config
from indicators import Indicators
from strategy import (AdvRSICombinedStrategy, ExtremeReversalStrategy,
                      PARAMS_CLASS, MAX_HOLD_DAYS, _fmt_time)

MODEL_VERSION = "ohlc-net-v1"
WARMUP_CANDLES = 300


@dataclass(frozen=True)
class ExecutionConfig:
    entry_fee_rate: float = 0.0
    exit_fee_rate: float = 0.0
    slippage_bps: float = 0.0
    holding_rate_per_day: float = 0.0
    holding_policy: str = "none"
    position_mode: str = "net"
    fill_timing: str = "next_open"
    margin_fraction: float = 1.0
    liquidation_ratio: float = 1.1

    def __post_init__(self):
        for field in ("entry_fee_rate", "exit_fee_rate", "slippage_bps", "holding_rate_per_day"):
            value = getattr(self, field)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{field} must be finite and nonnegative")
        if self.slippage_bps >= 10000 or max(self.entry_fee_rate, self.exit_fee_rate) >= 1:
            raise ValueError("Fees and slippage must be less than 100%")
        if not 0 < self.margin_fraction <= 1:
            raise ValueError("margin_fraction must be in (0, 1]")
        if not math.isfinite(self.liquidation_ratio) or self.liquidation_ratio <= 0:
            raise ValueError("liquidation_ratio must be positive")
        if self.position_mode not in ("net", "multi") or self.fill_timing not in ("next_open", "legacy_close"):
            raise ValueError("Unknown position mode or fill timing")
        if self.holding_policy not in ("none", "entry_notional_daily"):
            raise ValueError("Unknown holding policy")
        if self.holding_policy == "none" and self.holding_rate_per_day:
            raise ValueError("A nonzero holding rate requires an explicit holding policy")


def execution_config(value=None):
    return value if isinstance(value, ExecutionConfig) else ExecutionConfig(**(value or {}))


def required_warmup(params):
    """Conservative indicator history, separate from the evaluated window."""
    values = params if isinstance(params, dict) else asdict(params)
    return max(WARMUP_CANDLES, values.get("ema_trend_length", 0)*3,
               sum(values.get(k, 0) for k in ("rsi_length", "stoch_length", "k_smooth", "d_smooth")),
               values.get("rsi_fast_length", 0)+values.get("rsi_ma_length", 0),
               values.get("macd_slow", 0)+values.get("macd_signal", 0))


def _validate(df):
    if not isinstance(df.index, pd.DatetimeIndex) or not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError("Candles require a unique increasing DatetimeIndex")
    prices = df[["Open", "High", "Low", "Close"]].to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise ValueError("Candle prices must be finite and positive")
    if ((df.High < df[["Open", "Close", "Low"]].max(axis=1)) | (df.Low > df[["Open", "Close", "High"]].min(axis=1))).any():
        raise ValueError("Invalid candle high/low range")


def simulate_signals(df, decide, credit, leverage, execution=None, start_idx=0):
    """`decide(i, positions)` returns (entry direction or None, exit directions).

    Quantity is fixed at entry. Costs debit cash as incurred; margin is reserved,
    never reused. Daily holding scenarios accrue pro rata on entry notional.
    Marked equity includes open P&L; it excludes hypothetical future exit costs.
    """
    _validate(df)
    ex = execution_config(execution)
    if not math.isfinite(credit) or credit <= 0 or not math.isfinite(leverage) or leverage < 1:
        raise ValueError("Credit must be positive and leverage must be at least one")
    if not 0 <= start_idx <= len(df):
        raise ValueError("Evaluation boundary is outside the candle data")
    cash, reserved = float(credit), 0.0
    positions, trades, equity = [], [], []
    pending_entry, pending_exits = None, []
    rejected, fees_total, holding_total, gross_total = 0, 0.0, 0.0, 0.0

    def close(pos, i, raw_price, liquidated=False):
        nonlocal cash, reserved, fees_total, gross_total
        price = adverse_fill(float(raw_price), pos["direction"], False, ex.slippage_bps)
        gross = gross_pnl(pos["direction"], pos["quantity"], pos["entry_price"], price)
        exit_fee = pos["quantity"] * price * ex.exit_fee_rate
        cash += gross - exit_fee
        fees_total += exit_fee
        gross_total += gross
        reserved -= pos["margin"]
        fees = pos["entry_fee"] + exit_fee
        net = gross - fees - pos["holding_cost"]
        trades.append({"Trade #": len(trades), "Position": pos["direction"],
            "Start Time": _fmt_time(df.index[pos["entry_idx"]]), "End Time": _fmt_time(df.index[i]),
            "Start Price": pos["entry_price"], "End Price": price,
            "Candle Count": i-pos["entry_idx"]+1, "Quantity": pos["quantity"],
            "Reserved Margin ($)": pos["margin"], "Gross P&L ($)": gross,
            "Fees ($)": fees, "Borrowing ($)": pos["holding_cost"], "P&L ($)": net,
            "Balance ($)": cash, "Price Change": f"{(price/pos['entry_price']-1)*100:.6f}%",
            "Liquidated": liquidated, "Commit Ratio": (price/pos["entry_price"]+1/leverage if pos["direction"] == "Long" else (leverage+1)*pos["entry_price"]/(leverage*price)),
            "_start_ts": int(df.index[pos["entry_idx"]].timestamp()), "_end_ts": int(df.index[i].timestamp())})
        positions.remove(pos)

    def enter(direction, i, raw_price):
        nonlocal cash, reserved, rejected, fees_total
        if direction is None:
            return
        if direction not in ("Long", "Short"):
            raise ValueError("Invalid entry direction")
        available = max(0.0, cash-reserved)
        if (ex.position_mode == "net" and positions) or available <= 1e-10:
            rejected += 1
            return
        # Fee allowance prevents a full-margin entry being rejected just for fees.
        margin = min(credit * ex.margin_fraction, available / (1 + leverage*ex.entry_fee_rate))
        if positions and margin < credit * ex.margin_fraction - 1e-8:
            rejected += 1
            return
        price = adverse_fill(float(raw_price), direction, True, ex.slippage_bps)
        quantity = margin*leverage/price
        fee = quantity*price*ex.entry_fee_rate
        cash -= fee
        fees_total += fee
        reserved += margin
        positions.append({"direction": direction, "entry_price": price, "entry_idx": i,
                          "entry_time": str(df.index[i]), "quantity": quantity, "margin": margin,
                          "entry_fee": fee, "holding_cost": 0.0, "touched_extreme": False})

    for i in range(start_idx, len(df)):
        row = df.iloc[i]
        # Charge elapsed time only for positions held since the preceding candle.
        for pos in list(positions):
            days = (df.index[i]-df.index[i-1]).total_seconds()/86400 if i else 0
            charge = pos["quantity"]*pos["entry_price"]*ex.holding_rate_per_day*days
            pos["holding_cost"] += charge
            cash -= charge
            holding_total += charge
            threshold = liquidation_price(pos["direction"], pos["entry_price"], leverage, ex.liquidation_ratio)
            gap = row.Open <= threshold if pos["direction"] == "Long" else row.Open >= threshold
            if gap:
                close(pos, i, row.Open, True)
            elif pos["direction"] in pending_exits:
                close(pos, i, row.Open)
        if ex.fill_timing == "next_open":
            enter(pending_entry, i, row.Open)
        pending_entry, pending_exits = None, []
        for pos in list(positions):
            threshold = liquidation_price(pos["direction"], pos["entry_price"], leverage, ex.liquidation_ratio)
            breach = row.Low <= threshold if pos["direction"] == "Long" else row.High >= threshold
            if breach:
                close(pos, i, threshold, True)
        entry, exits = decide(i, positions)
        exits = list(exits)
        for pos in positions:
            if df.index[i] - df.index[pos["entry_idx"]] >= pd.Timedelta(days=MAX_HOLD_DAYS):
                exits.append(pos["direction"])
        if ex.fill_timing == "legacy_close":
            for pos in list(positions):
                if pos["direction"] in exits:
                    close(pos, i, row.Close)
            enter(entry, i, row.Close)
        else:
            pending_entry, pending_exits = entry, exits
        unrealized = sum(gross_pnl(p["direction"], p["quantity"], p["entry_price"], row.Close) for p in positions)
        equity.append({"time": int(df.index[i].timestamp()), "equity": cash+unrealized,
                       "balance": cash, "available": cash-reserved, "reserved": reserved})
    pnls = [t["P&L ($)"] for t in trades]
    profits, losses = sum(p for p in pnls if p > 0), sum(p for p in pnls if p < 0)
    total = (equity[-1]["equity"] if equity else credit)-credit
    peak, drawdown = credit, 0.0
    for point in equity:
        peak = max(peak, point["equity"])
        drawdown = max(drawdown, (peak-point["equity"])/peak*100)
    costs_configured = any((ex.entry_fee_rate, ex.exit_fee_rate, ex.slippage_bps, ex.holding_rate_per_day))
    metadata = {"model_version": MODEL_VERSION, "execution": asdict(ex), "costs_configured": costs_configured,
                "cost_assumption": ("User scenario; holding charges use entry quote notional pro rata daily; no asset-denominated costs" if costs_configured
                                    else "Zero-cost comparison: fees, slippage and holding charges are all zero; configure costs for net-cost research"),
                "data_hash": hashlib.sha256(pd.util.hash_pandas_object(df, index=True).values.tobytes()).hexdigest(),
                "data_source": df.attrs.get("data_source", "provided"), "start_idx": start_idx,
                "window_start": str(df.index[start_idx]) if start_idx < len(df) else None,
                "window_end": str(df.index[-1]) if len(df) else None}
    summary = {"profitable": total > 0 if equity else None, "total_pnl": total, "realized_pnl": cash-credit,
               "closed_net_pnl": sum(pnls), "gross_pnl": gross_total + total-(cash-credit),
               "fees": fees_total, "borrowing": holding_total, "equity": credit+total,
               "available_capital": cash-reserved, "reserved_margin": reserved,
               "max_drawdown_pct": drawdown, "trade_count": len(trades), "open_count": len(positions),
               "win_rate": 100*sum(p > 0 for p in pnls)/len(pnls) if pnls else 0,
               "candle_count": len(df)-start_idx, "rejected_entries": rejected,
               "long_count": sum(t["Position"] == "Long" for t in trades), "short_count": sum(t["Position"] == "Short" for t in trades),
               "long_pct": 100*sum(t["Position"] == "Long" for t in trades)/len(trades) if trades else 0,
               "sum_profits": profits, "sum_losses": losses,
               "profit_efficiency": 100*sum(pnls)/(profits-losses) if profits != losses else None,
               "profit_share": 100*profits/(profits-losses) if profits != losses else None,
               "price_changes": [float(t["Price Change"][:-1]) for t in trades], "metadata": metadata}
    return {"trades": trades, "trades_df": pd.DataFrame(trades), "open_positions": positions,
            "summary": summary, "equity_curve": equity, "metadata": metadata}


class _Decisions:
    """Incremental strategy decisions; fill state comes exclusively from simulator."""
    def __init__(self, df, name, params):
        self.df, self.name, self.p = df, name, params
        ind = Indicators(df)
        srsi = ind.stoch_rsi(params.rsi_length, params.stoch_length, params.k_smooth, params.d_smooth)
        self.k, self.d = srsi.k, srsi.d
        self.waiting_long = self.waiting_short = False
        if name == "TrendMomentum":
            self.ema = ind.ema(params.ema_trend_length)
            self.hist = ind.macd(params.macd_fast, params.macd_slow, params.macd_signal).hist
        else:
            cls = AdvRSICombinedStrategy if name == "AdvRSI" else ExtremeReversalStrategy
            self.strategy = cls(Config(), params)
            self.strategy._init_entry_state()
            self.rsi, self.ma = ind.rsi_with_ma(params.rsi_fast_length, params.rsi_ma_length)
            self.diff = self.k-self.d

    def __call__(self, i, positions):
        p = self.p
        k, d = self.k.iloc[i], self.d.iloc[i]
        pk, pd_ = self.k.iloc[max(0,i-1)], self.d.iloc[max(0,i-1)]
        if self.name != "TrendMomentum":
            r, diff = self.rsi.iloc[i], self.diff.iloc[i]
            lo, hi = getattr(p, "exit_oversold", p.oversold), getattr(p, "exit_overbought", p.overbought)
            exits = []
            if (pd_ >= hi and d < hi) or (k < hi and diff < -.08*k and 45 < r < 55) or (pk >= lo and k < lo):
                exits.append("Long")
            if (pd_ <= lo and d > lo) or (k > lo and diff > .08*k and 45 < r < 55) or (pk <= hi and k > hi):
                exits.append("Short")
            signal = self.strategy._entry_signal(i, self.k, self.d, self.rsi, self.ma, self.diff)
            return signal.title() if signal else None, exits
        ema, hist, prev_hist = self.ema.iloc[i], self.hist.iloc[i], self.hist.iloc[max(0,i-1)]
        price = self.df.Close.iloc[i]
        ready = all(pd.notna(x) for x in (ema, hist, prev_hist, k, d))
        exits = []
        for pos in positions:
            long = pos["direction"] == "Long"
            if (k >= p.overbought if long else k <= p.oversold):
                pos["touched_extreme"] = True
            kd = pos["touched_extreme"] and (k < d if long else k > d)
            trend = ready and (price < ema if long else price > ema)
            flip = ready and ((prev_hist >= 0 and hist < 0) if long else (prev_hist <= 0 and hist > 0))
            if kd or trend or flip:
                exits.append(pos["direction"])
        entry = None
        if not positions or all(pos["direction"] in exits for pos in positions):
            if k < p.oversold and d < p.oversold:
                self.waiting_long, self.waiting_short = True, False
            elif k > p.overbought and d > p.overbought:
                self.waiting_short, self.waiting_long = True, False
            if self.waiting_long and k >= p.oversold:
                if ready and k > d and price > ema and hist > 0:
                    entry = "Long"
                self.waiting_long = False
            elif self.waiting_short and k <= p.overbought:
                if ready and k < d and price < ema and hist < 0:
                    entry = "Short"
                self.waiting_short = False
        return entry, exits


def evaluate_research(df, strategy_name, credit, leverage, custom_params=None, execution=None, start_idx=None):
    params = custom_params if custom_params is not None and not isinstance(custom_params, dict) else PARAMS_CLASS[strategy_name](**(custom_params or {}))
    if start_idx is None:
        start_idx = min(len(df), required_warmup(params))
    result = simulate_signals(df, _Decisions(df, strategy_name, params), credit, leverage, execution, start_idx)
    result["metadata"].update(strategy=strategy_name, parameters=asdict(params))
    return result
