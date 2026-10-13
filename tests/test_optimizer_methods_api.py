"""Optimizer requests and saved-run deletion use isolated account storage."""
from concurrent.futures import Future

import pytest
from fastapi.testclient import TestClient


def _app(tmp_path, *, security=False):
    from bot_manager import BotManager
    from server import create_app
    from storage import Store

    store = Store(tmp_path / "optimizer-api.sqlite3")
    manager = BotManager(store, tmp_path)
    return create_app(manager=manager, offline=True, security=security), store


def _snapshot(job_id="saved-run", status="done", **extra):
    return {
        "id": job_id, "created_at": "2026-10-01T12:00:00+00:00", "status": status,
        "symbol": "btcusdt", "strategy": "AdvRSI", "timeframe": "60",
        "train_pct": None, **extra,
    }


def _run_payload(**extra):
    return {"symbol": "btcusdt", "strategy": "AdvRSI", "timeframe": "60", **extra}


@pytest.mark.parametrize("route", ["prefs", "run", "tester/run"])
@pytest.mark.parametrize("invalid", [
    {"method": "random"}, {"method": "TPE"},
    {"n_trials": 9}, {"n_trials": 100001}, {"n_trials": 10.5},
])
def test_optimizer_routes_reject_invalid_search_settings(route, invalid, tmp_path):
    app, store = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post(f"/api/optimizer/{route}", json=_run_payload(**invalid))
        assert response.status_code == 422, response.text
    assert store.list("optimizer_jobs") == {}
    assert store.get("prefs", "optimizer") is None


@pytest.mark.parametrize("route", ["run", "tester/run"])
@pytest.mark.parametrize("settings, expected", [
    ({}, {"method": "de", "n_trials": 1000}),
    ({"method": "tpe", "n_trials": 37}, {"method": "tpe", "n_trials": 37}),
])
def test_optimizer_start_preserves_method_and_trial_budget(route, settings, expected, tmp_path, monkeypatch):
    from optimizer import OptimizerJob

    async def complete_without_market_request(job, *args, **kwargs):
        job.status = "done"
        job._persist()

    # The search worker depends on market history; exercise request parsing,
    # manager creation, snapshots and preference persistence with that worker isolated.
    monkeypatch.setattr(OptimizerJob, "run", complete_without_market_request)
    app, store = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post(f"/api/optimizer/{route}", json=_run_payload(**settings))
        assert response.status_code == 200, response.text
        saved = client.get(f"/api/optimizer/{route}/{response.json()['job_id']}").json()
        assert {key: saved.get(key) for key in expected} == expected
        assert saved["evaluation_mode"] == ("in_sample" if route == "run" else "chronological")
        assert saved["train_pct"] == (None if route == "run" else 70.0)
        if route == "run":
            prefs = client.get("/api/optimizer/config").json()
            assert {key: prefs.get(key) for key in expected} == expected
            assert prefs["evaluation_mode"] == "in_sample"
    record = store.get("optimizer_jobs", response.json()["job_id"])
    assert {key: record.get(key) for key in expected} == expected


def test_saved_optimizer_method_preferences_survive_restart(tmp_path):
    from optimizer import OptimizerManager
    from storage import Store

    app, store = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/optimizer/prefs", json=_run_payload(method="tpe", n_trials=100000))
        assert response.status_code == 200, response.text
    recovered = OptimizerManager(Store(store.path)).get_config()
    assert recovered["method"] == "tpe"
    assert recovered["n_trials"] == 100000


@pytest.mark.parametrize("paired", [False, True])
def test_optimizer_job_list_is_compact_and_detail_routes_keep_full_saved_results(paired, tmp_path):
    app, store = _app(tmp_path)
    trades = [{"entry_price": 100, "exit_price": 110, "pnl": 10}]
    chart = {"candles": [{"time": 1, "open": 100, "close": 110}], "indicators": {"rsi": [55]}}
    snapshot = _snapshot(
        "rich-tpe", method="tpe", n_trials=100000, maxiter=30, de_strategy="rand1exp",
        evaluation_mode="chronological",
        strategy_label="Advanced RSI", train_pct=70, credit=200, objective="profit",
        evaluations=230, paired=paired, combined_pnl=12.5 if paired else None, best_pnl=12.5,
        best_summary={"total_pnl": 12.5, "trade_count": 2, "win_rate": 50, "trades": trades},
        test_summary={"total_pnl": 3, "trade_count": 1, "trades": trades},
        trials=[{"number": 0, "params": {"rsi_length": 14}, "summary": {"total_pnl": 12.5}}],
        history=[{"iteration": 1, "pnl": 12.5}],
        best_params={"rsi_length": 14}, params=[{"name": "rsi_length", "best": 14}],
        best_trades=trades, test_trades=trades, chart_data=chart, test_chart_data=chart,
        execution={"fee_bps": 10}, custom_bounds={"rsi_length": [2, 100]}, baseline=None,
        test_baseline={"total_pnl": 1}, search_bounds={"rsi_length": [2, 100]},
        sides={side: _snapshot(side, method="tpe", n_trials=100000, de_strategy="rand1exp",
                               evaluation_mode="chronological",
                               trials=[{"number": 0}], chart_data=chart, best_trades=trades)
               for side in ("long", "short")} if paired else {},
    )
    store.put("optimizer_jobs", snapshot["id"], snapshot)
    with TestClient(app) as client:
        response = client.get("/api/optimizer/jobs")
        assert response.status_code == 200, response.text
        assert response.json() == [{
            key: snapshot[key] for key in (
                "id", "created_at", "status", "symbol", "strategy", "timeframe", "train_pct",
                "method", "n_trials", "de_strategy", "maxiter", "strategy_label", "credit", "objective",
                "evaluations", "paired", "combined_pnl", "best_pnl", "evaluation_mode",
            )
        } | {
            "best_summary": {"total_pnl": 12.5, "trade_count": 2, "win_rate": 50},
            "test_summary": {"total_pnl": 3, "trade_count": 1},
        }]
        for route in ("run", "tester/run"):
            detail = client.get(f"/api/optimizer/{route}/{snapshot['id']}")
            assert detail.status_code == 200, detail.text
            assert detail.json() == snapshot
        assert app.state.optimizer_manager.list_jobs() == [snapshot]
    assert store.get("optimizer_jobs", snapshot["id"]) == snapshot


@pytest.mark.parametrize("status", ["done", "cancelled", "error", "interrupted"])
def test_delete_terminal_optimizer_run_removes_saved_record_after_reload(status, tmp_path):
    from optimizer import OptimizerManager
    from storage import Store

    app, store = _app(tmp_path)
    store.put("optimizer_jobs", "saved-run", _snapshot(status=status))
    with TestClient(app) as client:
        response = client.delete("/api/optimizer/jobs/saved-run")
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True}
        assert client.get("/api/optimizer/jobs").json() == []
        assert client.get("/api/optimizer/run/saved-run").status_code == 404
        assert client.delete("/api/optimizer/jobs/saved-run").status_code == 404
    assert store.get("optimizer_jobs", "saved-run") is None
    assert OptimizerManager(Store(store.path)).list_jobs() == []


@pytest.mark.parametrize("status", ["pending", "running", "pausing", "paused", "done"])
def test_delete_rejects_active_optimizer_status_or_unfinished_worker(status, tmp_path):
    from optimizer import OptimizerJob

    app, store = _app(tmp_path)
    job = OptimizerJob("btcusdt", "AdvRSI", "60", 420, 100, 3)
    job.status = status
    job._task = Future() if status == "done" else None
    app.state.optimizer_manager._attach(job)
    # Keep this live job in memory; a real restart would mark unfinished jobs interrupted.
    client = TestClient(app)
    response = client.delete(f"/api/optimizer/jobs/{job.id}")
    assert response.status_code == 409, response.text
    assert store.get("optimizer_jobs", job.id)["status"] == status
    if job._task:
        job._task.set_result(None)


def test_delete_paired_run_removes_nested_results_and_preserves_bots_and_models(tmp_path):
    app, store = _app(tmp_path)
    store.put("optimizer_jobs", "paired-run", _snapshot(
        "paired-run", paired=True, sides={
            "long": _snapshot("long-child"), "short": _snapshot("short-child"),
        }))
    store.put("optimizer_jobs", "other-run", _snapshot("other-run"))
    store.put("bots", "created-bot", {"name": "keep this bot"})
    store.put("model_trainer_jobs", "trained-model", {"artifact": "keep this model"})
    # No lifespan: the independent bot/model marker records are deliberately
    # minimal because deletion should never read their unrelated namespaces.
    client = TestClient(app)
    response = client.delete("/api/optimizer/jobs/paired-run")
    assert response.status_code == 200, response.text
    assert list(store.list("optimizer_jobs")) == ["other-run"]
    assert store.get("bots", "created-bot") == {"name": "keep this bot"}
    assert store.get("model_trainer_jobs", "trained-model") == {"artifact": "keep this model"}


def test_delete_failure_keeps_run_available(tmp_path, monkeypatch):
    app, store = _app(tmp_path)
    store.put("optimizer_jobs", "saved-run", _snapshot())
    app.state.optimizer_manager.load()

    def disk_failure(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "delete", disk_failure)
    with pytest.raises(OSError, match="disk unavailable"):
        app.state.optimizer_manager.delete_job("saved-run")
    assert app.state.optimizer_manager.get_job("saved-run") is not None
    assert store.get("optimizer_jobs", "saved-run") is not None


def test_recovered_legacy_optimizer_snapshots_default_to_de_including_paired_sides(tmp_path):
    from optimizer import OptimizerManager
    from storage import Store

    app, store = _app(tmp_path)
    store.put("optimizer_jobs", "legacy", _snapshot("legacy", train_pct=70))
    store.put("optimizer_jobs", "paired", _snapshot("paired", paired=True, sides={
        "long": _snapshot("long"), "short": _snapshot("short"),
    }))
    manager = OptimizerManager(Store(store.path))
    for saved in manager.list_jobs():
        assert saved["method"] == "de"
        assert saved["n_trials"] == 1000
        for side in saved.get("sides", {}).values():
            assert side["method"] == "de"
            assert side["n_trials"] == 1000
    client = TestClient(app)
    assert client.get("/api/optimizer/tester/run/legacy").json()["train_pct"] == 70


def test_delete_optimizer_run_requires_authentication_and_csrf(tmp_path):
    from auth import AuthState, hash_password

    app, store = _app(tmp_path, security=True)
    app.state.auth = AuthState(password_hash=hash_password("correct horse battery staple"),
                               allowed_hosts={"testserver"})
    store.put("optimizer_jobs", "saved-run", _snapshot())
    with TestClient(app) as client:
        assert client.delete("/api/optimizer/jobs/saved-run").status_code == 401
        login = client.post("/api/auth/login", json={"password": "correct horse battery staple"})
        assert login.status_code == 200
        assert client.delete("/api/optimizer/jobs/saved-run").status_code == 403
        assert store.get("optimizer_jobs", "saved-run") is not None
        response = client.delete("/api/optimizer/jobs/saved-run",
                                 headers={"X-CSRF-Token": login.json()["csrf_token"]})
        assert response.status_code == 200, response.text


def test_optimizer_deletion_is_scoped_to_authenticated_account(tmp_path, monkeypatch):
    from multiuser import create_multiuser_app
    from storage import Store

    monkeypatch.setenv("TRADER_INVITE_CODE", "invite-secret-for-tests")
    monkeypatch.setenv("TRADER_MASTER_KEY", "T8uO8gnZoHUruIXYMHau_cCn-3mV6D0bMLDRLgaCSf8=")
    app = create_multiuser_app(data_root=tmp_path, offline=True, allowed_hosts={"testserver"},
                               send_code=lambda *args: None)
    users = []
    for name in ("alice", "bob"):
        user = app.state.accounts.register(name, f"{name}@example.test", f"{name} password long enough",
                                           "invite", "invite")
        code = app.state.accounts.issue_verification(user["id"])
        assert app.state.accounts.verify_email(user["email"], code)
        users.append(user)
    alice_store = Store(tmp_path / "users" / users[0]["id"] / "trader.sqlite3")
    alice_store.put("optimizer_jobs", "alice-run", _snapshot("alice-run"))
    with TestClient(app) as client:
        bob = client.post("/api/auth/login", json={"username": "bob", "password": "bob password long enough"})
        assert bob.status_code == 200
        response = client.delete("/api/optimizer/jobs/alice-run",
                                 headers={"X-CSRF-Token": bob.json()["csrf_token"]})
        assert response.status_code == 404, response.text
        assert alice_store.get("optimizer_jobs", "alice-run") is not None
        alice = client.post("/api/auth/login", json={"username": "alice", "password": "alice password long enough"})
        assert alice.status_code == 200
        response = client.delete("/api/optimizer/jobs/alice-run",
                                 headers={"X-CSRF-Token": alice.json()["csrf_token"]})
        assert response.status_code == 200, response.text
    assert alice_store.get("optimizer_jobs", "alice-run") is None
