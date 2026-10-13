"""Exchange requests use durable full OHLCV before contacting the provider."""
import calendar
from datetime import datetime, timezone

import pandas as pd
import pytest

from config import Config
from data import DataFetcher


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def configure(exchange, limit=3, interval="1", symbol="btcusdt"):
    config = Config(symbol=symbol, interval=interval, limit=limit)
    config.exchange = exchange
    return config


def provider(monkeypatch, exchange, rows, calls):
    def get(session, url, *, params, timeout):
        calls.append(dict(params))
        if exchange == "binance":
            start = params.get("startTime", -1) / 1000
            end = params.get("endTime", float("inf")) / 1000
            selected = [r for r in rows if start <= r[0] <= end]
            if "startTime" not in params:
                selected = selected[-params["limit"]:]
            return Response([[r[0] * 1000, *r[1:]] for r in selected])
        start, end = params.get("from", -1), params["to"]
        selected = [r for r in rows if start <= r[0] <= end]
        if "countback" in params:
            selected = selected[-params["countback"]:]
        if not selected:
            return Response({"s": "no_data"})
        return Response({"s": "ok", **{name: [r[i] for r in selected]
            for i, name in enumerate(("t", "o", "h", "l", "c", "v"))}})
    monkeypatch.setattr("requests.Session.get", get)


@pytest.mark.parametrize("exchange", ["binance", "nobitex"])
def test_reopen_reuses_full_ohlcv_without_http(monkeypatch, exchange):
    calls = []
    rows = [(t, 100, 110, 90, 105, t / 10) for t in (60, 120, 180, 240)]
    provider(monkeypatch, exchange, rows, calls)
    monkeypatch.setattr("data.time.time", lambda: 250)
    first = DataFetcher(configure(exchange)).fetch()
    assert len(calls) == 1
    second = DataFetcher(configure(exchange)).fetch()
    pd.testing.assert_frame_equal(first, second)
    assert second["Volume"].tolist() == [6.0, 12.0, 18.0]
    assert len(calls) == 1, "A fresh fetcher must consult the persistent candle database"


@pytest.mark.parametrize("exchange", ["binance", "nobitex"])
def test_append_newly_closed_only_and_latest_reuses_history(monkeypatch, exchange):
    calls, clock = [], [250]
    rows = [(t, 100, 110, 90, 105, t / 10) for t in (60, 120, 180, 240, 300)]
    provider(monkeypatch, exchange, rows, calls)
    monkeypatch.setattr("data.time.time", lambda: clock[0])
    DataFetcher(configure(exchange)).fetch()
    latest = DataFetcher(configure(exchange)).fetch_latest()
    assert latest.index[-1] == pd.Timestamp(180, unit="s")
    assert len(calls) == 1
    clock[0] = 310
    updated = DataFetcher(configure(exchange)).fetch()
    assert updated.index[-1] == pd.Timestamp(240, unit="s")
    assert len(calls) == 2
    assert calls[-1].get("startTime", calls[-1].get("from", -1) * 1000) == 240000
    if exchange == "nobitex":
        assert "countback" not in calls[-1]
    assert len(DataFetcher(configure(exchange)).fetch_latest()) == 1
    assert len(calls) == 2


@pytest.mark.parametrize("exchange", ["binance", "nobitex"])
def test_more_history_fetches_before_saved_first_candle(monkeypatch, exchange):
    calls = []
    rows = [(t, 100, 110, 90, 105, 12) for t in (60, 120, 180, 240, 300, 360)]
    provider(monkeypatch, exchange, rows, calls)
    monkeypatch.setattr("data.time.time", lambda: 370)
    assert len(DataFetcher(configure(exchange, limit=2)).fetch()) == 2
    expanded = DataFetcher(configure(exchange, limit=5)).fetch()
    assert len(expanded) == 5
    assert len(calls) == 2
    assert calls[-1].get("endTime", calls[-1].get("to", 0) * 1000) < 240000


@pytest.mark.parametrize("exchange", ["binance", "nobitex"])
def test_interior_gap_repaired_without_redownloading_neighbors(monkeypatch, exchange):
    from candle_store import CandleStore
    cache = CandleStore()
    cache.save(exchange, "BTCUSDT", "1m", [
        {"open_time": t, "close_time": t + 60, "open": 100, "high": 110,
         "low": 90, "close": 105, "volume": 12} for t in (60, 180)
    ], as_of=250)
    calls = []
    provider(monkeypatch, exchange, [(120, 100, 110, 90, 105, 99)], calls)
    monkeypatch.setattr("data.time.time", lambda: 250)
    frame = DataFetcher(configure(exchange, limit=2)).fetch()
    assert len(calls) == 1
    assert frame["Volume"].tolist() == [99.0, 12.0]
    assert calls[0].get("startTime", calls[0].get("from", -1) * 1000) == 120000


def test_binance_monthly_uses_calendar_and_actual_close_time(monkeypatch):
    def epoch(year, month, day):
        return calendar.timegm(datetime(year, month, day, tzinfo=timezone.utc).timetuple())
    opened, closed = epoch(2026, 1, 1), epoch(2026, 2, 1)
    calls = []
    rows = [(opened, 100, 110, 90, 105, 12, closed * 1000 - 1)]
    provider(monkeypatch, "binance", rows, calls)
    monkeypatch.setattr("data.time.time", lambda: epoch(2026, 1, 31))
    assert DataFetcher(configure("binance", limit=1, interval="M")).fetch().empty
    monkeypatch.setattr("data.time.time", lambda: closed + 1)
    assert len(DataFetcher(configure("binance", limit=1, interval="M")).fetch()) == 1


def test_nobitex_alignment_uses_returned_open_times(monkeypatch):
    calls, clock = [], [14000]
    rows = [(t, 100, 110, 90, 105, 12) for t in (1800, 5400, 9000, 12600)]
    provider(monkeypatch, "nobitex", rows, calls)
    monkeypatch.setattr("data.time.time", lambda: clock[0])
    frame = DataFetcher(configure("nobitex", limit=2, interval="60")).fetch()
    assert frame.index[-1] == pd.Timestamp(9000, unit="s")
    clock[0] = 15000  # UTC hour closed; Nobitex's next shifted candle has not.
    pd.testing.assert_frame_equal(frame, DataFetcher(configure("nobitex", limit=2, interval="60")).fetch())
    assert len(calls) == 1
    clock[0] = 16201
    assert DataFetcher(configure("nobitex", limit=2, interval="60")).fetch().index[-1] == pd.Timestamp(12600, unit="s")
    assert len(calls) == 2


def test_cancel_preserves_successful_earlier_page(monkeypatch):
    from candle_store import CandleStore
    calls = []
    rows = [(t, 100, 110, 90, 105, 12) for t in (60, 120, 180, 240)]
    provider(monkeypatch, "binance", rows, calls)
    monkeypatch.setattr("data.time.time", lambda: 250)
    monkeypatch.setattr(DataFetcher, "_BINANCE_PAGE", 2)
    fetcher = DataFetcher(configure("binance", limit=4), cancel=lambda: len(calls) >= 1)
    with pytest.raises(RuntimeError, match="cancelled"):
        fetcher.fetch()
    saved = CandleStore().read("binance", "BTCUSDT", "1m", as_of=250)
    assert [r["open_time"] for r in saved] == [180]


@pytest.mark.parametrize("exchange", ["binance", "nobitex"])
def test_every_extra_closed_candle_is_saved_before_tail_selection(monkeypatch, exchange):
    calls = []
    rows = [(t, 100, 110, 90, 105, t / 10) for t in (60, 120, 180, 240, 300)]
    def get(session, url, *, params, timeout):
        calls.append(params)
        if exchange == "binance":
            return Response([[r[0] * 1000, *r[1:]] for r in rows])
        return Response({"s": "ok", **{key: [r[i] for r in rows]
            for i, key in enumerate(("t", "o", "h", "l", "c", "v"))}})
    monkeypatch.setattr("requests.Session.get", get)
    monkeypatch.setattr("data.time.time", lambda: 310)
    assert len(DataFetcher(configure(exchange, limit=1)).fetch()) == 1
    assert len(DataFetcher(configure(exchange, limit=4)).fetch()) == 4
    assert len(calls) == 1


def test_concurrent_callers_share_one_download(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import time
    calls = []
    rows = [[t * 1000, 100, 110, 90, 105, 12] for t in (60, 120, 180, 240)]
    def get(session, url, *, params, timeout):
        calls.append(params)
        time.sleep(.03)
        return Response(rows)
    monkeypatch.setattr("requests.Session.get", get)
    monkeypatch.setattr("data.time.time", lambda: 250)
    def fetch(_):
        return DataFetcher(configure("binance")).fetch()
    with ThreadPoolExecutor(max_workers=4) as pool:
        frames = list(pool.map(fetch, range(4)))
    assert len(calls) == 1
    for frame in frames:
        pd.testing.assert_frame_equal(frame, frames[0])


@pytest.mark.parametrize("exchange", ["binance", "nobitex"])
def test_http_errors_remain_retryable_and_do_not_erase_saved_page(monkeypatch, exchange):
    import requests
    from candle_store import CandleStore
    calls = []
    rows = [(t, 100, 110, 90, 105, 12) for t in (60, 120, 180, 240)]
    provider(monkeypatch, exchange, rows, calls)
    real_get = requests.Session.get
    def get(session, *args, **kwargs):
        if len(calls) == 1:
            raise requests.ConnectionError("disconnected")
        return real_get(session, *args, **kwargs)
    monkeypatch.setattr(requests.Session, "get", get)
    monkeypatch.setattr("data.time.time", lambda: 250)
    monkeypatch.setattr(DataFetcher, "_BINANCE_PAGE" if exchange == "binance" else "_NOBITEX_PAGE", 2)
    with pytest.raises(requests.ConnectionError):
        DataFetcher(configure(exchange, limit=3)).fetch()
    assert [r["open_time"] for r in CandleStore().read(exchange, "BTCUSDT", "1m")] == [180]
    monkeypatch.setattr(requests.Session, "get", real_get)
    assert len(DataFetcher(configure(exchange, limit=3)).fetch()) == 3
    assert len(calls) == 2
