"""Public spot candles for portfolio research. No exchange credentials are used."""

from datetime import datetime, timezone
import hashlib
import json
import math
import time

import requests


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
    now = retrieved_at or time.time()
    result = []
    for row in rows:
        if source == "binance":
            start = int(row[0]) / 1000
            opening, high, low, closing, volume = row[1:6]
            instrument = asset + "USDT"
        elif source == "kucoin":
            start = int(row[0])
            opening, closing, high, low, volume = row[1:6]
            instrument = asset + "-USDT"
        else:
            start = int(row[0]) / 1000
            opening, closing, high, low, volume = row[1:6]
            instrument = "tXAUT:UST"
        values = tuple(float(v) for v in (opening, high, low, closing, volume))
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Non-finite candle")
        digest = hashlib.sha256(json.dumps(row, separators=(",", ":")).encode()).hexdigest()
        result.append({"asset": asset, "exchange": source, "instrument": instrument,
                       "quote": "USDT", "interval": interval, "open_time": _iso(start),
                       "available_at": _iso(start + SECONDS[interval]),
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
    valid = []
    for bar in ordered:
        opened = datetime.fromisoformat(bar["open_time"]).timestamp()
        available = datetime.fromisoformat(bar["available_at"]).timestamp()
        if previous is not None:
            delta = opened - previous
            if delta == 0:
                issues.append({"time": bar["open_time"], "kind": "duplicate"})
                continue
            if delta > step:
                issues.append({"time": bar["open_time"], "kind": "gap", "missing_intervals": int(delta // step) - 1})
        previous = opened
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


class PublicSpotData:
    """Bounded, time-paginated public history. Missing periods remain missing."""

    def __init__(self, session=None):
        self.session = session or requests.Session()

    def fetch(self, asset, interval, start, end, *, live=False):
        if asset not in APPROVED_ASSETS or interval not in SECONDS:
            raise ValueError("Unsupported asset or interval")
        if not 0 < start < end or end - start > 5 * 366 * 86400:
            raise ValueError("Invalid or excessive historical range")
        source = source_for(asset, live=live)
        if source == "bitfinex" and interval not in BITFINEX_INTERVAL:
            raise ValueError("Bitfinex does not support this interval")
        page = 1000 if source == "binance" else 1500 if source == "kucoin" else 10000
        cursor = int(start)
        raw = []
        while cursor < end:
            stop = min(end, cursor + page * SECONDS[interval])
            if source == "binance":
                url = "https://data-api.binance.vision/api/v3/klines"
                params = {"symbol": asset + "USDT", "interval": interval,
                          "startTime": cursor * 1000, "endTime": stop * 1000 - 1, "limit": 1000}
            elif source == "kucoin":
                # Classic public spot endpoint: [time,open,close,high,low,base,quote].
                url = "https://api.kucoin.com/api/v1/market/candles"
                params = {"symbol": asset + "-USDT", "type": KUCOIN_INTERVAL[interval],
                          "startAt": cursor, "endAt": stop - 1}
            else:
                url = f"https://api-pub.bitfinex.com/v2/candles/trade:{BITFINEX_INTERVAL[interval]}:tXAUT:UST/hist"
                params = {"start": cursor * 1000, "end": stop * 1000 - 1,
                          "limit": 10000, "sort": 1}
            response = self.session.get(url, params=params, timeout=20)
            response.raise_for_status()
            payload = response.json()
            if source == "kucoin":
                if payload.get("code") != "200000":
                    raise ValueError("KuCoin candle request failed")
                batch = payload["data"]
            else:
                batch = payload
            if not isinstance(batch, list):
                raise ValueError("Unexpected candle response")
            raw.extend(batch)
            cursor = stop
        normalized = normalize_candles(source, asset, interval, raw)
        return audit_candles(normalized, interval, as_of=min(end, time.time()))


class NobitexPublicSpot:
    """Public execution-venue observations; deliberately has no order method."""

    def __init__(self, session=None):
        self.session = session or requests.Session()

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
        resolution = {"15m": "15", "1h": "60", "4h": "240", "1d": "D"}[interval]
        cursor = int(end)
        rows = []
        pages = 0
        while cursor > start:
            pages += 1
            if pages > 1000:
                raise ValueError("Nobitex history pagination limit reached")
            payload = self._get("/market/udf/history", symbol=(asset + "USDT").lower(),
                                resolution=resolution, countback=500, to=cursor)
            if payload.get("s") != "ok" or not payload.get("t"):
                break
            batch = list(zip(payload["t"], payload["o"], payload["h"], payload["l"],
                             payload["c"], payload["v"]))
            rows.extend(batch)
            new_cursor = int(payload["t"][0]) - 1
            if new_cursor >= cursor:
                raise ValueError("Nobitex history pagination stalled")
            cursor = new_cursor
        normalized = []
        now = time.time()
        for row in rows:
            opened = int(row[0])
            if opened < start or opened >= end:
                continue
            values = [float(value) for value in row[1:]]
            digest = hashlib.sha256(json.dumps(row, separators=(",", ":")).encode()).hexdigest()
            normalized.append({"asset": asset, "exchange": "nobitex", "instrument": asset + "USDT",
                               "quote": "USDT", "interval": interval, "open_time": _iso(opened),
                               "available_at": _iso(opened + SECONDS[interval]),
                               "retrieved_at": _iso(now), "open": values[0], "high": values[1],
                               "low": values[2], "close": values[3], "volume_base": values[4],
                               "data_hash": digest, "schema_version": 1})
        return audit_candles(normalized, interval, as_of=min(end, now))
