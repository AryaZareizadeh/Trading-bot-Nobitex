"""Transactional local state. Importing this module never opens a database."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from settings import data_dir


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._runtime_file = None
        with self.connection() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("Database was created by a newer application version")
            conn.execute("PRAGMA journal_mode=WAL")
            if version == 0:
                conn.executescript((Path(__file__).parent / "migrations/001_initial.sql").read_text())
            if version < 2:
                conn.executescript((Path(__file__).parent / "migrations/002_portfolio.sql").read_text())

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def write(conn, namespace, key, value):
        conn.execute("INSERT INTO records(namespace,key,value) VALUES(?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
                     (namespace, str(key), json.dumps(value, allow_nan=False)))

    def put(self, namespace, key, value):
        with self.connection() as conn:
            self.write(conn, namespace, key, value)

    def get(self, namespace, key, default=None):
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM records WHERE namespace=? AND key=?", (namespace, str(key))).fetchone()
        return json.loads(row[0]) if row else default

    def list(self, namespace):
        with self.connection() as conn:
            rows = conn.execute("SELECT key,value FROM records WHERE namespace=? ORDER BY key", (namespace,)).fetchall()
        return {key: json.loads(value) for key, value in rows}

    def delete(self, namespace, key):
        with self.connection() as conn:
            conn.execute("DELETE FROM records WHERE namespace=? AND key=?", (namespace, str(key)))

    def owner(self, account, position_id):
        with self.connection() as conn:
            row = conn.execute("SELECT bot_id FROM position_owners WHERE account=? AND position_id=?", (account, str(position_id))).fetchone()
        return row[0] if row else None

    def claim_position(self, account, position_id, bot_id):
        with self.connection() as conn:
            conn.execute("INSERT OR IGNORE INTO position_owners VALUES(?,?,?)", (account, str(position_id), bot_id))
            owner = conn.execute("SELECT bot_id FROM position_owners WHERE account=? AND position_id=?", (account, str(position_id))).fetchone()[0]
            if owner != bot_id:
                raise ValueError("Exchange position is already owned by another bot")

    def acquire_runtime(self):
        if self._runtime_file is not None:
            raise RuntimeError("Trading runtime already active")
        stream = open(str(self.path) + ".lock", "a+b")
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError("Trading runtime already active for this database") from exc
        self._runtime_file = stream

    def release_runtime(self):
        if self._runtime_file is not None:
            stream, self._runtime_file = self._runtime_file, None
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def backup(self, destination):
        destination = Path(destination).resolve()
        if destination == self.path:
            raise ValueError("Backup must use a different path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()


_stores = {}
_lock = threading.Lock()


def get_store():
    path = data_dir() / "trader.sqlite3"
    with _lock:
        if path not in _stores:
            _stores[path] = Store(path)
        return _stores[path]
