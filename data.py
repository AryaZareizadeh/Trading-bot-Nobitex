"""Closed OHLCV history shared by bots, research, and model training."""
import hashlib
import json
import math
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from candle_store import CandleStore, candle_close_time, canonical_interval
from config import Config
from nobitex_http import direct_nobitex_session


class DataFetcher:
    _NOBITEX_URL = "https://apiv2.nobitex.ir/market/udf/history"
    _BINANCE_URL = "https://api.binance.com/api/v3/klines"
    _NOBITEX_PAGE = 500
    _BINANCE_PAGE = 1000

    # Compatibility mappings for existing consumers. Monthly closure uses the
    # provider's closing time or calendar, never this duration estimate.
    _RESOLUTION_SECONDS = {
        "1": 60, "5": 300, "15": 900, "30": 1800, "60": 3600,
        "180": 10800, "240": 14400, "D": 86400, "W": 604800, "M": 2592000,
    }
    _BINANCE_INTERVALS = {
        "1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1h",
        "180": "3h", "240": "4h", "D": "1d", "W": "1w", "M": "1M",
    }
    _NOBITEX_RESOLUTIONS = {interval: resolution for resolution, interval in _BINANCE_INTERVALS.items()}

    def __init__(self, config: Config, cancel=None, cache=None):
        self.config = config
        self._cancel = cancel
        self.exchange = getattr(config, "exchange", "nobitex").lower()
        if self.exchange not in {"nobitex", "binance"}:
            raise ValueError("Unsupported candle exchange")
        self._interval = canonical_interval(config.interval)
        self._resolution = self._NOBITEX_RESOLUTIONS[self._interval]
        self._instrument = config.symbol.upper()
        self._cache = cache if cache is not None else CandleStore()
        # Binance keeps environment/VPN proxies; Nobitex always connects directly.
        self._session = direct_nobitex_session() if self.exchange == "nobitex" else requests.Session()

    def _check_cancel(self):
        if self._cancel is not None and self._cancel():
            raise RuntimeError("Historical data fetch cancelled")

    def fetch(self) -> pd.DataFrame:
        return self._fetch_cached(int(self.config.limit))

    def fetch_latest(self) -> pd.DataFrame:
        """Return the latest closed candle, fetching only if another has closed."""
        return self._fetch_cached(1)

    def _read(self, now, limit=None):
        return self._cache.read(self.exchange, self._instrument, self._interval,
                                as_of=now, limit=limit)

    def _save(self, rows, now):
        self._cache.save(self.exchange, self._instrument, self._interval, rows,
                         as_of=now)

    @property
    def _page_size(self):
        return self._BINANCE_PAGE if self.exchange == "binance" else self._NOBITEX_PAGE

    def _span(self, count):
        # Only a bound for backwards pagination; never determines monthly closure.
        duration = (31 * 86400 if self._interval == "1M" else
                    candle_close_time(self._interval, 0))
        return duration * count

    def _closed_frontier(self, last, now):
        """Latest expected closing boundary, anchored to exchange timestamps."""
        boundary = last["close_time"]
        if self._interval == "1M" or (self.exchange == "nobitex" and self._interval in {"1d", "1w"}):
            while True:
                following = candle_close_time(self._interval, boundary, exchange=self.exchange)
                if following > now:
                    return boundary
                boundary = following
        duration = candle_close_time(self._interval, last["open_time"]) - last["open_time"]
        return boundary + max(0, (now - boundary) // duration) * duration

    def _fetch_cached(self, total):
        self._check_cancel()
        if total <= 0:
            return self._frame([])
        with self._cache.series_lock(self.exchange, self._instrument, self._interval):
            self._check_cancel()
            now = int(time.time())
            saved = self._read(now, total)
            if saved:
                last = saved[-1]
                frontier = self._closed_frontier(last, now)
                if frontier > last["close_time"]:
                    self._fill_range(last["close_time"], frontier, now)
                saved = self._read(now, total)
                # Count alone does not establish consecutive history.
                self._fill_range(saved[0]["open_time"], saved[-1]["close_time"], now)
            self._backfill(total, now)
            saved = self._read(now, total)
            if saved:
                self._fill_range(saved[0]["open_time"], saved[-1]["close_time"], now)
            return self._frame(self._read(now, total))

    def _fill_range(self, start, end, now):
        """Fetch gaps in small bounded windows; never request saved neighbors."""
        cursor = start
        while cursor < end:
            self._check_cancel()
            gaps = self._cache.missing_ranges(self.exchange, self._instrument,
                                              self._interval, cursor, end, now=now)
            if not gaps:
                break
            gap_start, gap_end = gaps[0]
            stop = min(gap_end, gap_start + self._span(self._page_size))
            previous = self._cache.missing_ranges(self.exchange, self._instrument,
                                                  self._interval, gap_start, stop, now=now)
            # Include the next monthly opening as proof of the prior closure.
            before = stop + 2 if self.exchange == "nobitex" and self._interval == "1M" else stop
            rows, received = self._download(before, self._page_size, now, start=gap_start)
            self._save(rows, now)
            if received == 0:
                self._cache.remember_empty(self.exchange, self._instrument,
                                           self._interval, gap_start, stop, now=now)
            remaining = self._cache.missing_ranges(self.exchange, self._instrument,
                                                   self._interval, gap_start, stop, now=now)
            # Continue a partial page at its remaining gap while it makes
            # progress. An unchanged response stays retryable without a loop.
            cursor = gap_start if remaining != previous else stop

    def _backfill(self, total, now):
        saved = self._read(now, total)
        while len(saved) < total:
            self._check_cancel()
            before = saved[0]["open_time"] if saved else now
            count = min(self._page_size, total - len(saved) + (1 if not saved else 0))
            lower = max(0, before - self._span(count + 1))
            if lower >= before or not self._cache.missing_ranges(
                    self.exchange, self._instrument, self._interval, lower, before, now=now):
                break
            rows, received = self._download(before, count, now)
            self._save(rows, now)
            self._check_cancel()
            updated = self._read(now, total)
            if received == 0:
                self._cache.remember_empty(self.exchange, self._instrument,
                                           self._interval, lower, before, now=now)
                break
            if not rows:
                # A response containing only an unfinished/unproven candle is
                # neither usable history nor a verified empty historical range.
                break
            oldest = min(r["open_time"] for r in rows)
            if oldest >= before or len(updated) == len(saved):
                # Never loop on an endpoint that has stopped making progress.
                break
            saved = updated

    def _download(self, before, count, now, *, start=None):
        self._check_cancel()
        before = int(before)
        start = int(start) if start is not None else None
        if self.exchange == "binance":
            params = {"symbol": self._instrument, "interval": self._interval,
                      "limit": min(count, self._BINANCE_PAGE), "endTime": before * 1000 - 1}
            if start is not None:
                params["startTime"] = start * 1000
            response = self._session.get(self._BINANCE_URL, params=params, timeout=15)
        else:
            params = {"symbol": self._instrument.lower(), "resolution": self._resolution,
                      "to": before - 1}
            if start is None:
                params["countback"] = min(count, self._NOBITEX_PAGE)
            else:
                # countback takes precedence over from on Nobitex.
                params["from"] = start
            response = self._session.get(self._NOBITEX_URL, params=params, timeout=15)
        response.raise_for_status()
        payload = response.json()
        if self.exchange == "binance":
            if not isinstance(payload, list):
                raise ValueError("Unexpected Binance candle response")
            raw = payload
        else:
            if not isinstance(payload, dict):
                raise ValueError("Unexpected Nobitex candle response")
            if payload.get("s") == "no_data":
                return [], 0
            if payload.get("s") != "ok":
                raise ValueError("Nobitex candle request failed")
            arrays = [payload.get(name) for name in ("t", "o", "h", "l", "c", "v")]
            if any(not isinstance(a, list) for a in arrays) or len({len(a) for a in arrays}) != 1:
                raise ValueError("Inconsistent Nobitex candle response")
            raw = list(zip(*arrays))
        return self._normalize(raw, now), len(raw)

    def _normalize(self, raw, now):
        rows = []
        nobitex_opens = sorted({int(r[0]) for r in raw}) if self.exchange == "nobitex" else []
        if self._interval == "1M" and nobitex_opens:
            # A later monthly candle saved by another page/feature also proves
            # the last row of an older page has already closed.
            later = self._cache.read(self.exchange, self._instrument, self._interval,
                                     start=nobitex_opens[-1] + 1, as_of=now, limit=1)
            if later:
                nobitex_opens.append(later[0]["open_time"])
        following = dict(zip(nobitex_opens, nobitex_opens[1:]))
        for row in raw:
            if len(row) < 6:
                raise ValueError("Incomplete OHLCV candle")
            opened = int(row[0]) // 1000 if self.exchange == "binance" else int(row[0])
            values = [float(v) for v in row[1:6]]
            if not all(math.isfinite(v) for v in values):
                raise ValueError("Non-finite OHLCV candle")
            if (min(values[:4]) <= 0 or values[4] < 0 or
                    values[1] < max(values[0], values[3]) or values[2] > min(values[0], values[3])):
                raise ValueError("Inconsistent OHLCV candle")
            if self.exchange == "binance" and len(row) > 6:
                closed = (int(row[6]) + 1) // 1000
            elif self.exchange == "nobitex" and self._interval == "1M":
                # A subsequent candle proves closure for this undocumented interval.
                if opened not in following:
                    continue
                closed = candle_close_time(self._interval, opened, exchange=self.exchange)
                if following[opened] < closed:
                    continue
            else:
                closed = candle_close_time(self._interval, opened, exchange=self.exchange)
            metadata = {
                "retrieved_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "data_hash": hashlib.sha256(json.dumps(list(row), separators=(",", ":")).encode()).hexdigest(),
            }
            rows.append({"open_time": opened, "close_time": closed,
                         **dict(zip(("open", "high", "low", "close", "volume"), values)),
                         "metadata": metadata})
        return rows

    @staticmethod
    def _frame(rows):
        frame = pd.DataFrame([[r[name] for name in ("open", "high", "low", "close", "volume")]
                              for r in rows], columns=["Open", "High", "Low", "Close", "Volume"], dtype=float)
        frame.index = pd.to_datetime([r["open_time"] for r in rows], unit="s")
        frame.index.name = "Date"
        return frame
