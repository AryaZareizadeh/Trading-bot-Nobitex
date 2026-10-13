"""Public spot candles for portfolio research. No exchange credentials are used."""

from datetime import datetime, timezone
import hashlib
import json
import math
import time

import requests

from candle_store import candle_close_time
from nobitex_http import direct_nobitex_session


APPROVED_ASSETS = ("BTC", "ETH", "BNB", "XRP", "SOL", "ZEC", "HYPE", "ADA",
                   "XLM", "NEAR", "UNI", "LTC", "SUI", "ENA", "XAUT")
SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
KUCOIN_INTERVAL = {"15m": "15min", "1h": "1hour", "4h": "4hour", "1d": "1day"}
BITFINEX_INTERVAL = {"15m": "15m", "1h": "1h", "1d": "1D"}


def source_for(asset, *, live=False):
    return "binance" if live else {"HYPE": "kucoin", "XAUT": "bitfinex"}.get(asset, "binance")


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def normalize_candles(source, asset, interval, rows, retrieved_at=None):
    """Map provider-specific OHLCV order and timestamp units into UTC bars.

    A candle is usable only after its entire interval has closed. Duplicate and
    malformed rows remain visible in the audit; they are never silently spliced.
    """
    if source not in {"binance", "kucoin", "bitfinex"} or interval not in SECONDS:
        raise ValueError("Unsupported spot source or interval")
    if asset not in APPROVED_ASSETS:
        raise ValueError("Asset is outside the approved universe")
    now = time.time() if retrieved_at is None else retrieved_at
    result = []
    for row in rows:
        if source == "binance":
            start = int(row[0]) / 1000
            opening, high, low, closing, volume = row[1:6]
            instrument = asset + "USDT"
            # Binance supplies the inclusive close in milliseconds. Six-field
            # fixtures and compatible endpoints still use the interval length.
            closes = (int(row[6]) + 1) / 1000 if len(row) > 6 else start + SECONDS[interval]
        elif source == "kucoin":
            start = int(row[0])
            opening, closing, high, low, volume = row[1:6]
            instrument = asset + "-USDT"
            closes = start + SECONDS[interval]
        else:
            start = int(row[0]) / 1000
            opening, closing, high, low, volume = row[1:6]
            instrument = "tXAUT:UST"
            closes = start + SECONDS[interval]
        values = tuple(float(v) for v in (opening, high, low, closing, volume))
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Non-finite candle")
        digest = hashlib.sha256(json.dumps(row, separators=(",", ":")).encode()).hexdigest()
        result.append({"asset": asset, "exchange": source, "instrument": instrument,
                       "quote": "USDT", "interval": interval, "open_time": _iso(start),
                       "available_at": _iso(closes),
                       "retrieved_at": _iso(now), "open": values[0], "high": values[1],
                       "low": values[2], "close": values[3], "volume_base": values[4],
                       "data_hash": digest, "schema_version": 1})
    return result


def audit_candles(candles, interval, *, as_of=None):
    """Report missing/duplicate periods and suspect bars without filling them."""
    step = SECONDS[interval]
    as_of = as_of or datetime.now(timezone.utc).timestamp()
    ordered = sorted(candles, key=lambda bar: bar["open_time"])
    issues = []
    previous = None
    previous_available = None
    valid = []
    for bar in ordered:
        opened = datetime.fromisoformat(bar["open_time"]).timestamp()
        available = datetime.fromisoformat(bar["available_at"]).timestamp()
        if previous is not None:
            delta = opened - previous
            if delta == 0:
                issues.append({"time": bar["open_time"], "kind": "duplicate"})
                continue
            if interval == "1d" and bar["exchange"] == "nobitex":
                # Historical Tehran calendar days can last 23 or 25 hours.
                # The prior close is the next expected opening, not open+24h.
                boundary, missing = previous_available, 0
                while boundary < opened:
                    missing += 1
                    boundary = candle_close_time(interval, int(boundary), exchange="nobitex")
                if missing:
                    issues.append({"time": bar["open_time"], "kind": "gap", "missing_intervals": missing})
            elif delta > step:
                issues.append({"time": bar["open_time"], "kind": "gap", "missing_intervals": int(delta // step) - 1})
        previous = opened
        previous_available = available
        if available > as_of:
            issues.append({"time": bar["open_time"], "kind": "incomplete"})
            continue
        if min(bar["open"], bar["high"], bar["low"], bar["close"]) <= 0 or bar["volume_base"] < 0 or bar["high"] < max(bar["open"], bar["close"]) or bar["low"] > min(bar["open"], bar["close"]):
            issues.append({"time": bar["open_time"], "kind": "invalid_ohlcv"})
            continue
        if bar["high"] / bar["low"] > 2:
            issues.append({"time": bar["open_time"], "kind": "suspect_outlier"})
            continue
        valid.append(bar)
    return {"first": ordered[0]["open_time"] if ordered else None,
            "last": ordered[-1]["open_time"] if ordered else None,
            "received": len(candles), "valid": len(valid), "issues": issues,
            "candles": valid}


def _instrument(source, asset):
    if source == "kucoin":
        return asset + "-USDT"
    return "tXAUT:UST" if source == "bitfinex" else asset + "USDT"


def _cache_rows(candles):
    return [{"open_time": datetime.fromisoformat(bar["open_time"]).timestamp(),
             "close_time": datetime.fromisoformat(bar["available_at"]).timestamp(),
             "open": bar["open"], "high": bar["high"], "low": bar["low"],
             "close": bar["close"], "volume": bar["volume_base"], "metadata": bar}
            for bar in candles]


def _cached_bar(row, source, asset, interval):
    """Older fetchers have OHLCV alone; reconstruct the public audit schema."""
    metadata = row.get("metadata") or {}
    digest = hashlib.sha256(json.dumps(
        [row[key] for key in ("open_time", "close_time", "open", "high", "low", "close", "volume")],
        separators=(",", ":")).encode()).hexdigest()
    retrieved = metadata.get("retrieved_at", _iso(row["close_time"]))
    if isinstance(retrieved, (int, float)):
        retrieved = _iso(retrieved)
    return {"asset": asset, "exchange": source, "instrument": _instrument(source, asset),
            "quote": "USDT", "interval": interval, "open_time": _iso(row["open_time"]),
            "available_at": _iso(row["close_time"]), "retrieved_at": retrieved,
            "open": row["open"], "high": row["high"], "low": row["low"],
            "close": row["close"], "volume_base": row["volume"],
            "data_hash": metadata.get("data_hash", digest), "schema_version": 1}


def _cached_audit(cache, source, asset, interval, start, end, now, downloaded):
    rows = cache.read(source, _instrument(source, asset), interval,
                      start=start, end=end, as_of=now)
    candles = [_cached_bar(row, source, asset, interval) for row in rows]
    cached_times = {bar["open_time"] for bar in candles}
    seen = set()
    for bar in downloaded:
        opened = datetime.fromisoformat(bar["open_time"]).timestamp()
        if not start <= opened < end:
            continue
        # Invalid/open rows and provider duplicates remain available to the
        # audit. A valid candle already on disk keeps its original values.
        if bar["open_time"] not in cached_times or bar["open_time"] in seen:
            candles.append(bar)
        seen.add(bar["open_time"])
    return audit_candles(candles, interval, as_of=min(end, now))


def _range_start(cache, source, instrument, interval, start, end, now):
    # A caller can begin partway through a provider interval. Once its first
    # candle is known, that shorter prefix cannot contain another opening.
    prefix_end = (candle_close_time(interval, int(math.ceil(start)), exchange=source)
                  if source == "nobitex" and interval == "1d" else start + SECONDS[interval])
    first = cache.read(source, instrument, interval, start=start,
                       end=min(end, prefix_end), as_of=now)
    return first[0]["open_time"] if first else start


class PublicSpotData:
    """Bounded, time-paginated public history. Missing periods remain missing."""

    def __init__(self, session=None, cache=None):
        self.session = session or requests.Session()
        if cache is None:
            from candle_store import CandleStore
            cache = CandleStore()
        self.cache = cache

    def fetch(self, asset, interval, start, end, *, live=False):
        if asset not in APPROVED_ASSETS or interval not in SECONDS:
            raise ValueError("Unsupported asset or interval")
        if not 0 < start < end or end - start > 5 * 366 * 86400:
            raise ValueError("Invalid or excessive historical range")
        source = source_for(asset, live=live)
        if source == "bitfinex" and interval not in BITFINEX_INTERVAL:
            raise ValueError("Bitfinex does not support this interval")
        page = 1000 if source == "binance" else 1500 if source == "kucoin" else 10000
        instrument = _instrument(source, asset)
        step = SECONDS[interval]
        downloaded = []
        with self.cache.series_lock(source, instrument, interval):
            now = time.time()
            # These providers use UTC boundaries. The current candle becomes a
            # missing range only after its close, so repeated calls stay local.
            closed_end = min(end, int(now // step) * step)
            range_start = _range_start(self.cache, source, instrument, interval, start, closed_end, now)
            missing = self.cache.missing_ranges(source, instrument, interval,
                                                range_start, closed_end, now=now)
            for lower, upper in missing:
                cursor = lower
                while cursor < upper:
                    # Extra returned rows can already have filled a later page
                    # or suffix since the initial cache check.
                    remaining = self.cache.missing_ranges(source, instrument, interval, cursor, upper, now=now)
                    if not remaining:
                        break
                    cursor, hole_end = remaining[0]
                    stop = min(hole_end, cursor + page * step)
                    if source == "binance":
                        url = "https://data-api.binance.vision/api/v3/klines"
                        params = {"symbol": instrument, "interval": interval,
                                  "startTime": int(math.ceil(cursor * 1000)), "endTime": int(math.ceil(stop * 1000)) - 1, "limit": 1000}
                    elif source == "kucoin":
                        url = "https://api.kucoin.com/api/v1/market/candles"
                        params = {"symbol": instrument, "type": KUCOIN_INTERVAL[interval],
                                  "startAt": int(math.ceil(cursor)), "endAt": int(math.ceil(stop)) - 1}
                    else:
                        url = f"https://api-pub.bitfinex.com/v2/candles/trade:{BITFINEX_INTERVAL[interval]}:{instrument}/hist"
                        params = {"start": int(math.ceil(cursor * 1000)), "end": int(math.ceil(stop * 1000)) - 1,
                                  "limit": 10000, "sort": 1}
                    response = self.session.get(url, params=params, timeout=20)
                    response.raise_for_status()
                    payload = response.json()
                    if source == "kucoin":
                        if not isinstance(payload, dict) or payload.get("code") != "200000":
                            raise ValueError("KuCoin candle request failed")
                        batch = payload.get("data")
                    else:
                        batch = payload
                    if not isinstance(batch, list):
                        raise ValueError("Unexpected candle response")
                    normalized = normalize_candles(source, asset, interval, batch, now)
                    self.cache.save(source, instrument, interval, _cache_rows(normalized), as_of=now)
                    downloaded.extend(normalized)
                    if not batch:
                        self.cache.remember_empty(source, instrument, interval, cursor, stop, now=now)
                    cursor = stop
            return _cached_audit(self.cache, source, asset, interval, start, end, now, downloaded)


class NobitexPublicSpot:
    """Public execution-venue observations; deliberately has no order method."""

    def __init__(self, session=None, cache=None):
        self.session = session or direct_nobitex_session()
        if cache is None:
            from candle_store import CandleStore
            cache = CandleStore()
        self.cache = cache
        self._alignments = {}

    def _get(self, path, **params):
        response = self.session.get("https://apiv2.nobitex.ir" + path,
                                    params=params or None, timeout=20)
        response.raise_for_status()
        return response.json()

    def books(self):
        payload = self._get("/v3/orderbook/all")
        if payload.get("status") != "ok":
            raise ValueError("Nobitex books unavailable")
        return {asset: payload[asset + "USDT"] for asset in APPROVED_ASSETS
                if asset + "USDT" in payload}

    def rules(self):
        payload = self._get("/v2/options")
        if payload.get("status") != "ok":
            raise ValueError("Nobitex market rules unavailable")
        options = payload["nobitex"]
        steps = options.get("amountPrecisions", {})
        minimum = options.get("minOrders", {}).get("usdt")
        if minimum is None:
            raise ValueError("Nobitex minimum USDT order unknown")
        return {"min_notional": str(minimum),
                "quantity_steps": {asset: steps[asset + "USDT"] for asset in APPROVED_ASSETS
                                   if asset + "USDT" in steps}}

    def history(self, asset, interval, start, end):
        """Historical executable-price proxy. Depth cannot be reconstructed."""
        if asset not in APPROVED_ASSETS or interval not in SECONDS:
            raise ValueError("Unsupported Nobitex history request")
        if not 0 < start < end or end - start > 5 * 366 * 86400:
            raise ValueError("Invalid or excessive historical range")
        resolution = {"15m": "15", "1h": "60", "4h": "240", "1d": "D"}[interval]
        instrument = asset + "USDT"
        step = SECONDS[interval]
        downloaded = []
        with self.cache.series_lock("nobitex", instrument, interval):
            now = time.time()
            known = self.cache.read("nobitex", instrument, interval, end=min(end, now), limit=1)
            anchor = self._alignments.get((instrument, interval))
            if known:
                anchor = max(known[-1]["open_time"], anchor) if anchor is not None else known[-1]["open_time"]
            closed_end = min(end, now)
            if anchor is not None and end > now - step:
                if interval == "1d":
                    boundary = anchor
                    following = candle_close_time(interval, boundary, exchange="nobitex")
                    while following <= now:
                        boundary = following
                        following = candle_close_time(interval, boundary, exchange="nobitex")
                    closed_end = min(end, boundary)
                else:
                    closed_end = min(end, anchor + int((now - anchor) // step) * step)
            range_start = _range_start(self.cache, "nobitex", instrument, interval, start, closed_end, now)
            missing = self.cache.missing_ranges("nobitex", instrument, interval,
                                                range_start, closed_end, now=now)
            pages = 0
            for lower, upper in missing:
                cursor = lower
                while cursor < upper:
                    remaining = self.cache.missing_ranges("nobitex", instrument, interval, cursor, upper, now=now)
                    if not remaining:
                        break
                    cursor, hole_end = remaining[0]
                    pages += 1
                    if pages > 1000:
                        raise ValueError("Nobitex history pagination limit reached")
                    stop = min(hole_end, cursor + 500 * step)
                    # countback takes precedence over from at this endpoint.
                    # Bounded windows preserve already cached periods and holes.
                    payload = self._get("/market/udf/history", **{
                        "symbol": instrument.lower(), "resolution": resolution,
                        "from": int(math.ceil(cursor)), "to": int(math.ceil(stop)) - 1})
                    if not isinstance(payload, dict) or payload.get("s") not in {"ok", "no_data"}:
                        raise ValueError("Nobitex candle request failed")
                    if payload["s"] == "no_data":
                        batch = []
                    else:
                        arrays = [payload.get(key) for key in ("t", "o", "h", "l", "c", "v")]
                        if not all(isinstance(values, list) for values in arrays) or len({len(values) for values in arrays}) != 1:
                            raise ValueError("Unexpected Nobitex candle response")
                        batch = list(zip(*arrays))
                    normalized = []
                    for row in batch:
                        opened = int(row[0])
                        values = [float(value) for value in row[1:]]
                        if not all(math.isfinite(value) for value in values):
                            raise ValueError("Non-finite candle")
                        digest = hashlib.sha256(json.dumps(row, separators=(",", ":")).encode()).hexdigest()
                        normalized.append({"asset": asset, "exchange": "nobitex", "instrument": instrument,
                                           "quote": "USDT", "interval": interval, "open_time": _iso(opened),
                                           "available_at": _iso(candle_close_time(interval, opened, exchange="nobitex")), "retrieved_at": _iso(now),
                                           "open": values[0], "high": values[1], "low": values[2],
                                           "close": values[3], "volume_base": values[4],
                                           "data_hash": digest, "schema_version": 1})
                    self.cache.save("nobitex", instrument, interval, _cache_rows(normalized), as_of=now)
                    downloaded.extend(normalized)
                    if batch:
                        self._alignments[(instrument, interval)] = max(int(row[0]) for row in batch)
                    else:
                        # Without a timestamp anchor, the last interval may
                        # contain an open Tehran-aligned candle. Never cover it
                        # with a negative cache entry that outlives its close.
                        empty_stop = min(stop, now - step) if anchor is None else min(stop, closed_end)
                        if empty_stop > cursor:
                            self.cache.remember_empty("nobitex", instrument, interval,
                                                      cursor, empty_stop, now=now)
                    cursor = stop
            return _cached_audit(self.cache, "nobitex", asset, interval, start, end, now, downloaded)
