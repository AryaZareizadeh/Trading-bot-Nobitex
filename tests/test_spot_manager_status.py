"""Paper readiness and failed-update status use only local synthetic artifacts."""

import joblib
import pytest
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from bot_manager import BotManager
from server import create_app
from spot_paper import SpotPaperService
from spot_portfolio import mark, new_state
from spot_research import RESEARCH_VERSION
from storage import Store


@pytest.mark.parametrize("holdings, cash, prices, liquidation_prices, equity, liquidation", [
    ({"BTC": "0"}, "200", {}, {}, 200, 200),
    ({"BTC": "0.00000000", "ETH": "0.5"}, "150", {"ETH": 100}, {"ETH": 99}, 200, 199.5),
])
def test_zero_holdings_need_no_market_or_liquidation_price(
        holdings, cash, prices, liquidation_prices, equity, liquidation):
    state = new_state(200, ["BTC", "ETH"])
    state["holdings"] = holdings.copy()
    state["cash_usdt"] = cash
    point = mark(state, prices, liquidation_prices=liquidation_prices)
    assert point["complete"] is True
    assert point["equity_usdt"] == equity
    assert point["liquidation_usdt"] == liquidation
    assert point["drawdown_pct"] == 0
    assert state["holdings"] == holdings


def model_package(assets=None, **changes):
    return {"version": RESEARCH_VERSION, "method": "momentum", "models": None,
            "assets": assets or ["BTC"], "run_id": "synthetic-run",
            "trained_at": "2026-10-01T00:00:00+00:00", **changes}


def test_model_readiness_uses_artifact_and_saved_portfolio_assets(tmp_path):
    service = SpotPaperService(Store(tmp_path / "state.db"))
    service.start(["BTC", "ETH"], 200)
    missing = service.model_status()
    assert missing["exists"] is False
    assert missing["ready"] is False
    assert "research" in missing["reason"]

    joblib.dump(model_package(), service.model_path)
    partial = service.model_status()
    assert partial["exists"] is True
    assert partial["ready"] is True
    assert partial["assets"] == ["BTC"]
    assert partial["missing_paper_assets"] == ["ETH"]
    assert partial["version"] == RESEARCH_VERSION
    assert partial["run_id"] == "synthetic-run"

    joblib.dump(model_package(["SOL"]), service.model_path)
    unrelated = service.model_status()
    assert unrelated["ready"] is False
    assert unrelated["missing_paper_assets"] == ["BTC", "ETH"]
    assert "does not cover" in unrelated["reason"]


@pytest.mark.parametrize("package, reason", [
    (model_package(version="old-version"), "version mismatch"),
    (model_package(method="hgb"), "incomplete"),
    ({"version": RESEARCH_VERSION}, "could not be read"),
])
def test_unusable_model_is_not_reported_ready(tmp_path, package, reason):
    service = SpotPaperService(Store(tmp_path / "state.db"))
    joblib.dump(package, service.model_path)
    status = service.model_status()
    assert status["exists"] is True
    assert status["ready"] is False
    assert reason in status["reason"]


def test_corrupt_model_is_actionable_without_breaking_status(tmp_path):
    service = SpotPaperService(Store(tmp_path / "state.db"))
    service.model_path.write_bytes(b"not a valid joblib artifact")
    assert service.model_status()["ready"] is False
    assert "research again" in service.model_status()["reason"]


def test_model_readiness_updates_for_portfolio_and_artifact_changes(tmp_path):
    service = SpotPaperService(Store(tmp_path / "state.db"))
    joblib.dump(model_package(), service.model_path)
    assert service.model_status()["ready"] is True
    service.start(["ETH"], 200)
    assert service.model_status()["ready"] is False
    assert service.model_status()["missing_paper_assets"] == ["ETH"]

    replacement = service.model_path.with_suffix(".new")
    joblib.dump(model_package(["BTC", "ETH"]), replacement)
    replacement.replace(service.model_path)
    assert service.model_status()["ready"] is True
    assert service.model_status()["missing_paper_assets"] == []

    service.model_path.unlink()
    assert service.model_status()["exists"] is False
    assert service.model_status()["ready"] is False


def test_manager_api_exposes_model_readiness_and_failed_tick_reason(tmp_path):
    store = Store(tmp_path / "state.db")
    manager = BotManager(store, tmp_path)
    app = create_app(manager=manager, offline=True, security=False)
    with TestClient(app) as client:
        assert client.post("/api/portfolio-manager/paper/start", json={"assets": ["BTC"]}).status_code == 200
        tick = client.post("/api/portfolio-manager/paper/tick")
        assert tick.status_code == 200
        assert tick.json()["ok"] is False
        manager_status = client.get("/api/portfolio-manager").json()
        assert manager_status["paper_error"] == tick.json()["reason"]
        assert manager_status["model_status"]["ready"] is False

        service = SpotPaperService(store)
        joblib.dump(model_package(), service.model_path)
        model_status = client.get("/api/portfolio-manager").json()["model_status"]
        assert model_status["ready"] is True
        assert model_status["assets"] == ["BTC"]


def test_successful_tick_clears_old_failure_and_exception_is_saved(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    app = create_app(manager=BotManager(store, tmp_path), offline=True, security=False)
    with TestClient(app) as client:
        monkeypatch.setattr(SpotPaperService, "tick", lambda self: {"ok": False, "reason": "Latest signal candle is stale"})
        client.post("/api/portfolio-manager/paper/tick")
        assert client.get("/api/portfolio-manager").json()["paper_error"] == "Latest signal candle is stale"

        monkeypatch.setattr(SpotPaperService, "tick", lambda self: {"ok": True, "reason": "Hourly valuation recorded"})
        assert client.post("/api/portfolio-manager/paper/tick").json()["ok"] is True
        assert client.get("/api/portfolio-manager").json()["paper_error"] is None

        def failing_tick(self):
            raise ValueError("Market precision unavailable")

        monkeypatch.setattr(SpotPaperService, "tick", failing_tick)
        assert client.post("/api/portfolio-manager/paper/tick").status_code == 503
        assert client.get("/api/portfolio-manager").json()["paper_error"] == "Market precision unavailable"


def test_reset_archives_only_paper_and_unlocks_budget_and_assets(tmp_path):
    store = Store(tmp_path / "state.db")
    report = {"run_id": "synthetic-run", "selected": "momentum"}
    store.put("spot_manager", "research", report)
    store.put("spot_manager", "experiments", [{"run_id": "synthetic-run"}])
    store.put("spot_experiments", "synthetic-run", report)
    store.put("portfolio_settings", "targets", {"BTC": 0.2})
    store.put("exchange", "wallets", {"USDT": 100})
    store.put("spot_manager", "research_status", {"status": "complete"})
    joblib.dump(model_package(), tmp_path / "spot_research_model.joblib")
    model_bytes = (tmp_path / "spot_research_model.joblib").read_bytes()
    app = create_app(manager=BotManager(store, tmp_path), offline=True, security=False)
    with TestClient(app) as client:
        old = client.post("/api/portfolio-manager/paper/start", json={"assets": ["BTC"], "budget_usdt": 200}).json()["paper"]
        old["holdings"] = {"BTC": "0.001"}
        old["fills"] = [{"asset": "BTC", "quantity": "0.001"}]
        store.put("spot_manager", "paper", old)
        store.put("spot_manager", "paper_error", "Latest signal candle is stale")
        reset = client.post("/api/portfolio-manager/paper/reset")
        assert reset.status_code == 200
        body = reset.json()
        assert body["ok"] is True
        assert body["paper"] is None
        assert body["paper_error"] is None
        assert body["research_status"] == {"status": "complete"}
        backup = store.get("spot_paper_backups", body["backup_id"])
        assert backup["paper"] == old
        assert backup["paper_error"] == "Latest signal candle is stale"
        assert backup["reset_at"]
        assert store.get("spot_manager", "paper") is None
        assert store.get("spot_manager", "paper_error") is None
        assert store.get("spot_manager", "research") == report
        assert store.get("spot_manager", "experiments") == [{"run_id": "synthetic-run"}]
        assert store.get("spot_experiments", "synthetic-run") == report
        assert store.get("portfolio_settings", "targets") == {"BTC": 0.2}
        assert store.get("exchange", "wallets") == {"USDT": 100}
        assert (tmp_path / "spot_research_model.joblib").read_bytes() == model_bytes
        restarted = client.post("/api/portfolio-manager/paper/start", json={"assets": ["ETH"], "budget_usdt": 350})
        assert restarted.status_code == 200
        assert restarted.json()["paper"]["assets"] == ["ETH"]
        assert float(restarted.json()["paper"]["cash_usdt"]) == 350
        second = client.post("/api/portfolio-manager/paper/reset").json()
        assert second["backup_id"] != body["backup_id"]
        assert len(store.list("spot_paper_backups")) == 2
        assert client.post("/api/portfolio-manager/paper/reset").json()["backup_id"] is None
        assert len(store.list("spot_paper_backups")) == 2


def test_failed_reset_rolls_back_archive_and_keeps_original_session(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    app = create_app(manager=BotManager(store, tmp_path), offline=True, security=False)
    with TestClient(app) as client:
        old = client.post("/api/portfolio-manager/paper/start", json={"assets": ["BTC"]}).json()["paper"]
        store.put("spot_manager", "paper_error", "Existing update failure")
        real_write = Store.write

        def failing_write(conn, namespace, key, value):
            if namespace == "spot_manager" and key == "paper" and value is None:
                raise OSError("Synthetic reset storage failure")
            return real_write(conn, namespace, key, value)

        monkeypatch.setattr(Store, "write", staticmethod(failing_write))
        response = client.post("/api/portfolio-manager/paper/reset")
        assert response.status_code == 503
        assert "storage failure" in response.json()["error"]
        assert store.get("spot_manager", "paper") == old
        assert store.get("spot_manager", "paper_error") == "Existing update failure"
        assert store.list("spot_paper_backups") == {}


@pytest.mark.parametrize("endpoint", ["/api/portfolio-manager", "/api/portfolio-manager/paper/reset"])
def test_stale_running_research_recovers_after_restart(tmp_path, endpoint):
    store = Store(tmp_path / "state.db")
    started_at = "2026-09-30T00:00:00+00:00"
    store.put("spot_manager", "research_status", {"status": "running", "started_at": started_at})
    app = create_app(manager=BotManager(store, tmp_path), offline=True, security=False)
    with TestClient(app) as client:
        response = client.get(endpoint) if endpoint == "/api/portfolio-manager" else client.post(endpoint)
        status = response.json()["research_status"]
        assert status["status"] == "failed"
        assert status["started_at"] == started_at
        assert "interrupted" in status["error"]
        assert "Run research again" in status["error"]
        assert store.get("spot_manager", "research_status") == status


def test_reset_preserves_real_running_research(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    entered, release = threading.Event(), threading.Event()

    def running_research(self, *args):
        entered.set()
        assert release.wait(5), "Test did not release synthetic research"

    monkeypatch.setattr(SpotPaperService, "research", running_research)
    app = create_app(manager=BotManager(store, tmp_path), offline=True, security=False)
    with TestClient(app) as client:
        try:
            assert client.post("/api/portfolio-manager/research", json={"assets": ["BTC"], "days": 90}).status_code == 200
            assert entered.wait(2)
            assert client.get("/api/portfolio-manager").json()["research_status"]["status"] == "running"
            response = client.post("/api/portfolio-manager/paper/reset")
            assert response.status_code == 200
            assert response.json()["research_status"]["status"] == "running"
            assert client.post("/api/portfolio-manager/research", json={"assets": ["BTC"], "days": 90}).status_code == 409
            assert release.is_set() is False
        finally:
            release.set()
    assert store.get("spot_manager", "research_status")["status"] == "complete"


@pytest.mark.parametrize("fail_tick", [False, True])
def test_reset_during_blocked_tick_does_not_wait_or_restore_old_state(tmp_path, monkeypatch, fail_tick):
    boundary = datetime(2026, 9, 27, 17, tzinfo=timezone.utc)
    now = boundary.timestamp() + 60
    monkeypatch.setattr("spot_paper.time.time", lambda: now)
    bars = []
    for i in range(120):
        opened = boundary - timedelta(hours=120 - i)
        price = 100 + i
        bars.append({"open_time": opened.isoformat(), "available_at": (opened + timedelta(hours=1)).isoformat(),
                     "open": price, "high": price + 1, "low": price - 1, "close": price, "volume_base": 100})
    entered, release = threading.Event(), threading.Event()

    def blocked_books(self):
        entered.set()
        assert release.wait(5), "Test did not release synthetic market data"
        if fail_tick:
            raise ValueError("Old market request failed")
        return {"BTC": {"lastUpdate": (now - 5) * 1000, "asks": [["220", "10"]], "bids": [["219", "10"]]}}

    monkeypatch.setattr("spot_paper.NobitexPublicSpot.books", blocked_books)
    monkeypatch.setattr("spot_paper.NobitexPublicSpot.rules", lambda self: {"min_notional": "0.5", "quantity_steps": {"BTC": "0.000001"}})
    monkeypatch.setattr("spot_paper.PublicSpotData.fetch", lambda *args, **kwargs: {"valid": len(bars), "candles": bars})
    store = Store(tmp_path / "state.db")
    joblib.dump(model_package(), tmp_path / "spot_research_model.joblib")
    app = create_app(manager=BotManager(store, tmp_path), offline=True, security=False)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as executor:
        client.post("/api/portfolio-manager/paper/start", json={"assets": ["BTC"]})
        old = store.get("spot_manager", "paper")
        tick = executor.submit(client.post, "/api/portfolio-manager/paper/tick")
        try:
            assert entered.wait(2)
            reset = executor.submit(client.post, "/api/portfolio-manager/paper/reset").result(timeout=1)
            assert reset.status_code == 200
            assert reset.json()["paper"] is None
            assert store.get("spot_paper_backups", reset.json()["backup_id"])["paper"] == old
            new = client.post("/api/portfolio-manager/paper/start", json={"assets": ["ETH"], "budget_usdt": 350}).json()["paper"]
        finally:
            release.set()
        result = tick.result(timeout=2)
        assert result.status_code == (503 if fail_tick else 200)
        if not fail_tick:
            assert result.json()["discarded"] is True
        assert store.get("spot_manager", "paper") == new
        assert store.get("spot_manager", "paper_error") is None
