"""Public providers share durable closed candles and repair only missing spans."""

from datetime import datetime, timezone

import pytest
import requests

from spot_data import NobitexPublicSpot, PublicSpotData, normalize_candles


STEP = 3600
START = 1_749_999_600


def test_binance_normalization_uses_the_reported_close_boundary():
    row = [START * 1000, "10", "12", "9", "11", "4", (START + 1800) * 1000 - 1]
    bar = normalize_candles("binance", "BTC", "1h", [row], START + STEP)[0]
    assert datetime.fromisoformat(bar["available_at"]).timestamp() == START + 1800


def test_public_adapters_accept_an_injected_candle_cache():
    marker = object()
    assert PublicSpotData(cache=marker).cache is marker
    assert NobitexPublicSpot(cache=marker).cache is marker


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class ProviderSession:
    """Fake only HTTP; exercise the real provider adapters and SQLite store."""

    def __init__(self, source, openings=(), *, extra=(), malformed=False, error=False):
        self.source = source
        self.openings = list(openings)
        self.extra = list(extra)
        self.malformed = malformed
        self.error = error
        self.calls = []
        self.close_offset = STEP
        self.invalid_at = None

    def get(self, url, *, params, timeout):
        self.calls.append(dict(params))
        if self.error:
            raise requests.ConnectionError("temporary network failure")
        if self.source == "binance":
            lower, upper = params["startTime"] / 1000, (params["endTime"] + 1) / 1000
        elif self.source == "kucoin":
            lower, upper = params["startAt"], params["endAt"] + 1
        elif self.source == "bitfinex":
            lower, upper = params["start"] / 1000, (params["end"] + 1) / 1000
        else:
            assert "countback" not in params, "bounded requests must honor the missing span"
            lower, upper = params["from"], params["to"] + 1
        selected = [opened for opened in self.openings if lower <= opened < upper] + self.extra
        if self.malformed:
            return Response({"s": "ok", "t": selected, "o": []} if self.source == "nobitex" else {"bad": True})
        if self.source == "binance":
            payload = [[opened * 1000, "10", "12", "9", "11", "4",
                        (opened + self.close_offset) * 1000 - 1, "44", 1, "2", "22", "0"]
                       for opened in selected]
        elif self.source == "kucoin":
            payload = {"code": "200000", "data": [[str(opened), "10", "11", "12", "9", "4", "44"]
                                                    for opened in reversed(selected)]}
        elif self.source == "bitfinex":
            payload = [[opened * 1000, 10, 11, 12, 9, 4] for opened in selected]
        else:
            payload = {"s": "ok" if selected else "no_data", "t": selected,
                       "o": [10] * len(selected), "h": [12] * len(selected),
                       "l": [9] * len(selected), "c": [11] * len(selected), "v": [4] * len(selected)}
        if self.invalid_at in selected:
            index = selected.index(self.invalid_at)
            if self.source == "nobitex":
                payload["v"][index] = -1
            elif self.source == "kucoin":
                payload["data"][len(selected) - index - 1][5] = "-1"
            else:
                payload[index][5] = "-1"
        return Response(payload)


def adapter(source, session, cache):
    return NobitexPublicSpot(session, cache=cache) if source == "nobitex" else PublicSpotData(session, cache=cache)


def fetch(client, source, start, end):
    asset = {"kucoin": "HYPE", "bitfinex": "XAUT"}.get(source, "BTC")
    return client.history(asset, "1h", start, end) if source == "nobitex" else client.fetch(asset, "1h", start, end)


def openings(report):
    return [int(datetime.fromisoformat(bar["open_time"]).timestamp()) for bar in report["candles"]]


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    monkeypatch.setattr("spot_data.time.time", lambda: START + 100 * STEP)
    return tmp_path / "candles.sqlite3"


@pytest.mark.parametrize("source", ["binance", "kucoin", "bitfinex", "nobitex"])
def test_reopened_cache_reuses_history_and_only_downloads_extensions(source, cache_path):
    from candle_store import CandleStore

    rows = [START + i * STEP for i in range(5)]
    first_session = ProviderSession(source, rows)
    first = fetch(adapter(source, first_session, CandleStore(cache_path)), source,
                  START + STEP, START + 4 * STEP)
    assert openings(first) == rows[1:4]
    reopened = CandleStore(cache_path)
    offline = ProviderSession(source, error=True)
    repeated = fetch(adapter(source, offline, reopened), source, START + STEP, START + 4 * STEP)
    assert repeated == first
    assert not offline.calls

    extension_session = ProviderSession(source, rows)
    wider = fetch(adapter(source, extension_session, reopened), source, START, START + 5 * STEP)
    assert openings(wider) == rows
    if source == "binance":
        bounds = [(call["startTime"] // 1000, (call["endTime"] + 1) // 1000) for call in extension_session.calls]
    elif source == "kucoin":
        bounds = [(call["startAt"], call["endAt"] + 1) for call in extension_session.calls]
    elif source == "bitfinex":
        bounds = [(call["start"] // 1000, (call["end"] + 1) // 1000) for call in extension_session.calls]
    else:
        bounds = [(call["from"], call["to"] + 1) for call in extension_session.calls]
    assert bounds == [(START, START + STEP), (START + 4 * STEP, START + 5 * STEP)]


@pytest.mark.parametrize("source", ["binance", "kucoin", "bitfinex", "nobitex"])
def test_extra_closed_rows_are_retained_for_later_requests(source, cache_path):
    from candle_store import CandleStore

    session = ProviderSession(source, [START + STEP], extra=[START, START + 2 * STEP])
    client = adapter(source, session, CandleStore(cache_path))
    first = fetch(client, source, START + STEP, START + 2 * STEP)
    assert openings(first) == [START + STEP]
    offline = ProviderSession(source, error=True)
    wider = fetch(adapter(source, offline, CandleStore(cache_path)), source, START, START + 3 * STEP)
    assert openings(wider) == [START, START + STEP, START + 2 * STEP]
    assert not offline.calls


@pytest.mark.parametrize("source", ["binance", "kucoin", "bitfinex", "nobitex"])
def test_an_internal_gap_is_repaired_on_the_next_call(source, cache_path):
    from candle_store import CandleStore

    cache = CandleStore(cache_path)
    incomplete = ProviderSession(source, [START, START + 2 * STEP])
    first = fetch(adapter(source, incomplete, cache), source, START, START + 3 * STEP)
    assert [issue["kind"] for issue in first["issues"]] == ["gap"]
    repaired_session = ProviderSession(source, [START + STEP])
    repaired = fetch(adapter(source, repaired_session, cache), source, START, START + 3 * STEP)
    assert openings(repaired) == [START, START + STEP, START + 2 * STEP]
    assert not repaired["issues"]
    assert len(repaired_session.calls) == 1


@pytest.mark.parametrize("source", ["binance", "kucoin", "bitfinex", "nobitex"])
def test_open_candle_is_fetched_when_it_closes(source, cache_path, monkeypatch):
    from candle_store import CandleStore

    # Nobitex's half-hour alignment must come from timestamps, not UTC floors.
    start = START + (1800 if source == "nobitex" else 0)
    clock = [start + STEP + 1200]
    monkeypatch.setattr("spot_data.time.time", lambda: clock[0])
    cache = CandleStore(cache_path)
    session = ProviderSession(source, [start, start + STEP])
    client = adapter(source, session, cache)
    assert openings(fetch(client, source, start, start + 2 * STEP)) == [start]
    before = len(session.calls)
    assert openings(fetch(client, source, start, start + 2 * STEP)) == [start]
    assert len(session.calls) == before
    clock[0] = start + 2 * STEP
    assert openings(fetch(client, source, start, start + 2 * STEP)) == [start, start + STEP]
    assert len(session.calls) == before + 1


@pytest.mark.parametrize("source", ["binance", "kucoin", "bitfinex", "nobitex"])
@pytest.mark.parametrize("failure", ["error", "malformed"])
def test_failures_do_not_hide_a_later_successful_response(source, failure, cache_path):
    from candle_store import CandleStore

    cache = CandleStore(cache_path)
    failed = ProviderSession(source, [START], **{failure: True})
    with pytest.raises((ValueError, KeyError, requests.ConnectionError)):
        fetch(adapter(source, failed, cache), source, START, START + STEP)
    healthy = ProviderSession(source, [START])
    assert openings(fetch(adapter(source, healthy, cache), source, START, START + STEP)) == [START]
    assert len(healthy.calls) == 1


@pytest.mark.parametrize("source", ["binance", "nobitex"])
def test_rows_saved_by_the_other_fetcher_retain_normalized_audit_fields(source, cache_path):
    from candle_store import CandleStore

    cache = CandleStore(cache_path)
    cache.save(source, "BTCUSDT", "60", [{"open_time": START, "close_time": START + STEP,
               "open": 10, "high": 12, "low": 9, "close": 11, "volume": 4, "metadata": {}}],
               as_of=START + 2 * STEP)
    session = ProviderSession(source, error=True)
    report = fetch(adapter(source, session, cache), source, START, START + STEP)
    assert openings(report) == [START]
    bar = report["candles"][0]
    assert (bar["asset"], bar["exchange"], bar["instrument"], bar["quote"], bar["interval"], bar["schema_version"]) == (
        "BTC", source, "BTCUSDT", "USDT", "1h", 1)
    assert bar["volume_base"] == 4
    assert len(bar["data_hash"]) == 64
    assert datetime.fromisoformat(bar["retrieved_at"])
    assert not session.calls


@pytest.mark.parametrize("source", ["binance", "kucoin", "bitfinex", "nobitex"])
def test_invalid_rows_remain_visible_and_are_not_saved(source, cache_path):
    from candle_store import CandleStore

    cache = CandleStore(cache_path)
    session = ProviderSession(source, [START, START + STEP])
    session.invalid_at = START + STEP
    report = fetch(adapter(source, session, cache), source, START, START + 2 * STEP)
    assert openings(report) == [START]
    assert [issue["kind"] for issue in report["issues"]] == ["invalid_ohlcv"]
    healthy = ProviderSession(source, [START + STEP])
    recovered = fetch(adapter(source, healthy, cache), source, START, START + 2 * STEP)
    assert openings(recovered) == [START, START + STEP]
    assert len(healthy.calls) == 1


@pytest.mark.parametrize("source", ["binance", "kucoin", "bitfinex", "nobitex"])
def test_successful_empty_response_is_temporarily_reused(source, cache_path, monkeypatch):
    from candle_store import CandleStore

    clock = [START + 100 * STEP]
    monkeypatch.setattr("spot_data.time.time", lambda: clock[0])
    cache = CandleStore(cache_path)
    empty = ProviderSession(source)
    assert not fetch(adapter(source, empty, cache), source, START, START + STEP)["candles"]
    offline = ProviderSession(source, error=True)
    assert not fetch(adapter(source, offline, CandleStore(cache_path)), source, START, START + STEP)["candles"]
    assert not offline.calls
    clock[0] += 61
    healthy = ProviderSession(source, [START])
    assert openings(fetch(adapter(source, healthy, cache), source, START, START + STEP)) == [START]


def test_nobitex_paginated_windows_do_not_skip_candles(cache_path, monkeypatch):
    from candle_store import CandleStore

    rows = [START + index * STEP for index in range(1100)]
    session = ProviderSession("nobitex", rows)
    # This historical request is fully closed and wider than the API page.
    monkeypatch.setattr("spot_data.time.time", lambda: START + 1200 * STEP)
    report = fetch(adapter("nobitex", session, CandleStore(cache_path)), "nobitex", START, START + 1100 * STEP)
    assert openings(report) == rows
    assert [(call["from"], call["to"] + 1) for call in session.calls] == [
        (START, START + 500 * STEP), (START + 500 * STEP, START + 1000 * STEP),
        (START + 1000 * STEP, START + 1100 * STEP)]


@pytest.mark.parametrize("source", ["binance", "kucoin", "bitfinex", "nobitex"])
def test_unaligned_range_reuses_candles_without_requesting_partial_prefix(source, cache_path):
    from candle_store import CandleStore

    cache = CandleStore(cache_path)
    session = ProviderSession(source, [START + STEP, START + 2 * STEP, START + 3 * STEP])
    first = fetch(adapter(source, session, cache), source, START + 1200, START + 3 * STEP + 1200)
    assert openings(first) == [START + STEP, START + 2 * STEP]
    offline = ProviderSession(source, error=True)
    repeated = fetch(adapter(source, offline, CandleStore(cache_path)), source,
                     START + 1200, START + 3 * STEP + 1200)
    assert repeated == first
    assert not offline.calls


@pytest.mark.parametrize("source", ["binance", "kucoin", "bitfinex", "nobitex"])
def test_extra_rows_from_prefix_request_can_already_cover_missing_suffix(source, cache_path):
    from candle_store import CandleStore

    cache = CandleStore(cache_path)
    fetch(adapter(source, ProviderSession(source, [START + STEP]), cache), source, START + STEP, START + 2 * STEP)
    session = ProviderSession(source, [START], extra=[START + 2 * STEP])
    report = fetch(adapter(source, session, cache), source, START, START + 3 * STEP)
    assert openings(report) == [START, START + STEP, START + 2 * STEP]
    assert not report["issues"]
    assert len(session.calls) == 1


def test_a_completed_binance_page_survives_failure_on_a_later_page(cache_path, monkeypatch):
    from candle_store import CandleStore

    monkeypatch.setattr("spot_data.time.time", lambda: START + 1200 * STEP)
    cache = CandleStore(cache_path)
    rows = [START + index * STEP for index in range(1001)]
    session = ProviderSession("binance", rows)
    original_get = session.get

    def fail_second(url, *, params, timeout):
        if session.calls:
            raise requests.ConnectionError("second page failed")
        return original_get(url, params=params, timeout=timeout)

    session.get = fail_second
    with pytest.raises(requests.ConnectionError):
        fetch(adapter("binance", session, cache), "binance", START, START + 1001 * STEP)
    offline = ProviderSession("binance", error=True)
    assert openings(fetch(adapter("binance", offline, CandleStore(cache_path)), "binance", START, START + 1000 * STEP)) == rows[:1000]
    assert not offline.calls


def test_nobitex_daily_closes_follow_historical_tehran_dst_timestamps(cache_path, monkeypatch):
    from candle_store import CandleStore

    day = 86400
    start = int(datetime(2020, 6, 14, 19, 30, tzinfo=timezone.utc).timestamp())
    clock = [start + day + 600]
    monkeypatch.setattr("spot_data.time.time", lambda: clock[0])
    session = ProviderSession("nobitex", [start, start + day])
    cache = CandleStore(cache_path)
    first = NobitexPublicSpot(session, cache=cache).history("BTC", "1d", start, start + 2 * day)
    assert openings(first) == [start]
    offline = ProviderSession("nobitex", error=True)
    repeated = NobitexPublicSpot(offline, cache=CandleStore(cache_path)).history("BTC", "1d", start, start + 2 * day)
    assert openings(repeated) == [start]
    assert not offline.calls
    clock[0] = start + 2 * day
    fresh = NobitexPublicSpot(session, cache=cache).history("BTC", "1d", start, start + 2 * day)
    assert openings(fresh) == [start, start + day]


@pytest.mark.parametrize("source", ["binance", "kucoin", "bitfinex", "nobitex"])
def test_negative_cache_prefix_does_not_mask_newly_closed_tail(source, cache_path, monkeypatch):
    from candle_store import CandleStore

    clock = [START + 2 * STEP - 10]
    monkeypatch.setattr("spot_data.time.time", lambda: clock[0])
    cache = CandleStore(cache_path)
    asset = {"kucoin": "HYPE", "bitfinex": "XAUT"}.get(source, "BTC")
    instrument = {"kucoin": "HYPE-USDT", "bitfinex": "tXAUT:UST"}.get(source, asset + "USDT")
    cache.remember_empty(source, instrument, "1h", START, START + STEP, now=clock[0])
    clock[0] += 10
    session = ProviderSession(source, [START + STEP])
    report = fetch(adapter(source, session, cache), source, START, START + 2 * STEP)
    assert openings(report) == [START + STEP]
    assert len(session.calls) == 1
    for key, value in session.calls[0].items():
        if key in {"startTime", "endTime", "startAt", "endAt", "start", "end", "from", "to"}:
            assert isinstance(value, int)
