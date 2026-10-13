"""DE variants reach SciPy and survive API, paired runs, and saved history."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import optimizer
from storage import Store


DE_VARIANTS = (
    "best1bin", "best1exp", "rand1bin", "rand1exp", "rand2bin", "rand2exp",
    "randtobest1bin", "randtobest1exp", "currenttobest1bin", "currenttobest1exp",
    "best2bin", "best2exp",
)


def _job(**settings):
    return optimizer.OptimizerJob("btcusdt", "AdvRSI", "60", 420, 100, 3,
                                  cores=1, random_seed=42, **settings)


def _app(tmp_path):
    from bot_manager import BotManager
    from server import create_app

    store = Store(tmp_path / "de-api.sqlite3")
    return create_app(manager=BotManager(store, tmp_path), offline=True, security=False), store


def _payload(**settings):
    return {"symbol": "btcusdt", "strategy": "AdvRSI", "timeframe": "60", **settings}


def test_config_advertises_all_de_variants_and_preserves_default(tmp_path):
    config = optimizer.OptimizerManager(Store(tmp_path / "de.sqlite3")).get_config()
    assert config.get("de_strategy") == config.get("default_de_strategy") == "best2bin"
    options = config.get("available_de_strategies", [])
    assert [option["value"] for option in options] == list(DE_VARIANTS)
    assert all(isinstance(option.get("label"), str) and option["label"] for option in options)
    assert _job().to_dict().get("de_strategy") == "best2bin"


@pytest.mark.parametrize("variant", DE_VARIANTS)
def test_selected_variant_survives_saved_preferences(variant, tmp_path):
    store = Store(tmp_path / "de.sqlite3")
    manager = optimizer.OptimizerManager(store)
    manager.save_prefs("btcusdt", "AdvRSI", "60", 420, 100, 3, de_strategy=variant)
    assert optimizer.OptimizerManager(Store(store.path)).get_config()["de_strategy"] == variant


@pytest.mark.parametrize("invalid", ["unknown", "BEST1BIN", "", None, 7, []])
@pytest.mark.parametrize("entry", ["job", "prefs", "manager"])
def test_invalid_de_variant_is_rejected_before_persistence(entry, invalid, tmp_path):
    store = Store(tmp_path / "de.sqlite3")
    manager = optimizer.OptimizerManager(store)
    with pytest.raises(ValueError, match="de_strategy"):
        if entry == "job":
            _job(de_strategy=invalid)
        elif entry == "prefs":
            manager.save_prefs("btcusdt", "AdvRSI", "60", 420, 100, 3, de_strategy=invalid)
        else:
            manager.start_job("btcusdt", "AdvRSI", "60", 420, 100, 3, de_strategy=invalid)
    assert store.list("optimizer_jobs") == {}
    assert store.get("prefs", "optimizer") is None


@pytest.mark.parametrize("variant", DE_VARIANTS)
@pytest.mark.parametrize("parallel", [False, True])
def test_serial_and_parallel_search_pass_selected_variant_to_scipy(variant, parallel, monkeypatch):
    job = _job(de_strategy=variant)
    seen = []
    scipy_de = optimizer.differential_evolution

    def search(objective, **kwargs):
        seen.append(kwargs["strategy"])
        return scipy_de(objective, **kwargs)

    settings = job._de_kwargs
    monkeypatch.setattr(job, "_de_kwargs", lambda *args: {
        **settings(*args), "maxiter": 1, "popsize": 1, "init": "random",
    })
    monkeypatch.setattr(optimizer, "differential_evolution", search)
    monkeypatch.setattr(optimizer, "evaluate", lambda df, strategy, params, *args, **kwargs: {
        "total_pnl": float(params.rsi_length), "win_rate": 70.0, "trade_count": 40,
    })
    monkeypatch.setattr(job, "_refresh_best_chart", lambda df: None)

    if parallel:
        # Keep SciPy's parallel map contract and real worker objective; avoid
        # launching 24 independent Windows spawn pools for this forwarding check.
        class InlinePool:
            def __init__(self, processes, initializer, initargs):
                initializer(*initargs)

            def map(self, function, vectors):
                return [function(vector) for vector in vectors]

            def terminate(self):
                pass

            def join(self):
                pass

        monkeypatch.setattr(optimizer.mp, "get_context", lambda method: SimpleNamespace(Pool=InlinePool))
        job._run_de_parallel(None, 2)
    else:
        job._run_de_serial(None)
    assert seen == [variant]
    assert job.best_params is not None
    assert job.best_pnl == job.best_params["rsi_length"]


@pytest.mark.parametrize("method", ["de", "tpe"])
def test_paired_run_and_recovery_preserve_variant_on_both_sides(method, monkeypatch, tmp_path):
    async def finish(job):
        job.status = "done"
        job._persist()

    monkeypatch.setattr(optimizer.IndependentOptimizerJob, "run", finish)
    store = Store(tmp_path / "paired.sqlite3")
    manager = optimizer.OptimizerManager(store)

    async def start():
        job = manager.start_job("btcusdt", "AdvRSIIndependent", "60", 420, 100, 3,
                                method=method, de_strategy="currenttobest1exp")
        await job._task
        return job

    job = asyncio.run(start())
    for snapshot in (job.to_dict(), optimizer.OptimizerManager(Store(store.path)).get_job(job.id).to_dict()):
        assert snapshot["de_strategy"] == "currenttobest1exp"
        assert {side["de_strategy"] for side in snapshot["sides"].values()} == {"currenttobest1exp"}


def test_legacy_snapshots_and_preferences_default_variant_without_overwriting_saved_sides(tmp_path):
    store = Store(tmp_path / "legacy.sqlite3")
    store.put("prefs", "optimizer", {"method": "de", "symbol": "btcusdt"})
    legacy = {"id": "paired", "created_at": 1, "status": "done", "sides": {
        "long": {"id": "long"}, "short": {"id": "short", "de_strategy": "best1exp"},
    }}
    store.put("optimizer_jobs", "paired", legacy)
    manager = optimizer.OptimizerManager(store)
    saved = manager.get_job("paired").to_dict()
    assert saved.get("de_strategy") == saved["sides"]["long"].get("de_strategy") == "best2bin"
    assert saved["sides"]["short"]["de_strategy"] == "best1exp"
    assert manager.get_config()["de_strategy"] == "best2bin"


@pytest.mark.parametrize("route", ["prefs", "run", "tester/run"])
@pytest.mark.parametrize("invalid", ["unknown", "BEST1BIN", "", None, 7, []])
def test_api_rejects_invalid_variant_without_saving_or_starting(route, invalid, tmp_path):
    app, store = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post(f"/api/optimizer/{route}", json=_payload(de_strategy=invalid))
        assert response.status_code == 422, response.text
    assert store.list("optimizer_jobs") == {}
    assert store.get("prefs", "optimizer") is None


@pytest.mark.parametrize("variant", DE_VARIANTS)
def test_api_preferences_accept_every_builtin_de_variant(variant, tmp_path):
    app, store = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/optimizer/prefs", json=_payload(de_strategy=variant))
        assert response.status_code == 200, response.text
        assert client.get("/api/optimizer/config").json().get("de_strategy") == variant
    assert store.get("prefs", "optimizer").get("de_strategy") == variant


@pytest.mark.parametrize("route", ["run", "tester/run"])
@pytest.mark.parametrize("method", ["de", "tpe"])
@pytest.mark.parametrize("settings, expected", [({}, "best2bin"), ({"de_strategy": "rand2exp"}, "rand2exp")])
def test_api_run_records_default_or_selected_variant(route, method, settings, expected, monkeypatch, tmp_path):
    async def finish(job):
        job.status = "done"
        job._persist()

    monkeypatch.setattr(optimizer.OptimizerJob, "run", finish)
    app, store = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post(f"/api/optimizer/{route}", json=_payload(method=method, **settings))
        assert response.status_code == 200, response.text
        saved = client.get(f"/api/optimizer/{route}/{response.json()['job_id']}").json()
        assert saved.get("de_strategy") == expected
        assert saved["method"] == method
        assert client.get("/api/optimizer/jobs").json()[0].get("de_strategy") == expected
        if route == "run":
            assert client.get("/api/optimizer/config").json().get("de_strategy") == expected
    assert store.get("optimizer_jobs", response.json()["job_id"]).get("de_strategy") == expected


def test_tpe_candidates_are_unaffected_by_de_variant(monkeypatch):
    monkeypatch.setattr(optimizer, "evaluate", lambda df, strategy, params, *args, **kwargs: {
        "total_pnl": float(params.rsi_length), "win_rate": 70.0, "trade_count": 40,
    })
    jobs = [_job(method="tpe", n_trials=10, de_strategy=variant) for variant in ("best2bin", "rand1exp")]
    for job in jobs:
        monkeypatch.setattr(job, "_refresh_best_chart", lambda df: None)
        job._run_search(None)
    assert [trial["params"] for trial in jobs[0].trials] == [trial["params"] for trial in jobs[1].trials]
    assert all(job.evaluations == 10 for job in jobs)
