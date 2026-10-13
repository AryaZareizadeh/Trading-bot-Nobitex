"""Shared public display quotes, with a durable polling budget across workers."""
from contextlib import closing
from email.utils import parsedate_to_datetime
from functools import lru_cache
import json
import math
from pathlib import Path
import sqlite3
import time

from nobitex_http import direct_nobitex_session
from runtime_lease import FileLease
from settings import data_dir


class MarketQuotes:
    POLL_SECONDS = 10
    STALE_SECONDS = 30
    URL = 'https://apiv2.nobitex.ir/market/stats'

    def __init__(self, path, *, clock=None, get=None):
        self.path = Path(path)
        self.clock = clock or time.time
        self.session = direct_nobitex_session()
        self.get = get or self.session.get

    @staticmethod
    def _empty():
        return {'quotes': {}, 'next_poll': 0, 'failures': 0}

    def snapshot(self):
        if not self.path.exists():
            return self._empty()
        with closing(sqlite3.connect(self.path, timeout=15)) as connection:
            try:
                row = connection.execute('SELECT payload FROM market_quotes WHERE id=1').fetchone()
            except sqlite3.OperationalError as exc:
                if 'no such table' in str(exc):
                    return self._empty()  # Another worker is creating the initial schema.
                raise
        return json.loads(row[0]) if row else self._empty()

    def _save(self, state):
        with closing(sqlite3.connect(self.path, timeout=15)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS market_quotes (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
            connection.execute('INSERT OR REPLACE INTO market_quotes VALUES (1, ?)', (json.dumps(state),))

    def refresh_due(self):
        # A nonblocking OS lease prevents duplicate HTTP across threads/processes.
        # The persisted reservation also survives restarts and request failures.
        lease = FileLease(self.path.with_suffix('.lock'))
        try:
            lease.acquire()
        except RuntimeError:
            return False
        try:
            state = self.snapshot()
            now = self.clock()
            if now < state['next_poll']:
                return False
            state['next_poll'] = now + self.POLL_SECONDS
            self._save(state)
            try:
                response = self.get(self.URL, timeout=15)
                response.raise_for_status()
                payload = response.json()
                if payload.get('status') != 'ok' or not isinstance(payload.get('stats'), dict):
                    raise ValueError('Invalid market stats')
                fetched_at = self.clock()
                valid = {}
                for market, item in payload['stats'].items():
                    if market.lower() in state['quotes']:
                        state['quotes'][market.lower()]['stale'] = True
                    if not isinstance(item, dict) or item.get('isClosed'):
                        continue
                    try:
                        if isinstance(item['latest'], bool):
                            continue
                        price = float(item['latest'])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if math.isfinite(price) and price > 0:
                        valid[market.lower()] = {'price': price, 'updated_at': fetched_at, 'stale': False}
                if not valid:
                    raise ValueError('No valid market prices')
                state['quotes'].update(valid)
                state['failures'] = 0
                state['next_poll'] = fetched_at + self.POLL_SECONDS
            except Exception as exc:
                state['failures'] = min(state['failures'] + 1, 8)
                wait = min(300, self.POLL_SECONDS * 2 ** state['failures'])
                response = getattr(exc, 'response', None)
                if response is not None and response.status_code == 429:
                    retry = response.headers.get('Retry-After', '')
                    try:
                        delay = float(retry)
                    except (TypeError, ValueError):
                        try:
                            delay = parsedate_to_datetime(retry).timestamp() - self.clock()
                        except (TypeError, ValueError, OverflowError):
                            delay = 0
                    if math.isfinite(delay):
                        wait = max(wait, delay)
                state['next_poll'] = self.clock() + wait
            self._save(state)
            return True
        finally:
            lease.release()

    def quote(self, symbol, *, snapshot=None):
        state = self.snapshot() if snapshot is None else snapshot
        symbol = str(symbol).lower()
        # Stats use rls; public chart/orderbook symbols use IRT for that quote.
        market = next((symbol[:-len(dst)] + '-' + ('rls' if dst == 'irt' else dst)
                       for dst in ('usdt', 'irt', 'rls') if symbol.endswith(dst)), '')
        item = state['quotes'].get(market, {})
        updated = item.get('updated_at')
        age = max(0, self.clock() - updated) if updated is not None else None
        return {'live_price': item.get('price'), 'live_price_updated_at': updated,
                'live_price_age_seconds': round(age, 1) if age is not None else None,
                'live_price_stale': updated is None or bool(state['failures']) or item.get('stale', False) or age >= self.STALE_SECONDS,
                'live_price_source': 'nobitex'}


@lru_cache(maxsize=8)
def _shared_quotes(path):
    return MarketQuotes(path)


def shared_market_quotes():
    return _shared_quotes(str(data_dir() / 'market-quotes.sqlite3'))
