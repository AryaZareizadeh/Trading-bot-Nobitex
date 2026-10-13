import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


def candle(opened=0, closed=60, **changes):
    row = dict(open_time=opened, close_time=closed, open=10.0, high=13.0,
               low=9.0, close=12.0, volume=7.25, metadata={"source": "test"})
    row.update(changes)
    return row


@pytest.mark.parametrize("interval, expected", [
    ("1", "1m"), ("5", "5m"), ("15", "15m"), ("30", "30m"),
    ("60", "1h"), ("180", "3h"), ("240", "4h"), ("D", "1d"),
    ("W", "1w"), ("M", "1M"), ("1m", "1m"), ("1M", "1M"),
    ("1h", "1h"), ("1w", "1w"),
])
def test_canonical_interval_preserves_month_and_minute_distinction(interval, expected):
    from candle_store import canonical_interval
    assert canonical_interval(interval) == expected


def test_unknown_interval_is_rejected():
    from candle_store import canonical_interval
    with pytest.raises(ValueError):
        canonical_interval("banana")


def test_calendar_month_closes_at_next_utc_month_boundary():
    from candle_store import candle_close_time
    # February 2024 is a leap month; April has 30 days.
    assert candle_close_time("M", 1706745600) == 1709251200
    assert candle_close_time("1M", 1711929600) == 1714521600
    assert candle_close_time("60", 12345, exchange="nobitex") == 15945


def test_default_database_is_shared_public_data_and_import_has_no_side_effect(tmp_path):
    environment = dict(os.environ, TRADER_DATA_DIR=str(tmp_path / "unopened"))
    subprocess.run([sys.executable, "-c", "import candle_store"], env=environment,
                   check=True, timeout=10)
    assert not (tmp_path / "unopened").exists()
    from candle_store import CandleStore
    store = CandleStore()
    assert store.path == tmp_path / "candles.sqlite3"
    assert store.path.exists()
    assert not (tmp_path / "state.db").exists()


def test_reopen_preserves_full_candles_and_metadata(tmp_path):
    from candle_store import CandleStore
    path = tmp_path / "candles.sqlite3"
    rows = [candle(60, 120, volume=21.75, metadata={"trade_count": 15, "nested": {"quote_volume": 3.125}}),
            candle(0, 60)]
    CandleStore(path).save("binance", "BTCUSDT", "1m", rows, as_of=120)
    assert CandleStore(path).read("binance", "BTCUSDT", "1m") == [rows[1], rows[0]]


def test_canonical_series_keys_share_rows_but_exchanges_and_intervals_do_not(tmp_path):
    from candle_store import CandleStore
    store = CandleStore(tmp_path / "candles.sqlite3")
    store.save("BiNaNcE", "btcusdt", "60", [candle(0, 3600)], as_of=3600)
    assert store.read("binance", "BTCUSDT", "1h") == [candle(0, 3600)]
    assert store.read("nobitex", "BTCUSDT", "1h") == []
    assert store.read("binance", "BTCUSDT", "1m") == []
    assert store.read("binance", "ETHUSDT", "1h") == []


@pytest.mark.parametrize("changes", [
    {"open": 0}, {"close": float("nan")}, {"high": float("inf")},
    {"low": -1}, {"volume": -1}, {"volume": float("inf")},
    {"high": 11}, {"low": 11}, {"close_time": 0},
    {"close_time": 121}, {"open_time": 0.5}, {"close_time": 59.5},
    {"metadata": {"bad": float("nan")}}, {"metadata": object()},
])
def test_invalid_or_open_rows_do_not_poison_valid_rows(tmp_path, changes):
    from candle_store import CandleStore
    store = CandleStore(tmp_path / "candles.sqlite3")
    store.save("binance", "BTCUSDT", "1", [candle(**changes), {}, None, candle(60, 120)], as_of=120)
    assert store.read("binance", "BTCUSDT", "1m") == [candle(60, 120)]


def test_exchange_supplied_closing_boundary_is_authoritative(tmp_path):
    from candle_store import CandleStore
    store = CandleStore(tmp_path / "candles.sqlite3")
    row = candle(0, 100)
    store.save("nobitex", "BTCIRT", "1m", [row], as_of=100)
    assert store.read("nobitex", "BTCIRT", "1m") == [row]


def test_incomplete_candle_can_be_saved_after_it_closes(tmp_path):
    from candle_store import CandleStore
    store = CandleStore(tmp_path / "candles.sqlite3")
    store.save("binance", "BTCUSDT", "1", [candle()], as_of=59)
    assert store.read("binance", "BTCUSDT", "1m") == []
    store.save("binance", "BTCUSDT", "1", [candle()], as_of=60)
    assert store.read("binance", "BTCUSDT", "1m") == [candle()]


def test_duplicate_completed_bars_keep_original_values(tmp_path):
    from candle_store import CandleStore
    path = tmp_path / "candles.sqlite3"
    CandleStore(path).save("binance", "BTCUSDT", "1", [candle()], as_of=60)
    CandleStore(path).save("binance", "BTCUSDT", "1m", [candle(volume=99, close=11, metadata={"revised": True})], as_of=60)
    assert CandleStore(path).read("binance", "BTCUSDT", "1") == [candle()]


def test_read_filters_open_range_completion_and_newest_limit(tmp_path):
    from candle_store import CandleStore
    store = CandleStore(tmp_path / "candles.sqlite3")
    rows = [candle(opened, opened + 60) for opened in (0, 60, 120, 180)]
    store.save("binance", "BTCUSDT", "1", rows, as_of=240)
    assert store.read("binance", "BTCUSDT", "1m", start=60, end=180, as_of=179) == [rows[1]]
    assert store.read("binance", "BTCUSDT", "1m", limit=2) == rows[2:]
    assert store.read("binance", "BTCUSDT", "1m", limit=0) == []


def test_missing_ranges_subtract_actual_overlapping_bars(tmp_path):
    from candle_store import CandleStore
    store = CandleStore(tmp_path / "candles.sqlite3")
    store.save("binance", "BTCUSDT", "1m", [candle(0, 60), candle(120, 180), candle(180, 240)], as_of=240)
    assert store.missing_ranges("binance", "BTCUSDT", "1", 30, 270, now=300) == [(60, 120), (240, 270)]
    assert store.missing_ranges("binance", "BTCUSDT", "1", -30, 30, now=300) == [(-30, 0)]
    assert store.missing_ranges("binance", "BTCUSDT", "1", 50, 50, now=300) == []
    assert store.missing_ranges("nobitex", "BTCUSDT", "1", 30, 270, now=300) == [(30, 270)]


def test_future_relative_to_request_is_not_used_as_coverage(tmp_path):
    from candle_store import CandleStore
    store = CandleStore(tmp_path / "candles.sqlite3")
    store.save("binance", "BTCUSDT", "1m", [candle(0, 60)], as_of=60)
    assert store.missing_ranges("binance", "BTCUSDT", "1", 0, 60, now=59) == [(0, 60)]


def test_checked_empty_ranges_temporarily_cover_gaps_and_survive_reopen(tmp_path):
    from candle_store import CandleStore
    path = tmp_path / "candles.sqlite3"
    store = CandleStore(path)
    store.save("binance", "BTCUSDT", "1", [candle(0, 60)], as_of=200)
    store.remember_empty("binance", "BTCUSDT", "1m", 50, 100, now=200, ttl=60)
    store.remember_empty("binance", "BTCUSDT", "1m", 90, 120, now=200, ttl=60)
    assert CandleStore(path).missing_ranges("binance", "BTCUSDT", "1", 30, 150, now=259) == [(120, 150)]
    assert store.missing_ranges("binance", "BTCUSDT", "1", 30, 150, now=260) == [(60, 150)]


def test_series_lock_coordinates_recheck_across_store_instances(tmp_path):
    from candle_store import CandleStore
    path = tmp_path / "candles.sqlite3"
    barrier = threading.Barrier(6)
    requests = []

    def worker():
        store = CandleStore(path)
        barrier.wait(timeout=5)
        with store.series_lock("BINANCE", "btcusdt", "1"):
            if not store.read("binance", "BTCUSDT", "1m"):
                requests.append(1)
                time.sleep(0.03)
                store.save("binance", "BTCUSDT", "1m", [candle()], as_of=60)
            return store.read("binance", "BTCUSDT", "1m")

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: worker(), range(6)))
    assert requests == [1]
    assert results == [[candle()]] * 6


def test_series_lock_is_reentrant_and_different_series_can_proceed(tmp_path):
    from candle_store import CandleStore
    first = CandleStore(tmp_path / "candles.sqlite3")
    second = CandleStore(first.path)
    with first.series_lock("binance", "BTCUSDT", "1"):
        with second.series_lock("BINANCE", "btcusdt", "1m"):
            with ThreadPoolExecutor(max_workers=1) as pool:
                def other_series():
                    with second.series_lock("binance", "ETHUSDT", "1m"):
                        return True
                assert pool.submit(other_series).result(timeout=5)


def test_series_lock_coordinates_separate_processes(tmp_path):
    from candle_store import CandleStore
    store = CandleStore(tmp_path / "candles.sqlite3")
    program = """import sys
from candle_store import CandleStore
store = CandleStore(sys.argv[1])
print('ready', flush=True)
with store.series_lock('BINANCE', 'btcusdt', '1m'):
    print('entered', flush=True)
"""
    process = None
    try:
        with store.series_lock("binance", "BTCUSDT", "1"):
            process = subprocess.Popen([sys.executable, "-c", program, str(store.path)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert process.stdout.readline().strip() == "ready"
            with pytest.raises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.15)
        output, error = process.communicate(timeout=10)
        assert process.returncode == 0, error
        assert output.strip() == "entered"
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
def test_nobitex_monthly_calendar_keeps_tehran_opening_boundary():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from candle_store import candle_close_time
    tehran = ZoneInfo("Asia/Tehran")
    for year, month, next_year, next_month in [(2026, 2, 2026, 3), (2024, 2, 2024, 3), (2026, 12, 2027, 1)]:
        opened = int(datetime(year, month, 1, tzinfo=tehran).timestamp())
        expected = int(datetime(next_year, next_month, 1, tzinfo=tehran).timestamp())
        assert candle_close_time("1M", opened, exchange="nobitex") == expected


def test_nobitex_daily_calendar_preserves_historical_dst():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from candle_store import candle_close_time
    tehran = ZoneInfo("Asia/Tehran")
    # September 2021's DST transition changes the elapsed length of a local day.
    opened = datetime(2021, 9, 21, tzinfo=tehran)
    expected = int((opened + timedelta(days=1)).timestamp())
    assert expected - int(opened.timestamp()) != 86400
    assert candle_close_time("D", int(opened.timestamp()), exchange="nobitex") == expected

