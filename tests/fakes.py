"""Deterministic candles and a scripted exchange; no credentials or network."""

from collections import defaultdict, deque
from copy import deepcopy

import numpy as np
import pandas as pd


def synthetic_candles(count=500):
    x = np.arange(count, dtype=float)
    close = 100 + 12 * np.sin(x / 12) + 3 * np.sin(x / 3) + x * 0.015
    opening = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"Open": opening, "High": np.maximum(opening, close) + 1,
                         "Low": np.minimum(opening, close) - 1, "Close": close,
                         "Volume": np.full(count, 1000.0)},
                        index=pd.date_range("2024-01-01", periods=count, freq="h", name="Date"))


def momentum_candles(count=500):
    """Seeded trend/pullback history that exercises momentum entry and exit rules."""
    candles = synthetic_candles(count)
    close = 100 + np.cumsum(np.random.default_rng(42).normal(0.05, 1, count))
    candles["Close"] = close
    candles["Open"] = np.r_[close[0], close[:-1]]
    candles["High"] = candles[["Open", "Close"]].max(axis=1) + 1
    candles["Low"] = candles[["Open", "Close"]].min(axis=1) - 1
    return candles


class FakeExchange:
    """Queue responses/exceptions per method and record every invocation."""

    def __init__(self):
        self.calls = []
        self.responses = defaultdict(deque)

    def queue(self, method, *responses):
        self.responses[method].extend(responses)
        return self

    def _call(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        if not self.responses[method]:
            raise AssertionError(f"Unexpected exchange call: {method}")
        response = self.responses[method].popleft()
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self._call(name, *args, **kwargs)
