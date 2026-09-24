import time
from fastapi.testclient import TestClient
import pytest

from auth import AuthState, hash_password
from bot_manager import BotManager
from storage import Store
from tests.fakes import synthetic_candles


def client_for(tmp_path):
    from server import create_app
    manager = BotManager(Store(tmp_path / "trader.sqlite3"), tmp_path)
    app = create_app(manager=manager, offline=True)
    app.state.auth = AuthState(password_hash=hash_password("test password sufficiently long"), allowed_hosts={"testserver"})
    return TestClient(app), manager


def login(client):
    r = client.post("/api/auth/login", json={"password": "test password sufficiently long"})
    assert r.status_code == 200
    return {"X-CSRF-Token": r.json()["csrf_token"], "Origin": "http://testserver"}


def test_authenticated_simulation_restart_and_settings(tmp_path):
    first, manager = client_for(tmp_path)
    with first:
        assert first.get("/api/bots").status_code == 401
        headers = login(first)
        response = first.post("/api/settings/execution", headers=headers, json={"entry_fee_rate": .001, "margin_fraction": .5})
        assert response.status_code == 200
        response = first.post("/api/bots", headers=headers, json=dict(symbol="btcusdt", strategy_name="AdvRSI", timeframe="60", credit=100))
        assert response.status_code == 201, response.text
        bot_id = response.json()["id"]
        bot = manager.get_bot(bot_id)
        assert bot.cfg.execution["entry_fee_rate"] == .001
        bot.df = synthetic_candles(700)
        bot._live_from_ts = bot.df.index[300]
        bot._run_strategy()
        expected = first.get(f"/api/bots/{bot_id}").json()
    second, other = client_for(tmp_path)
    with second:
        login(second)
        actual = second.get(f"/api/bots/{bot_id}").json()
        assert actual["balance"] == expected["balance"]
        assert actual["trades"] == expected["trades"]
        assert actual["status"] == "idle"
        assert second.get("/api/settings/execution").json()["entry_fee_rate"] == .001


def test_two_app_instances_have_separate_registries(tmp_path):
    first, one = client_for(tmp_path / "one")
    second, two = client_for(tmp_path / "two")
    with first, second:
        headers = login(first)
        login(second)
        response = first.post("/api/bots", headers=headers, json=dict(symbol="btcusdt", strategy_name="AdvRSI", timeframe="60", credit=100))
        assert response.status_code == 201
        assert len(first.get("/api/bots").json()) == 1
        assert second.get("/api/bots").json() == []


def test_invalid_execution_is_rejected_without_saving(tmp_path):
    client, manager = client_for(tmp_path)
    with client:
        headers = login(client)
        response = client.post("/api/settings/execution", headers=headers, json={"slippage_bps": 10000})
        assert response.status_code == 400
        assert client.get("/api/settings/execution").json()["slippage_bps"] == 0
