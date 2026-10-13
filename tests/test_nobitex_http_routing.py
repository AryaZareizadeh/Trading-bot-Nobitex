"""Nobitex connects directly while other exchanges keep proxy/VPN routing."""
import io
import json
from email.message import Message
import urllib.request
import urllib.response

import pytest
import requests

from config import Config
from data import DataFetcher
from market_quotes import MarketQuotes
from nobitex_client import NobitexClient
from spot_data import NobitexPublicSpot, PublicSpotData
import position_limits


@pytest.mark.parametrize("consumer", ["wallet", "candles", "quotes", "spot"])
@pytest.mark.parametrize("proxy", [None, "http://gateway.docker.internal:3128"])
def test_nobitex_requests_are_direct_even_when_proxy_variables_are_set(tmp_path, monkeypatch, consumer, proxy):
    monkeypatch.setenv("HTTPS_PROXY", "http://unrelated-vpn-proxy:8080")
    monkeypatch.setenv("ALL_PROXY", "http://unrelated-all-proxy:8080")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "unrelated-ca-bundle.pem")
    if proxy:
        monkeypatch.setenv("NOBITEX_HTTPS_PROXY", proxy)
    else:
        monkeypatch.delenv("NOBITEX_HTTPS_PROXY", raising=False)
    sent = []
    def send(session, request, **kwargs):
        sent.append((request.url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({
            "status": "ok", "wallets": {}, "stats": {"btc-usdt": {"latest": "101"}},
            "s": "no_data", "BTCUSDT": {"asks": [], "bids": []},
        }).encode()
        response.request = request
        return response
    # Keep Requests' real proxy/certificate resolution, replace only socket I/O.
    monkeypatch.setattr(requests.Session, "request", ORIGINAL_REQUEST)
    monkeypatch.setattr(requests.Session, "send", send)
    if consumer == "wallet":
        NobitexClient(token="test-token", allow_environment=False).wallets("margin")
    elif consumer == "candles":
        fetcher = DataFetcher(Config(symbol="btcusdt", interval="60"))
        fetcher._download(1700000000, 1, 1700000000)
    elif consumer == "quotes":
        quotes = MarketQuotes(tmp_path / "quotes.sqlite3")
        quotes.refresh_due()
        assert quotes.quote("btcusdt")["live_price"] == 101.
    else:
        NobitexPublicSpot().books()
    assert len(sent) == 1
    url, kwargs = sent[0]
    assert url.startswith("https://apiv2.nobitex.ir/")
    assert kwargs["proxies"] == {}
    assert kwargs["verify"] is True


ORIGINAL_REQUEST = requests.Session.request


@pytest.mark.parametrize("consumer", ["bot", "public"])
def test_optional_binance_candles_keep_their_existing_proxy_route(monkeypatch, consumer):
    monkeypatch.setenv("HTTPS_PROXY", "http://research-vpn:8080")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("NOBITEX_HTTPS_PROXY", "http://nobitex-only:3128")
    sent = []
    def send(session, request, **kwargs):
        sent.append(kwargs["proxies"])
        response = requests.Response()
        response.status_code = 200
        response._content = b"[]"
        response.request = request
        return response
    monkeypatch.setattr(requests.Session, "request", ORIGINAL_REQUEST)
    monkeypatch.setattr(requests.Session, "send", send)
    if consumer == "bot":
        config = Config(symbol="btcusdt", interval="60")
        config.exchange = "binance"
        DataFetcher(config)._download(1700000000, 1, 1700000000)
    else:
        PublicSpotData().fetch("BTC", "1h", 1699999200, 1700002800)
    assert sent
    assert all(proxies["https"] == "http://research-vpn:8080" for proxies in sent)


def test_injected_public_session_is_preserved():
    supplied = object()
    assert NobitexPublicSpot(session=supplied).session is supplied


ORIGINAL_RULE_OPEN = position_limits.urlopen


def test_nobitex_help_rule_bypasses_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://unrelated-vpn-proxy:8080")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("TRADER_OFFLINE", "false")
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(position_limits, "urlopen", ORIGINAL_RULE_OPEN)
    monkeypatch.setattr(position_limits, "_next_refresh", 0)
    monkeypatch.setattr(position_limits, "_days", 60)
    monkeypatch.setattr(position_limits, "_verified_at", None)
    monkeypatch.setattr(position_limits, "_last_refresh_ok", False)
    captured = []
    def open_https(handler, request):
        captured.append((request.host, getattr(request, "_tunnel_host", None)))
        html = "<p>هر سفارش تعهدی، حداکثر تا 45 روز قابل تمدید است.</p>"
        response = urllib.response.addinfourl(io.BytesIO(html.encode()), Message(), request.full_url, 200)
        response.msg = "OK"
        return response
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", open_https)
    assert position_limits.current_max_hold_days() == 45
    assert captured == [("nobitx.com", None)]
