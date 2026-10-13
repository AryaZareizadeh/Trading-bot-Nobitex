"""Shared persistent public candles, independent of account and trading state.

Connections live only for individual database operations.  Series locks can
therefore span a cache recheck and an HTTP request without holding SQLite open.
Importing this module creates no files or database connections.
"""
import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from settings import data_dir


_INTERVALS = {
    "1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1h",
    "180": "3h", "240": "4h", "D": "1d", "W": "1w", "M": "1M",
}
_DURATIONS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
    "3h": 10800, "4h": 14400, "1d": 86400, "1w": 604800,
}
_thread_locks = {}
_thread_locks_guard = threading.Lock()
_held_locks = threading.local()
_SQLITE_INTEGER_LIMIT = 2 ** 63 - 1


def canonical_interval(interval):
    """Normalize application timeframe codes, keeping months case-sensitive."""
    value = str(interval).strip()
    if value in _INTERVALS:
        return _INTERVALS[value]
    if value in _DURATIONS or value == "1M":
        return value
    raise ValueError(f"Unsupported candle interval: {interval!r}")


def _integer_time(value):
    if isinstance(value, bool):
        raise ValueError("A candle timestamp must be an integer")
    integer = int(value)
    if not math.isfinite(float(value)) or integer != float(value):
        raise ValueError("A candle timestamp must be a finite integer")
    if not -_SQLITE_INTEGER_LIMIT <= integer <= _SQLITE_INTEGER_LIMIT:
        raise ValueError("Candle timestamp is outside SQLite's integer range")
    return integer


def candle_close_time(interval, opened, exchange="binance"):
    """Derive an exclusive close, preserving exchange calendar boundaries.

    A provider-supplied closing boundary is authoritative when saving a row;
    this helper is a fallback for fixed durations and UTC monthly candles.
    """
    interval = canonical_interval(interval)
    opened = _integer_time(opened)
    if str(exchange).lower() == "nobitex" and interval in {"1d", "1w", "1M"}:
        start = datetime.fromtimestamp(opened, tz=ZoneInfo("Asia/Tehran"))
        if interval == "1M":
            year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
            return int(start.replace(year=year, month=month, day=1).timestamp())
        midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
        # At Iran's historical spring transition, midnight was skipped and
        # round-trips to 01:00. Its following boundary is still local midnight.
        if midnight.timestamp() == opened:
            start = midnight
        return int((start + timedelta(days=1 if interval == "1d" else 7)).timestamp())
    if interval != "1M":
        return opened + _DURATIONS[interval]
    start = datetime.fromtimestamp(opened, tz=timezone.utc)
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())


def _series(exchange, instrument, interval):
    exchange, instrument = str(exchange).strip().lower(), str(instrument).strip().upper()
    if not exchange or not instrument:
        raise ValueError("Candle exchange and instrument must be nonempty")
    return exchange, instrument, canonical_interval(interval)


@contextmanager
def _file_lock(database_path, key):
    """Reentrant thread lock plus an advisory OS lock shared across processes."""
    path_key = os.path.normcase(str(database_path))
    identity = (path_key, key)
    with _thread_locks_guard:
        lock = _thread_locks.setdefault(identity, threading.RLock())
    with lock:
        held = getattr(_held_locks, "locks", None)
        if held is None:
            held = _held_locks.locks = set()
        if identity in held:
            yield
            return
        directory = database_path.parent / (database_path.name + ".locks")
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with open(directory / (digest + ".lock"), "a+b") as stream:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                while True:
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as error:
                        # Windows reports a byte-range lock held by another
                        # process as EACCES/EAGAIN (occasionally EDEADLK).
                        if error.errno not in (11, 13, 36):
                            raise
                        time.sleep(0.05)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            held.add(identity)
            try:
                yield
            finally:
                held.remove(identity)
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class CandleStore:
    def __init__(self, path=None):
        self.path = Path(path if path is not None else data_dir() / "candles.sqlite3").resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serializing initialization also avoids journal-mode races when many
        # processes encounter an empty database at the same time.
        with _file_lock(self.path, "schema"):
            with self._connection() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS candles (
                        exchange TEXT NOT NULL,
                        instrument TEXT NOT NULL,
                        interval TEXT NOT NULL,
                        open_time INTEGER NOT NULL,
                        close_time INTEGER NOT NULL,
                        open REAL NOT NULL,
                        high REAL NOT NULL,
                        low REAL NOT NULL,
                        close REAL NOT NULL,
                        volume REAL NOT NULL,
                        metadata TEXT NOT NULL,
                        PRIMARY KEY (exchange, instrument, interval, open_time)
                    );
                    CREATE INDEX IF NOT EXISTS candles_coverage
                        ON candles(exchange, instrument, interval, close_time, open_time);
                    CREATE TABLE IF NOT EXISTS checked_empty (
                        exchange TEXT NOT NULL,
                        instrument TEXT NOT NULL,
                        interval TEXT NOT NULL,
                        start_time REAL NOT NULL,
                        end_time REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        PRIMARY KEY (exchange, instrument, interval, start_time, end_time)
                    );
                    CREATE INDEX IF NOT EXISTS checked_empty_expiry ON checked_empty(expires_at);
                """)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def series_lock(self, exchange, instrument, interval):
        """Coordinate a cache recheck and fetch for one canonical series."""
        key = json.dumps(_series(exchange, instrument, interval), separators=(",", ":"))
        with _file_lock(self.path, key):
            yield

    def save(self, exchange, instrument, interval, rows, *, as_of):
        """Atomically retain valid completed rows, never revising stored bars."""
        series = _series(exchange, instrument, interval)
        as_of = float(as_of)
        if not math.isfinite(as_of):
            raise ValueError("as_of must be finite")
        records = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            try:
                opened, closed = _integer_time(row["open_time"]), _integer_time(row["close_time"])
                prices = [float(row[name]) for name in ("open", "high", "low", "close")]
                volume = float(row["volume"])
                if closed <= opened or closed > as_of:
                    continue
                if not all(math.isfinite(value) and value > 0 for value in prices):
                    continue
                opening, high, low, closing = prices
                if not low <= opening <= high or not low <= closing <= high:
                    continue
                if not math.isfinite(volume) or volume < 0:
                    continue
                metadata = row.get("metadata", {})
                if not isinstance(metadata, dict):
                    continue
                encoded = json.dumps(metadata, allow_nan=False, separators=(",", ":"))
                records.append((*series, opened, closed, *prices, volume, encoded))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        if not records:
            return 0
        with self._connection() as connection:
            previous = connection.total_changes
            connection.executemany("INSERT OR IGNORE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", records)
            return connection.total_changes - previous

    def read(self, exchange, instrument, interval, *, start=None, end=None, as_of=None, limit=None):
        """Read chronological rows; a limit selects the newest matching bars."""
        parameters = list(_series(exchange, instrument, interval))
        conditions = ["exchange=?", "instrument=?", "interval=?"]
        for value, condition in ((start, "open_time>=?"), (end, "open_time<?"), (as_of, "close_time<=?")):
            if value is not None:
                conditions.append(condition)
                parameters.append(value)
        query = "SELECT open_time, close_time, open, high, low, close, volume, metadata FROM candles WHERE " + " AND ".join(conditions)
        if limit is None:
            query += " ORDER BY open_time"
        else:
            limit = _integer_time(limit)
            if limit < 0:
                raise ValueError("Candle limit cannot be negative")
            query += " ORDER BY open_time DESC LIMIT ?"
            parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        if limit is not None:
            rows.reverse()
        return [dict(open_time=row[0], close_time=row[1], open=row[2], high=row[3], low=row[4],
                     close=row[5], volume=row[6], metadata=json.loads(row[7])) for row in rows]

    def remember_empty(self, exchange, instrument, interval, start, end, *, now, ttl=60):
        """Temporarily cover a successfully checked no-data range."""
        series = _series(exchange, instrument, interval)
        start, end, now, ttl = map(float, (start, end, now, ttl))
        if not all(math.isfinite(value) for value in (start, end, now, ttl)):
            raise ValueError("Checked-empty bounds and lifetime must be finite")
        if end <= start or ttl <= 0:
            return
        with self._connection() as connection:
            connection.execute("DELETE FROM checked_empty WHERE expires_at<=?", (now,))
            connection.execute("""INSERT INTO checked_empty VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange, instrument, interval, start_time, end_time)
                DO UPDATE SET expires_at=excluded.expires_at""", (*series, start, end, now + ttl))

    def missing_ranges(self, exchange, instrument, interval, start, end, *, now):
        """Subtract actual candle coverage and unexpired checked-empty spans."""
        series = _series(exchange, instrument, interval)
        if not all(math.isfinite(float(value)) for value in (start, end, now)):
            raise ValueError("Candle range bounds and time must be finite")
        if end <= start:
            return []
        with self._connection() as connection:
            bars = connection.execute("""SELECT open_time, close_time FROM candles
                WHERE exchange=? AND instrument=? AND interval=?
                AND open_time<? AND close_time>? AND close_time<=?""", (*series, end, start, now)).fetchall()
            empty = connection.execute("""SELECT start_time, end_time FROM checked_empty
                WHERE exchange=? AND instrument=? AND interval=?
                AND start_time<? AND end_time>? AND expires_at>?""", (*series, end, start, now)).fetchall()
        cursor, missing = start, []
        for opened, closed in sorted(bars + empty):
            opened, closed = max(start, opened), min(end, closed)
            if opened > cursor:
                missing.append((cursor, opened))
            cursor = max(cursor, closed)
        if cursor < end:
            missing.append((cursor, end))
        return missing
