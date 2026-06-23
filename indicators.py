import pandas as pd
import talib

from config import Config


class IndicatorSet:
    def __init__(self, df: pd.DataFrame, config: Config):
        self.config = config
        close = df["Close"]

        # Slow RSI used internally for stochastic; fast RSI used in strategies/plotting
        self._srsi_base, self.raw_stoch, self.stoch_k, self.stoch_d = self._stochastic_rsi(close)
        self.rsi, self.rsi_ma = self._rsi_with_ma(close)
        self.kd_diff = (self.stoch_k - self.stoch_d).fillna(0)
        self.ema_trend = self._ema(close, config.ema_trend_length)
        self.macd_line, self.macd_signal_line, self.macd_hist = self._macd(close)

    def _stochastic_rsi(
        self, series: pd.Series
    ) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        rsi = pd.Series(
            talib.RSI(series.values, timeperiod=self.config.rsi_length),
            index=series.index,
        )
        lowest = rsi.rolling(window=self.config.stoch_length, min_periods=1).min()
        highest = rsi.rolling(window=self.config.stoch_length, min_periods=1).max()
        raw_stoch = 100 * (rsi - lowest) / (highest - lowest)
        k = raw_stoch.rolling(window=self.config.k_smooth, min_periods=1).mean()
        d = k.rolling(window=self.config.d_smooth, min_periods=1).mean()
        return rsi, raw_stoch, k, d

    def _rsi_with_ma(self, series: pd.Series) -> tuple[pd.Series, pd.Series]:
        rsi = pd.Series(
            talib.RSI(series.values, timeperiod=self.config.rsi_fast_length),
            index=series.index,
        )
        rsi_ma = pd.Series(
            talib.SMA(rsi.values, timeperiod=self.config.rsi_ma_length),
            index=series.index,
        )
        return rsi, rsi_ma

    def _ema(self, series: pd.Series, period: int) -> pd.Series:
        return pd.Series(talib.EMA(series.values, timeperiod=period), index=series.index)

    def _macd(self, series: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
        macd, signal, hist = talib.MACD(
            series.values,
            fastperiod=self.config.macd_fast,
            slowperiod=self.config.macd_slow,
            signalperiod=self.config.macd_signal,
        )
        return (
            pd.Series(macd, index=series.index),
            pd.Series(signal, index=series.index),
            pd.Series(hist, index=series.index),
        )
