from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import json
import pytest
from storage import Store


def test_status_bounds_audit_history_and_redacts_error_values(tmp_path):
    from runtime_status import runtime_status
    store = Store(tmp_path / "trader.sqlite3")
    for i in range(120):
        store.put("audit_events", i, {"bot_id": "b", "time": f"2026-10-02T00:{i:03d}",
                                     "event": "error", "detail": "token=secret-value"})
    observed = datetime.now(timezone.utc) - timedelta(seconds=120)
    bot = SimpleNamespace(id="b", cfg=SimpleNamespace(symbol="btcusdt", timeframe="60", mode="real"),
                          status=SimpleNamespace(value="error"), error="Authorization: Token secret-value",
                          last_updated=observed, _last_processed_ts=observed,
                          unresolved_intents=lambda: [{"state": "unknown"}], execution_bots=lambda: [bot])
    manager = SimpleNamespace(store=store, get_all=lambda: [bot])
    result = runtime_status(manager, {"last_synced": observed.isoformat(), "error": "password=secret-value"}, [])
    assert len(result["audit"]) == 100
    assert result["bots"][0]["unresolved_intents"] == 1
    assert result["bots"][0]["candle_age_seconds"] >= 120
    assert result["exchange"]["age_seconds"] >= 120
    assert "secret-value" not in json.dumps(result)


def test_status_is_authenticated_and_liveness_has_no_private_state(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from bot_manager import BotManager
    from server import create_app
    from auth import hash_password
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", hash_password("test-password-long-enough"))
    with TestClient(create_app(manager=BotManager(store=Store(tmp_path / "trader.sqlite3")), offline=True),
                    base_url="http://localhost") as client:
        assert client.get("/healthz").json() == {"ok": True}
        assert client.get("/api/runtime/status").status_code == 401
        login = client.post("/api/auth/login", json={"password": "test-password-long-enough"})
        assert login.status_code == 200
        status = client.get("/api/runtime/status")
        assert status.status_code == 200
        assert "queues" in status.json() and "audit" in status.json()
