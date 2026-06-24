from dataclasses import dataclass

import pandas as pd
import talib


@dataclass(frozen=True)
class StochRsi:
    """Grouped Stochastic-RSI output."""
    raw: pd.Series
    k: pd.Series
    d: pd.Series


@dataclass(frozen=True)
class Macd:
    """Grouped MACD output."""
    line: pd.Series
    signal: pd.Series
    hist: pd.Series


class Indicators:
    """Parameterized, cached technical-indicator provider.

    One instance is built per DataFrame and shared across strategies. Every
    indicator is computed on demand and memoized by its parameters, so two
    strategies requesting the same indicator with identical parameters share a
    single computation, while differing parameters are computed and cached
    independently. This lets multiple strategies reuse the same indicator with
    their own inputs.
    """

    def __init__(self, df: pd.DataFrame):
        self._close = df["Close"]
        self._index = df.index
        self._cache: dict = {}

    def _memo(self, key, compute):
        if key not in self._cache:
            self._cache[key] = compute()
        return self._cache[key]

    # ── Primitives ─────────────────────────────────────────────────────────────

    def rsi(self, length: int) -> pd.Series:
        return self._memo(
            ("rsi", length),
            lambda: pd.Series(
                talib.RSI(self._close.values, timeperiod=length), index=self._index
            ),
        )

    def ema(self, period: int) -> pd.Series:
        return self._memo(
            ("ema", period),
            lambda: pd.Series(
                talib.EMA(self._close.values, timeperiod=period), index=self._index
            ),
        )

    # ── Composites ─────────────────────────────────────────────────────────────

    def rsi_with_ma(self, rsi_length: int, ma_length: int) -> tuple[pd.Series, pd.Series]:
        def compute():
            rsi = self.rsi(rsi_length)
            rsi_ma = pd.Series(
                talib.SMA(rsi.values, timeperiod=ma_length), index=self._index
            )
            return rsi, rsi_ma

        return self._memo(("rsi_with_ma", rsi_length, ma_length), compute)

    def stoch_rsi(
        self, rsi_length: int, stoch_length: int, k_smooth: int, d_smooth: int
    ) -> StochRsi:
        def compute():
            rsi = self.rsi(rsi_length)
            lowest = rsi.rolling(window=stoch_length, min_periods=1).min()
            highest = rsi.rolling(window=stoch_length, min_periods=1).max()
            raw = 100 * (rsi - lowest) / (highest - lowest)
            k = raw.rolling(window=k_smooth, min_periods=1).mean()
            d = k.rolling(window=d_smooth, min_periods=1).mean()
            return StochRsi(raw=raw, k=k, d=d)

        return self._memo(
            ("stoch_rsi", rsi_length, stoch_length, k_smooth, d_smooth), compute
        )

    def macd(self, fast: int, slow: int, signal: int) -> Macd:
        def compute():
            line, sig, hist = talib.MACD(
                self._close.values,
                fastperiod=fast,
                slowperiod=slow,
                signalperiod=signal,
            )
            return Macd(
                line=pd.Series(line, index=self._index),
                signal=pd.Series(sig, index=self._index),
                hist=pd.Series(hist, index=self._index),
            )

        return self._memo(("macd", fast, slow, signal), compute)
