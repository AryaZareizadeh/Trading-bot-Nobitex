import asyncio
import json
import math
import threading
import time
from dataclasses import asdict
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import optimizer
from storage import Store


def _candles(count=1200):
    price = 100 + np.sin(np.arange(count) / 7) * 10
    return pd.DataFrame({"Open": price, "High": price + 1, "Low": price - 1,
                         "Close": price, "Volume": 100.0},
                        index=pd.date_range("2025-01-01", periods=count, freq="h", tz="UTC"))


def _summary(pnl=12.0, win_rate=70.0, trades=40):
    return {"total_pnl": pnl, "win_rate": win_rate, "trade_count": trades}


def _job(**kwargs):
    return optimizer.OptimizerJob("btcusdt", "AdvRSI", "60", 420, 100, 3,
                                  cores=1, method="tpe", n_trials=12, random_seed=42, **kwargs)


def test_method_and_trial_budget_survive_preferences_and_job_snapshot(tmp_path):
    store = Store(tmp_path / "tpe.sqlite3")
    manager = optimizer.OptimizerManager(store)
    defaults = manager.get_config()
    assert defaults["method"] == "de"
    assert defaults["n_trials"] == defaults["default_n_trials"] == 1000
    manager.save_prefs("btcusdt", "AdvRSI", "60", 420, 100, 3, method="tpe", n_trials=37)
    prefs = optimizer.OptimizerManager(store).get_config()
    assert (prefs["method"], prefs["n_trials"]) == ("tpe", 37)
    job = _job()
    manager._attach(job)
    recovered = optimizer.OptimizerManager(store).get_job(job.id).to_dict()
    assert (recovered["method"], recovered["n_trials"]) == ("tpe", 12)


@pytest.mark.parametrize("kwargs", [{"method": "random"}, {"n_trials": 9}, {"n_trials": 100001},
                                    {"n_trials": 12.5}, {"n_trials": True}])
def test_invalid_method_or_trial_budget_is_rejected(kwargs):
    with pytest.raises(ValueError):
        optimizer.OptimizerJob("btcusdt", "AdvRSI", "60", 420, 100, 3, **kwargs)


def test_tpe_evaluates_integer_candidates_exactly_once_and_records_trials(monkeypatch):
    job = _job(custom_bounds={"rsi_length": [20, 23]})
    seen = []
    def evaluate(df, strategy, params, credit, leverage, **kwargs):
        seen.append(asdict(params))
        return _summary(pnl=float(params.rsi_length))
    monkeypatch.setattr(optimizer, "evaluate", evaluate)
    monkeypatch.setattr(job, "_refresh_best_chart", lambda df: None)
    job._run_search(None)
    result = job.to_dict()
    assert len(seen) == result["evaluations"] == result["iteration"] == 12
    assert len(result["trials"]) == len(result["history"]) == 12
    assert all(20 <= params["rsi_length"] <= 23 for params in seen)
    assert all(isinstance(value, int) for params in seen for value in params.values())
    assert result["best_pnl"] == max(params["rsi_length"] for params in seen)
    assert [trial["number"] for trial in result["trials"]] == list(range(12))
    assert all(trial["state"] == "complete" and math.isfinite(trial["score"])
               for trial in result["trials"])
    assert result["current_delta"] is None
    json.dumps(result, allow_nan=False)


def test_tpe_reproducible_and_uses_multivariate_sampler(monkeypatch):
    import optuna
    studies = []
    create_study = optuna.create_study
    def capture_study(**kwargs):
        study = create_study(**kwargs)
        studies.append(study)
        return study
    monkeypatch.setattr(optuna, "create_study", capture_study)
    monkeypatch.setattr(optimizer, "evaluate", lambda df, strategy, params, *args, **kwargs:
                        _summary(pnl=params.rsi_length))
    jobs = [_job(), _job()]
    for job in jobs:
        monkeypatch.setattr(job, "_refresh_best_chart", lambda df: None)
        job._run_search(None)
    assert [t["params"] for t in jobs[0].to_dict()["trials"]] == [t["params"] for t in jobs[1].to_dict()["trials"]]
    assert all(isinstance(study.sampler, optuna.samplers.TPESampler) for study in studies)
    assert all(study.sampler._multivariate and study.sampler._constant_liar for study in studies)
    assert all(len(study.trials) == 12 for study in studies)


@pytest.mark.parametrize("pnl", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_trials_fail_without_contaminating_best_or_persistence(monkeypatch, pnl):
    job = _job()
    monkeypatch.setattr(optimizer, "evaluate", lambda *args, **kwargs: _summary(pnl=pnl))
    monkeypatch.setattr(job, "_refresh_best_chart", lambda df: None)
    with pytest.raises(RuntimeError, match="All 12 TPE trials failed"):
        job._run_search(None)
    result = job.to_dict()
    assert result["evaluations"] == 12
    assert result["best_params"] is None
    assert all(t["state"] == "fail" and t["score"] is None for t in result["trials"])
    json.dumps(result, allow_nan=False)


def test_tpe_balanced_only_publishes_eligible_results(monkeypatch):
    job = _job(objective="balanced", min_win_rate=60, min_closed_trades=30)
    summaries = iter([_summary(10000, 100, 2), _summary(500, 60, 30)] + [_summary(10000, 59, 40)] * 10)
    monkeypatch.setattr(optimizer, "evaluate", lambda *args, **kwargs: next(summaries))
    monkeypatch.setattr(job, "_refresh_best_chart", lambda df: None)
    job._run_search(None)
    assert job.best_pnl == 500
    assert job.best_summary["trade_count"] == 30


def test_tpe_continues_after_candidate_exception(monkeypatch):
    job = _job()
    calls = 0
    def evaluate(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("invalid indicator candidate")
        return _summary()
    monkeypatch.setattr(optimizer, "evaluate", evaluate)
    monkeypatch.setattr(job, "_refresh_best_chart", lambda df: None)
    job._run_search(None)
    assert job.evaluations == calls == 12
    assert job.to_dict()["trials"][0]["state"] == "fail"
    assert job.best_pnl == 12


def test_tpe_run_reports_error_when_every_trial_fails(monkeypatch, tmp_path):
    job = _job()
    store = Store(tmp_path / "failed-tpe.sqlite3")
    manager = optimizer.OptimizerManager(store)
    manager._attach(job)
    calls = 0

    def evaluate(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _summary()  # The default-parameter baseline succeeds.
        raise ValueError("invalid indicator candidate")

    monkeypatch.setattr(optimizer, "_fetch_df", lambda *args: _candles())
    monkeypatch.setattr(optimizer, "evaluate", evaluate)
    monkeypatch.setattr(job, "_refresh_best_chart", lambda df: None)
    asyncio.run(job.run())
    saved = store.get("optimizer_jobs", job.id)
    assert saved["status"] == "error"
    assert "All 12 TPE trials failed" in saved["error"]
    assert saved["evaluations"] == len(saved["trials"]) == 12
    assert saved["best_params"] is None
    assert all(trial["state"] == "fail" for trial in saved["trials"])


def test_tpe_run_with_completed_ineligible_trials_is_done(monkeypatch):
    job = _job(objective="balanced", min_win_rate=60, min_closed_trades=30)
    monkeypatch.setattr(optimizer, "_fetch_df", lambda *args: _candles())
    monkeypatch.setattr(optimizer, "evaluate", lambda *args, **kwargs: _summary(1000, 100, 2))
    monkeypatch.setattr(job, "_refresh_best_chart", lambda df: None)
    asyncio.run(job.run())
    result = job.to_dict()
    assert result["status"] == "done"
    assert result["error"] is None
    assert result["no_eligible_result"]
    assert all(trial["state"] == "complete" for trial in result["trials"])


@pytest.mark.parametrize("cancel_after", [1, 12])
def test_tpe_cancelled_run_with_failed_trials_remains_cancelled(monkeypatch, cancel_after):
    job = _job()
    calls = 0

    def evaluate(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _summary()
        if calls - 1 == cancel_after:
            job.cancel()
        raise ValueError("invalid indicator candidate")

    monkeypatch.setattr(optimizer, "_fetch_df", lambda *args: _candles())
    monkeypatch.setattr(optimizer, "evaluate", evaluate)
    monkeypatch.setattr(job, "_refresh_best_chart", lambda df: None)
    asyncio.run(job.run())
    result = job.to_dict()
    assert result["status"] == "cancelled"
    assert result["error"] is None
    assert result["evaluations"] == cancel_after
    assert all(trial["state"] == "fail" for trial in result["trials"])


def test_tpe_pause_and_cancel_stop_before_next_batch(monkeypatch):
    job = _job()
    job.status = "running"
    entered = threading.Event()
    released = threading.Event()
    def evaluate(*args, **kwargs):
        entered.set()
        assert released.wait(3)
        return _summary()
    monkeypatch.setattr(optimizer, "evaluate", evaluate)
    monkeypatch.setattr(job, "_refresh_best_chart", lambda df: None)
    thread = threading.Thread(target=job._run_search, args=(None,))
    thread.start()
    try:
        assert entered.wait(3)
        assert job.pause()
        released.set()
        deadline = time.monotonic() + 3
        while job.status != "paused" and time.monotonic() < deadline:
            time.sleep(.01)
        assert job.status == "paused"
        assert job.evaluations == 1
        job.cancel()
        thread.join(3)
        assert not thread.is_alive()
        assert job.evaluations == 1
    finally:
        released.set()
        job.cancel()
        thread.join(3)


def test_tpe_spawn_pool_respects_remaining_budget_and_execution_context(monkeypatch):
    job = optimizer.OptimizerJob("btcusdt", "AdvRSI", "60", 420, 100, 3,
                                 cores=3, method="tpe", n_trials=10, random_seed=42,
                                 execution={"entry_fee_rate": .001})
    job.evaluation_start = 400
    batches, contexts = [], []
    class Pool:
        def __init__(self, processes, initializer, initargs):
            assert processes == 3
            initializer(*initargs)
        def map(self, evaluate, vectors):
            batches.append(len(vectors))
            return [evaluate(vector) for vector in vectors]
        def terminate(self):
            pass
        def join(self):
            pass
    def context(name):
        contexts.append(name)
        return SimpleNamespace(Pool=Pool)
    monkeypatch.setattr(optimizer.mp, "get_context", context)
    monkeypatch.setattr(optimizer, "_configured_worker_cap", lambda: 8)
    monkeypatch.setattr(optimizer, "_expand_cpu_affinity", lambda: None)
    seen = []
    def evaluate(df, strategy, params, credit, leverage, **kwargs):
        seen.append((kwargs["start_idx"], kwargs["execution"]["entry_fee_rate"]))
        return _summary()
    monkeypatch.setattr(optimizer, "evaluate", evaluate)
    monkeypatch.setattr(job, "_refresh_best_chart", lambda df: None)
    job._run_search(None)
    assert contexts == ["spawn"]
    assert batches == [3, 3, 3, 1]
    assert seen == [(400, .001)] * 10
    assert job.evaluations == 10


def test_independent_tpe_runs_both_sides_and_persists_budgets(monkeypatch, tmp_path):
    fetches = []
    monkeypatch.setattr(optimizer, "_fetch_df", lambda *args: fetches.append(args) or _candles(args[2] + optimizer.WARMUP_CANDLES))
    monkeypatch.setattr(optimizer.OptimizerJob, "_refresh_best_chart", lambda job, df: None)
    store = Store(tmp_path / "independent-tpe.sqlite3")
    manager = optimizer.OptimizerManager(store)
    async def run():
        job = manager.start_job("btcusdt", "AdvRSIIndependent", "60", 420, 999, 2,
                                cores=1, method="tpe", n_trials=10, random_seed=42,
                                long_credit=120, short_credit=300)
        await job._task
        return job.to_dict()
    result = asyncio.run(run())
    assert result["status"] == "done", result["error"]
    assert len(fetches) == 1
    assert result["method"] == "tpe"
    assert result["n_trials"] == 10
    assert result["iteration"] == result["evaluations"] == 20
    for side, credit in (("long", 120), ("short", 300)):
        child = result["sides"][side]
        assert (child["method"], child["n_trials"], child["credit"]) == ("tpe", 10, credit)
        assert child["evaluations"] == len(child["trials"]) == 10
        assert child["evaluation_start"] == result["evaluation_start"]
    recovered = optimizer.OptimizerManager(store).get_job(result["id"]).to_dict()
    assert recovered["sides"] == json.loads(json.dumps(result["sides"]))


def test_legacy_de_dispatch_retains_generation_progress(monkeypatch):
    job = optimizer.OptimizerJob("btcusdt", "AdvRSI", "60", 420, 100, 3, cores=1)
    def de(df):
        job.iteration = 3
        job.evaluations = 88
    monkeypatch.setattr(job, "_run_de", de)
    job._run_search(None)
    assert job.to_dict()["method"] == "de"
    assert (job.iteration, job.evaluations) == (3, 88)
