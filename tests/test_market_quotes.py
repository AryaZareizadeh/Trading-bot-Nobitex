from concurrent.futures import ThreadPoolExecutor
import importlib

import requests


class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload = payload or {'status': 'ok', 'stats': {'btc-usdt': {'latest': '101.5'}}}
        self.status_code, self.headers = status, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self.payload


def service(path, clock, get):
    module = importlib.import_module('market_quotes')
    return module.MarketQuotes(path, clock=lambda: clock[0], get=get)


def test_many_instances_and_bots_share_six_calls_per_minute(tmp_path):
    clock, calls = [1000.0], []
    def get(*args, **kwargs):
        calls.append(clock[0])
        return Response()
    instances = [service(tmp_path / 'quotes.sqlite3', clock, get) for _ in range(12)]
    for tick in range(60):
        clock[0] = 1000 + tick
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda instance: instance.refresh_due(), instances))
        assert instances[0].quote('BTCUSDT')['live_price'] == 101.5
    assert calls == [1000, 1010, 1020, 1030, 1040, 1050]


def test_rate_limit_cooldown_survives_reopen_and_retains_last_price(tmp_path):
    clock, calls = [1000.0], []
    def get(*args, **kwargs):
        calls.append(clock[0])
        return Response() if len(calls) == 1 else Response(status=429, headers={'Retry-After': '120'})
    path = tmp_path / 'quotes.sqlite3'
    quotes = service(path, clock, get)
    quotes.refresh_due()
    clock[0] += 10
    quotes.refresh_due()
    clock[0] += 50
    other = service(path, clock, get)
    other.refresh_due()
    quote = other.quote('btcusdt')
    assert len(calls) == 2
    assert quote['live_price'] == 101.5
    assert quote['live_price_stale'] is True
    assert quote['live_price_updated_at'] == 1000
    assert quote['live_price_age_seconds'] == 60


def test_missing_or_invalid_market_is_not_shown_as_live(tmp_path):
    clock = [1000.0]
    quotes = service(tmp_path / 'quotes.sqlite3', clock, lambda *a, **k: Response(
        {'status': 'ok', 'stats': {'btc-usdt': {'latest': 'NaN'}, 'eth-usdt': {'latest': '0'}}}))
    quotes.refresh_due()
    assert quotes.quote('BTCUSDT')['live_price'] is None
    assert quotes.quote('ETHUSDT')['live_price'] is None
    assert quotes.quote('ZECUSDT')['live_price_stale'] is True


def test_separate_processes_share_one_public_request(tmp_path):
    import subprocess
    import sys
    script = '''
import sys
from pathlib import Path
from market_quotes import MarketQuotes
class Response:
    def raise_for_status(self): pass
    def json(self): return {'status':'ok','stats':{'btc-usdt':{'latest':'101.5'}}}
def get(*args, **kwargs):
    with Path(sys.argv[2]).open('a') as output: output.write('request\\n')
    return Response()
MarketQuotes(sys.argv[1],clock=lambda:1000,get=get).refresh_due()
'''
    path, calls = tmp_path / 'quotes.sqlite3', tmp_path / 'requests.txt'
    workers = [subprocess.Popen([sys.executable, '-c', script, str(path), str(calls)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
    for worker in workers:
        stdout, stderr = worker.communicate(timeout=30)
        assert worker.returncode == 0, stderr.decode()
    assert calls.read_text().splitlines() == ['request']


def test_retry_after_http_date_and_symbol_mapping(tmp_path):
    clock, count = [1000.0], [0]
    def get(*args, **kwargs):
        count[0] += 1
        if count[0] == 1:
            return Response({'status':'ok','stats':{'btc-rls':{'latest':'1000'}}})
        return Response(status=429, headers={'Retry-After':'Thu, 01 Jan 1970 00:20:00 GMT'})
    quotes = service(tmp_path / 'quotes.sqlite3', clock, get)
    quotes.refresh_due()
    assert quotes.quote('BTCIRT')['live_price'] == quotes.quote('BTCRLS')['live_price'] == 1000
    clock[0] = 1010
    quotes.refresh_due()
    clock[0] = 1199
    quotes.refresh_due()
    assert count[0] == 2
    clock[0] = 1200
    quotes.refresh_due()
    assert count[0] == 3


def test_closed_market_retains_price_but_is_immediately_stale(tmp_path):
    clock, count = [1000.0], [0]
    def get(*args, **kwargs):
        count[0] += 1
        if count[0] == 1:
            return Response()
        return Response({'status':'ok','stats':{
            'btc-usdt':{'latest':'101.5','isClosed':True}, 'eth-usdt':{'latest':'55'}}})
    quotes = service(tmp_path / 'quotes.sqlite3', clock, get)
    quotes.refresh_due()
    clock[0] += 10
    quotes.refresh_due()
    assert quotes.quote('BTCUSDT')['live_price'] == 101.5
    assert quotes.quote('BTCUSDT')['live_price_stale'] is True
    assert quotes.quote('ETHUSDT')['live_price_stale'] is False
