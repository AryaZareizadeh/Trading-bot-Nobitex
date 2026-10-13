"""Independent regression proofs for cache correctness edge cases."""
from datetime import datetime, timezone

import pandas as pd
import pytest

from candle_store import CandleStore
from config import Config
from data import DataFetcher


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def config(exchange, *, limit=2, interval="1"):
    result = Config(symbol="btcusdt", interval=interval, limit=limit)
    result.exchange = exchange
    return result


def bar(opened, closed=None):
    return dict(open_time=opened, close_time=opened + 60 if closed is None else closed,
                open=100, high=110, low=90, close=105, volume=12)


def payload(exchange, times):
    rows = [[t, 100, 110, 90, 105, 12] for t in times]
    if exchange == "binance":
        return [[r[0] * 1000, *r[1:]] for r in rows]
    if not rows:
        return {"s": "no_data"}
    return {"s": "ok", **{key: [r[i] for r in rows]
                           for i, key in enumerate(("t", "o", "h", "l", "c", "v"))}}


@pytest.mark.parametrize("exchange", ["binance", "nobitex"])
def test_sparse_success_never_negatively_caches_unreturned_candles(monkeypatch, exchange):
    cache = CandleStore()
    cache.save(exchange, "BTCUSDT", "1m", [bar(60), bar(300)], as_of=370)
    calls = []

    def get(session, url, *, params, timeout):
        calls.append(params)
        start = params.get("startTime", params.get("from", 0) * 1000) / 1000
        end = params.get("endTime", params.get("to", 0) * 1000) / 1000
        times = [t for t in (120, 180, 240) if start <= t <= end]
        if len(calls) == 1:
            times = times[:1]  # The provider's successful page is partial.
        return Response(payload(exchange, times))

    monkeypatch.setattr("requests.Session.get", get)
    monkeypatch.setattr("data.time.time", lambda: 370)
    DataFetcher(config(exchange), cache=cache).fetch()
    DataFetcher(config(exchange), cache=cache).fetch()
    assert [r["open_time"] for r in cache.read(exchange, "BTCUSDT", "1m")] == [60, 120, 180, 240, 300]


def test_nobitex_monthly_refresh_includes_next_open_as_closure_proof(monkeypatch):
    def epoch(month):
        return int(datetime(2026, month, 1, tzinfo=timezone.utc).timestamp())

    january, february, march = map(epoch, (1, 2, 3))
    cache = CandleStore()
    cache.save("nobitex", "BTCUSDT", "1M", [bar(january, february)], as_of=march + 1)

    def get(session, url, *, params, timeout):
        lower, upper = params.get("from", 0), params["to"]
        times = [t for t in (february, march) if lower <= t <= upper]
        if "countback" in params:
            times = times[-params["countback"]:]
        return Response(payload("nobitex", times))

    monkeypatch.setattr("requests.Session.get", get)
    monkeypatch.setattr("data.time.time", lambda: march + 1)
    frame = DataFetcher(config("nobitex", limit=1, interval="M"), cache=cache).fetch_latest()
    assert frame.index[-1] == pd.Timestamp(february, unit="s")


def test_gapped_nobitex_monthly_response_does_not_cover_unreturned_month(monkeypatch):
    def epoch(month):
        return int(datetime(2026, month, 1, tzinfo=timezone.utc).timestamp())

    january, february, march = map(epoch, (1, 2, 3))
    cache = CandleStore()

    def get(session, url, *, params, timeout):
        return Response(payload("nobitex", [january, march]))

    monkeypatch.setattr("requests.Session.get", get)
    monkeypatch.setattr("data.time.time", lambda: march + 1)
    DataFetcher(config("nobitex", limit=1, interval="M"), cache=cache).fetch()
    assert cache.missing_ranges("nobitex", "BTCUSDT", "1M", february, march, now=march + 1), (
        "A January candle must not establish coverage for missing February")


def test_nobitex_monthly_backfill_continues_after_full_raw_page(monkeypatch):
    times = [int(datetime(2026, month, 1, tzinfo=timezone.utc).timestamp()) for month in range(1, 6)]
    cache = CandleStore()

    def get(session, url, *, params, timeout):
        selected = [t for t in times if params.get("from", 0) <= t <= params["to"]]
        if "countback" in params:
            selected = selected[-params["countback"]:]
        return Response(payload("nobitex", selected))

    monkeypatch.setattr("requests.Session.get", get)
    monkeypatch.setattr("data.time.time", lambda: times[-1] + 1)
    monkeypatch.setattr(DataFetcher, "_NOBITEX_PAGE", 2)
    frame = DataFetcher(config("nobitex", limit=3, interval="M"), cache=cache).fetch()
    assert frame.index.tolist() == [pd.Timestamp(t, unit="s") for t in times[1:4]]


def test_latest_follows_partial_tail_pages_until_the_newest_closed_candle(monkeypatch):
    cache = CandleStore()
    cache.save("binance", "BTCUSDT", "1m", [bar(60)], as_of=370)
    calls = []

    def get(session, url, *, params, timeout):
        calls.append(params)
        lower = params.get("startTime", 0) / 1000
        upper = params["endTime"] / 1000
        selected = [t for t in (120, 180, 240, 300) if lower <= t <= upper]
        if len(calls) == 1:
            selected = selected[:1]
        return Response(payload("binance", selected))

    monkeypatch.setattr("requests.Session.get", get)
    monkeypatch.setattr("data.time.time", lambda: 370)
    frame = DataFetcher(config("binance", limit=1), cache=cache).fetch_latest()
    assert frame.index[-1] == pd.Timestamp(300, unit="s")


@pytest.mark.parametrize("exchange", ["binance", "nobitex"])
def test_unexpired_empty_prefix_does_not_make_new_tail_http_bounds_floats(monkeypatch, exchange):
    cache = CandleStore()
    cache.save(exchange, "BTCUSDT", "1m", [bar(60)], as_of=370)
    cache.remember_empty(exchange, "BTCUSDT", "1m", 120, 240, now=370)

    def get(session, url, *, params, timeout):
        for key in ("startTime", "endTime", "from", "to"):
            if key in params:
                assert isinstance(params[key], int), (key, params[key])
        return Response(payload(exchange, [240, 300]))

    monkeypatch.setattr("requests.Session.get", get)
    monkeypatch.setattr("data.time.time", lambda: 370)
    frame = DataFetcher(config(exchange), cache=cache).fetch()
    assert frame.index[-1] == pd.Timestamp(300, unit="s")


def test_nobitex_daily_autumn_candle_is_not_saved_one_hour_before_it_closes(monkeypatch):
    from spot_data import NobitexPublicSpot

    opened, closed = 1632166200, 1632256200  # Tehran midnights across the 25-hour day.
    cache = CandleStore()

    class Session:
        def get(self, url, *, params, timeout):
            return Response(payload("nobitex", [opened]))

    monkeypatch.setattr("spot_data.time.time", lambda: opened + 86400)
    report = NobitexPublicSpot(Session(), cache=cache).history("BTC", "1d", opened, closed + 86400)
    assert not report["candles"], "The autumn transition candle is still open for one hour"
    assert cache.read("nobitex", "BTCUSDT", "1d") == []


def test_nobitex_daily_reopened_frontier_fetches_just_closed_spring_candle(monkeypatch):
    from spot_data import NobitexPublicSpot

    previous, opened, closed = 1616272200, 1616358600, 1616441400
    now = closed + 600
    cache = CandleStore()
    cache.save("nobitex", "BTCUSDT", "1d", [bar(previous, opened)], as_of=now)
    calls = []

    class Session:
        def get(self, url, *, params, timeout):
            calls.append(params)
            selected = [t for t in (opened, closed) if params["from"] <= t <= params["to"]]
            return Response(payload("nobitex", selected))

    monkeypatch.setattr("spot_data.time.time", lambda: now)
    report = NobitexPublicSpot(Session(), cache=cache).history("BTC", "1d", previous, now)
    assert calls, "The spring transition candle closed after 23 hours, before the UTC-day floor"
    assert cache.read("nobitex", "BTCUSDT", "1d")[-1]["open_time"] == opened
    assert len(report["candles"]) == 2


def test_nobitex_daily_spring_prefix_is_not_mistaken_for_an_unaligned_empty_prefix(monkeypatch):
    from spot_data import NobitexPublicSpot

    opened, next_open, end = 1616358600, 1616441400, 1616527800
    cache = CandleStore()
    cache.save("nobitex", "BTCUSDT", "1d", [bar(next_open, end)], as_of=end + 3600)
    calls = []

    class Session:
        def get(self, url, *, params, timeout):
            calls.append(params)
            return Response(payload("nobitex", [opened, next_open]))

    monkeypatch.setattr("spot_data.time.time", lambda: end + 3600)
    NobitexPublicSpot(Session(), cache=cache).history("BTC", "1d", opened, end)
    assert calls, "A 23-hour daily candle is a missing prefix, not an unaligned request"
    assert [r["open_time"] for r in cache.read("nobitex", "BTCUSDT", "1d")] == [opened, next_open]


def test_nobitex_daily_autumn_adjacent_candles_have_no_false_gap_audit(monkeypatch):
    from spot_data import NobitexPublicSpot

    opened, next_open = 1632166200, 1632256200
    end = next_open + 86400
    cache = CandleStore()
    cache.save("nobitex", "BTCUSDT", "1d", [bar(opened, next_open), bar(next_open, end)], as_of=end)

    class Session:
        def get(self, url, *, params, timeout):
            raise AssertionError("Covered adjacent historical candles must stay offline")

    monkeypatch.setattr("spot_data.time.time", lambda: end)
    report = NobitexPublicSpot(Session(), cache=cache).history("BTC", "1d", opened, end)
    assert not report["issues"], "Calendar-adjacent 25-hour daily candles are continuous"


def test_nonempty_monthly_page_without_closed_rows_stays_retryable(monkeypatch):
    february = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp())
    march = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp())
    cache = CandleStore()
    calls = []

    def get(session, url, *, params, timeout):
        calls.append(params)
        return Response(payload("nobitex", [march] if len(calls) == 1 else [february, march]))

    monkeypatch.setattr("requests.Session.get", get)
    monkeypatch.setattr("data.time.time", lambda: march + 1)
    assert DataFetcher(config("nobitex", limit=1, interval="M"), cache=cache).fetch_latest().empty
    assert cache.missing_ranges("nobitex", "BTCUSDT", "1M", february, march, now=march + 1)
    frame = DataFetcher(config("nobitex", limit=1, interval="M"), cache=cache).fetch_latest()
    assert frame.index[-1] == pd.Timestamp(february, unit="s")


def test_canonical_interval_alias_uses_nobitex_wire_resolution(monkeypatch):
    cache = CandleStore()

    def get(session, url, *, params, timeout):
        assert params["resolution"] == "60", "Accepted canonical aliases must become provider resolution codes"
        return Response(payload("nobitex", [3600, 7200]))

    monkeypatch.setattr("requests.Session.get", get)
    monkeypatch.setattr("data.time.time", lambda: 7201)
    frame = DataFetcher(config("nobitex", limit=1, interval="1h"), cache=cache).fetch()
    assert frame.index[-1] == pd.Timestamp(3600, unit="s")
