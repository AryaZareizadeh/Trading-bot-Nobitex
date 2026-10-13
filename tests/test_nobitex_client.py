import json
from types import SimpleNamespace

import pytest
import requests

import nobitex_client
from nobitex_client import NobitexAPIError, NobitexClient


@pytest.fixture
def client_clock(monkeypatch):
    clock = SimpleNamespace(now=0.0, sleeps=[])
    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds
    monkeypatch.setattr(nobitex_client, "time", SimpleNamespace(
        monotonic=lambda: clock.now, time=lambda: 1800000000 + clock.now, sleep=sleep))
    if hasattr(nobitex_client, "_COOLDOWNS"):
        monkeypatch.setattr(nobitex_client, "_COOLDOWNS", {})
    if hasattr(nobitex_client, "_REQUEST_WINDOWS"):
        monkeypatch.setattr(nobitex_client, "_REQUEST_WINDOWS", {})
    return clock


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    result.url = "https://apiv2.nobitex.ir/test"
    return result


def scripted_client(monkeypatch, replies):
    client = NobitexClient(token="test-token", allow_environment=False)
    calls = []
    def request(*args, **kwargs):
        calls.append((args, kwargs))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(client._session, "request", request)
    return client, calls


def test_legacy_nobitex_proxy_is_ignored_for_direct_only_connections(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://unrelated-proxy:8080")
    monkeypatch.delenv("NOBITEX_HTTPS_PROXY", raising=False)
    direct = NobitexClient(token="test-token", allow_environment=False)
    assert direct._session.trust_env is False
    assert direct._session.proxies == {}

    monkeypatch.setenv("NOBITEX_HTTPS_PROXY", "http://gateway.docker.internal:3128")
    proxied = NobitexClient(token="test-token", allow_environment=False)
    assert proxied._session.trust_env is False
    assert proxied._session.proxies == {}


@pytest.mark.parametrize("method", ["position", "order_status"])
def test_read_timeout_recovers_without_abandoning_order_tracking(monkeypatch, client_clock, method):
    client, calls = scripted_client(monkeypatch, [
        requests.ReadTimeout("temporary failure"),
        response({"status": "ok", "position": {"id": 42}, "order": {"id": 42}})])
    assert getattr(client, method)(42)["id"] == 42
    assert len(calls) == 2
    assert client_clock.sleeps


def test_rate_limit_backoff_is_honored_before_retry(monkeypatch, client_clock):
    client, calls = scripted_client(monkeypatch, [
        response({"status": "failed", "code": "TooManyRequests", "backOff": 12}, 429),
        response({"status": "ok", "order": {"id": 42}})])
    assert client.order_status(42)["id"] == 42
    assert sum(client_clock.sleeps) >= 12
    assert len(calls) == 2


def test_read_retries_are_bounded(monkeypatch, client_clock):
    client, calls = scripted_client(monkeypatch, [requests.ReadTimeout("timeout") for _ in range(10)])
    with pytest.raises(requests.ReadTimeout):
        client.order_status(42)
    assert len(calls) == 3


@pytest.mark.parametrize("method,args,kwargs", [
    ("open_margin_order", (), dict(src_currency="btc", dst_currency="usdt", side="buy",
                                  amount="1", price="100")),
    ("close_position", (42,), dict(amount="1", price="100")),
    ("cancel_order", (42,), {}),
    ("transfer_spot_to_margin", ("usdt", "100"), {}),
])
def test_mutation_timeout_never_replays_request(monkeypatch, client_clock, method, args, kwargs):
    client, calls = scripted_client(monkeypatch, [requests.ReadTimeout("response lost")])
    with pytest.raises(requests.ReadTimeout):
        getattr(client, method)(*args, **kwargs)
    assert len(calls) == 1


def test_structured_http_rejection_preserves_exchange_code(monkeypatch, client_clock):
    client, calls = scripted_client(monkeypatch, [
        response({"status": "failed", "code": "BadPrice", "message": "Price out of range"}, 400)])
    with pytest.raises(NobitexAPIError) as caught:
        client.open_margin_order(src_currency="btc", dst_currency="usdt", side="buy", amount="1", price="100")
    assert caught.value.code == "BadPrice"
    assert caught.value.http_status == 400
    assert len(calls) == 1


def test_long_backoff_blocks_other_clients_for_same_endpoint(monkeypatch, client_clock):
    one, calls = scripted_client(monkeypatch, [
        response({"status": "failed", "code": "TooManyRequests", "backOff": 120}, 429)])
    with pytest.raises(NobitexAPIError) as caught:
        one.position(42)
    assert caught.value.retry_after == 120
    two, other_calls = scripted_client(monkeypatch, [])
    with pytest.raises(NobitexAPIError) as blocked:
        two.position(99)
    assert blocked.value.code == "TooManyRequests"
    assert len(calls) == 1
    assert other_calls == []


def test_signed_read_retry_rebuilds_signature_after_backoff(monkeypatch, client_clock):
    client, calls = scripted_client(monkeypatch, [
        response({"status": "failed", "code": "TooManyRequests", "backOff": 12}, 429),
        response({"status": "ok", "order": {"id": 42}})])
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    client._ed_key = Ed25519PrivateKey.generate()
    client._public_key = "test-public-key"
    assert client.order_status(42)["id"] == 42
    for args, kwargs in calls:
        headers = kwargs["headers"]
        message = (headers["Nobitex-Timestamp"] + "POST/market/orders/status" + kwargs["data"]).encode()
        import base64
        client._ed_key.public_key().verify(base64.urlsafe_b64decode(headers["Nobitex-Signature"]), message)
    assert calls[0][1]["headers"]["Nobitex-Timestamp"] != calls[1][1]["headers"]["Nobitex-Timestamp"]


def test_active_orders_includes_later_pages(monkeypatch, client_clock):
    client, calls = scripted_client(monkeypatch, [
        response({"status": "ok", "orders": [{"id": 1}], "hasNext": True}),
        response({"status": "ok", "orders": [{"id": 2}], "hasNext": False})])
    assert [order["id"] for order in client.active_orders()] == [1, 2]
    assert "page=2" in calls[1][0][1]


def test_position_list_budget_is_shared_across_clients_and_pages(monkeypatch, client_clock):
    one, calls = scripted_client(monkeypatch, [
        response({"status": "ok", "positions": [], "hasNext": False}) for _ in range(31)])
    for _ in range(30):
        assert one.positions() == []
    two, other_calls = scripted_client(monkeypatch, [
        response({"status": "ok", "positions": [], "hasNext": False})])
    with pytest.raises(NobitexAPIError) as blocked:
        two._call("GET", "/positions/list?status=past&page=2")
    assert blocked.value.code == "TooManyRequests"
    assert other_calls == []
    client_clock.now += 600
    assert two.positions() == []
    assert len(calls) == 30
    assert len(other_calls) == 1


def test_rate_limited_mutation_is_not_queued_in_background_thread(monkeypatch, client_clock):
    one, calls = scripted_client(monkeypatch, [
        response({"status": "failed", "code": "TooManyRequests", "backOff": 12}, 429)])
    with pytest.raises(NobitexAPIError):
        one.open_margin_order(src_currency="btc", dst_currency="usdt", side="buy", amount="1", price="100")
    two, other_calls = scripted_client(monkeypatch, [response({"status": "ok", "order": {"id": 99}})])
    with pytest.raises(NobitexAPIError):
        two.open_margin_order(src_currency="btc", dst_currency="usdt", side="buy", amount="1", price="100")
    assert other_calls == []
    assert client_clock.sleeps == []
