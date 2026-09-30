from nobitex_client import NobitexClient


def test_explicit_nobitex_proxy_does_not_enable_other_environment_proxies(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://unrelated-proxy:8080")
    monkeypatch.delenv("NOBITEX_HTTPS_PROXY", raising=False)
    direct = NobitexClient(token="test-token", allow_environment=False)
    assert direct._session.trust_env is False
    assert direct._session.proxies == {}

    monkeypatch.setenv("NOBITEX_HTTPS_PROXY", "http://gateway.docker.internal:3128")
    proxied = NobitexClient(token="test-token", allow_environment=False)
    assert proxied._session.trust_env is False
    assert proxied._session.proxies == {"https": "http://gateway.docker.internal:3128"}
