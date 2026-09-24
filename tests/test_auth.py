"""Authentication tests use a small app and never start exchange polling."""
import time

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def auth_app(password="correct horse battery staple", **kwargs):
    import auth

    app = FastAPI()
    state = auth.AuthState(password_hash=auth.hash_password(password) if password else "",
                           allowed_hosts={"testserver"}, **kwargs)
    auth.install_auth(app, state)

    @app.get("/api/bots")
    async def bots():
        return [{"name": "example"}]

    @app.post("/api/bots")
    async def create_bot():
        return {"created": True}

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        await ws.send_json({"connected": True})
        await ws.receive_text()

    return app, state


def login(client):
    response = client.post("/api/auth/login", json={"password": "correct horse battery staple"})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def test_password_hash_is_salted_and_rejects_wrong_password():
    import auth
    encoded = auth.hash_password("correct horse battery staple")
    assert encoded != auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", encoded)
    assert not auth.verify_password("incorrect password", encoded)
    assert not auth.verify_password("correct horse battery staple", "invalid hash")


def test_no_password_locks_data_and_reports_setup_required():
    app, _ = auth_app(password=None)
    with TestClient(app) as client:
        assert client.get("/api/auth/session").json()["setup_required"]
        assert client.get("/api/bots").status_code == 401
        assert client.post("/api/auth/login", json={"password": "anything"}).status_code == 401


def test_anonymous_reads_writes_docs_and_websocket_are_blocked():
    app, _ = auth_app()
    with TestClient(app) as client:
        for path in ["/api/bots", "/docs", "/openapi.json", "/redoc"]:
            assert client.get(path).status_code == 401
        assert client.post("/api/bots").status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws"):
                pytest.fail("Anonymous socket accepted")


def test_authenticated_session_csrf_origin_host_and_logout():
    app, _ = auth_app()
    with TestClient(app) as client:
        headers = login(client)
        assert client.get("/api/bots").json() == [{"name": "example"}]
        assert client.post("/api/bots").status_code == 403
        assert client.post("/api/bots", headers=headers).json() == {"created": True}
        assert client.post("/api/bots", headers={**headers, "Origin": "https://evil.invalid"}).status_code == 403
        assert client.get("/api/bots", headers={"Host": "evil.invalid"}).status_code == 400
        assert client.get("/api/auth/session").json()["authenticated"]
        cookie = next(c for c in client.cookies.jar if c.name == "dashboard_session")
        assert cookie.has_nonstandard_attr("HttpOnly")
        with client.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws:
            assert ws.receive_json() == {"connected": True}
            assert client.post("/api/auth/logout", headers=headers).status_code == 200
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        assert client.get("/api/bots").status_code == 401


def test_session_expiry_closes_socket_and_blocks_reads():
    app, _ = auth_app(session_seconds=0.15)
    with TestClient(app) as client:
        login(client)
        with client.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws:
            assert ws.receive_json() == {"connected": True}
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        assert client.get("/api/bots").status_code == 401


def test_cross_origin_websocket_and_login_are_rejected():
    app, _ = auth_app()
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": "correct horse battery staple"},
                           headers={"Origin": "http://evil.invalid"}).status_code == 403
        login(client)
        for headers in [{"Origin": "http://evil.invalid"}, {}]:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/ws", headers=headers):
                    pytest.fail("Cross-origin or originless socket accepted")


def test_failed_login_is_rate_limited_and_never_echoes_password():
    app, _ = auth_app(login_attempts=2)
    with TestClient(app) as client:
        for _ in range(2):
            response = client.post("/api/auth/login", json={"password": "not-a-real-secret"})
            assert response.status_code == 401
            assert "not-a-real-secret" not in response.text
        assert client.post("/api/auth/login", json={"password": "correct horse battery staple"}).status_code == 429


def test_secure_cookie_setting_for_https():
    app, _ = auth_app(secure_cookie=True)
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post("/api/auth/login", json={"password": "correct horse battery staple"})
        assert "Secure" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert client.get("/api/bots").status_code == 200


def test_real_dashboard_routes_are_locked(monkeypatch):
    import server
    from auth import AuthState
    monkeypatch.setattr(server.app.state, "auth", AuthState(password_hash="", allowed_hosts={"testserver"}), raising=False)
    # No context manager: intentionally do not run the server lifespan.
    client = TestClient(server.app)
    for path in ["/api/bots", "/api/wallet", "/api/settings/credentials", "/api/tester/config", "/api/optimizer/config"]:
        assert client.get(path).status_code == 401
    assert client.post("/api/settings/credentials", json={"token": "unused"}).status_code == 401


@pytest.mark.parametrize("payload", [
    {}, {"token": "invalid\nINJECTED=yes"},
    {"public_key": "public", "private_key": "invalid-base64"},
    {"token": "token", "public_key": "public", "private_key": "private"},
])
def test_invalid_credentials_preserve_file_and_environment(payload, tmp_path, monkeypatch):
    import asyncio
    import os
    import server
    environment = tmp_path / ".env"
    environment.write_text("NOBITEX_TOKEN=old-token\n", encoding="utf-8")
    monkeypatch.setenv("TRADER_ENV_FILE", str(environment))
    monkeypatch.setenv("NOBITEX_TOKEN", "old-token")
    response = asyncio.run(server.set_credentials(server.CredentialsRequest(**payload)))
    assert response.status_code == 400
    assert os.getenv("NOBITEX_TOKEN") == "old-token"
    assert environment.read_text(encoding="utf-8") == "NOBITEX_TOKEN=old-token\n"
    assert b"old-token" not in response.body


def test_credential_save_preserves_auth_hash_and_reports_only_presence(tmp_path, monkeypatch):
    import asyncio
    import os
    import server
    from dotenv import dotenv_values
    environment = tmp_path / ".env"
    environment.write_text("DASHBOARD_PASSWORD_HASH=preserved-hash\n", encoding="utf-8")
    monkeypatch.setenv("TRADER_ENV_FILE", str(environment))
    result = asyncio.run(server.set_credentials(server.CredentialsRequest(token="new-secret-token", testnet=True)))
    assert result["has_credentials"] is True
    assert "new-secret-token" not in str(result)
    assert os.getenv("NOBITEX_TOKEN") == "new-secret-token"
    assert dotenv_values(environment)["DASHBOARD_PASSWORD_HASH"] == "preserved-hash"


def test_credential_write_failure_keeps_prior_environment(monkeypatch):
    import asyncio
    import os
    import server
    import settings
    monkeypatch.setenv("NOBITEX_TOKEN", "old-token")
    def disk_failure(*args, **kwargs):
        raise OSError("private-path-or-secret")
    monkeypatch.setattr(settings.os, "replace", disk_failure)
    response = asyncio.run(server.set_credentials(server.CredentialsRequest(token="new-secret-token")))
    assert response.status_code == 500
    assert b"private-path-or-secret" not in response.body
    assert os.getenv("NOBITEX_TOKEN") == "old-token"


@pytest.mark.parametrize("condition", ["running", "position", "intent"])
def test_credentials_cannot_change_with_live_exposure(condition, monkeypatch):
    import asyncio
    import server
    from types import SimpleNamespace
    bot = SimpleNamespace(cfg=SimpleNamespace(mode="real"),
        status=server.BotStatus.RUNNING if condition == "running" else server.BotStatus.IDLE,
        _exchange_position_id=123 if condition == "position" else None,
        unresolved_intents=lambda: [{"state": "submitted"}] if condition == "intent" else [])
    monkeypatch.setattr(server.bot_manager, "get_all", lambda: [bot])
    def unexpected_save(*args, **kwargs):
        pytest.fail("Credentials changed despite exposure")
    monkeypatch.setattr(server, "update_environment", unexpected_save)
    response = asyncio.run(server.set_credentials(server.CredentialsRequest(token="fake-token")))
    assert response.status_code == 409


def test_credential_schema_errors_do_not_echo_secrets(monkeypatch):
    import server
    import auth
    state = auth.AuthState(password_hash=auth.hash_password("correct horse battery staple"),
                           allowed_hosts={"testserver"})
    monkeypatch.setattr(server.app.state, "auth", state)
    client = TestClient(server.app)
    headers = login(client)
    response = client.post("/api/settings/credentials", headers=headers,
                           json={"token": {"nested-secret": "never-echo-me"}})
    assert response.status_code == 422
    assert "never-echo-me" not in response.text
    assert "nested-secret" not in response.text


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1, 0])
def test_session_duration_must_be_finite_positive(duration):
    with pytest.raises(ValueError):
        auth_app(session_seconds=duration)


def test_test_configuration_paths_are_isolated(tmp_path):
    import settings
    assert settings.env_path() == tmp_path / ".env"
    assert settings.data_dir() == tmp_path


def test_malformed_unicode_password_is_rejected_without_server_error():
    app, _ = auth_app()
    with TestClient(app) as client:
        response = client.post("/api/auth/login", content=b'{"password":"\\ud800"}',
                               headers={"Content-Type": "application/json"})
        assert response.status_code == 401


def test_non_ascii_csrf_is_rejected_without_server_error():
    app, _ = auth_app()
    with TestClient(app) as client:
        login(client)
        assert client.post("/api/bots", headers={b"X-CSRF-Token": b"\xe9"}).status_code == 403
