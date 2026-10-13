"""Selection metrics must never masquerade as the chart's training backtest."""
import copy
from dataclasses import asdict
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import optimizer
from storage import Store


def chart_job():
    x = np.arange(1000)
    prices = 100 + 20 * np.sin(x / 9) + 6 * np.sin(x / 3)
    df = pd.DataFrame({"Open": prices, "High": prices + 1, "Low": prices - 1, "Close": prices},
                      index=pd.date_range("2025-01-01", periods=1000, freq="4h", tz="UTC"))
    df.attrs["optimizer_validation_folds"] = [(700, 800), (800, 900), (900, 1000)]
    params = optimizer.PARAMS_CLASS["AdvRSILong"](
        rsi_length=7, stoch_length=14, k_smooth=4, d_smooth=52,
        rsi_fast_length=48, rsi_ma_length=5, exit_oversold=13,
        exit_overbought=75, entry_wait_candles=1, oversold=32)
    job = optimizer.OptimizerJob("zecusdt", "AdvRSILong", "240", 600, 10, 5,
                                 cores=1, random_seed=42, evaluation_mode="chronological")
    job.evaluation_start = 400
    job.best_params = asdict(params)
    job.best_summary = optimizer.evaluate_selection(df, job.strategy, params, 10, 5, start_idx=400)
    job.best_pnl = job.best_summary["total_pnl"]
    job.validation_summary = copy.deepcopy(job.best_summary)
    job.evaluation_metadata = {"version": "optimizer-validation-v1"}
    return job, df


def test_chart_refresh_preserves_selection_and_publishes_matching_training_metrics():
    job, df = chart_job()
    selection = copy.deepcopy(job.best_summary)
    assert selection["trade_count"] == 3
    job._refresh_best_chart(df)
    result = job.to_dict()
    assert result["best_summary"] == selection
    assert result["best_pnl"] == selection["total_pnl"]
    assert result["training_summary"]["trade_count"] == len(result["best_trades"]) == 10
    assert result["training_summary"]["long_pct"] == 100
    assert result["training_summary"]["sum_profits"] > 0
    assert sum(bool(trade["_start_ts"]) for trade in result["best_trades"]) == 10
    job._refresh_best_chart(df)
    assert job.best_summary == selection
    assert job.chart_data["revision"] == 1


def test_tpe_history_keeps_validation_metrics_when_winning_parameters_are_unchanged(monkeypatch):
    pytest.importorskip("optuna")
    job, df = chart_job()
    job.method, job.n_trials = "tpe", 10
    monkeypatch.setitem(optimizer.PARAM_BOUNDS, job.strategy,
                        {name: (value, value) for name, value in job.best_params.items()})
    job._run_tpe(df)
    assert len(job.history) == 10
    assert {item["trade_count"] for item in job.history} == {3}
    assert {item["pnl"] for item in job.history} == {job.best_pnl}
    assert job.best_summary["trade_count"] == 3
    assert job.to_dict()["training_summary"]["trade_count"] == 10


@pytest.mark.parametrize("parallel", [False, True])
def test_de_keeps_selection_and_chart_metrics_separate_across_unchanged_generations(monkeypatch, parallel):
    job, df = chart_job()
    params = dict(job.best_params)
    def search(objective, **settings):
        vector = [params[name] for name in job._effective_bounds]
        for _ in range(2):
            objective(vector)
            settings["callback"](vector, convergence=.5)
        return SimpleNamespace(x=vector)
    monkeypatch.setattr(optimizer, "differential_evolution", search)
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
    if parallel:
        job._run_de_parallel(df, 2)
    else:
        job._run_de_serial(df)
    assert job.best_summary["trade_count"] == 3
    assert [item["trade_count"] for item in job.history] == [3, 3]
    assert job.to_dict()["training_summary"]["trade_count"] == len(job.best_trades) == 10
    assert job.chart_data["revision"] == 1


@pytest.mark.parametrize("paired", [False, True])
@pytest.mark.parametrize("tampered", [False, True])
def test_saved_validation_runs_recover_training_metrics_without_rewriting_data(tmp_path, paired, tampered):
    job, df = chart_job()
    job._compute_best_trades(df)
    snapshot = job.to_dict()
    snapshot.update(status="done", best_summary=job.validation_summary)
    snapshot.pop("training_summary", None)  # Format written before the reporting fix.
    if tampered:
        snapshot["best_trades"][0]["P&L ($)"] += 1
    if paired:
        snapshot = {"id": "pair", "created_at": 1, "status": "done", "sides": {"long": snapshot}}
    store = Store(tmp_path / "reporting.sqlite3")
    store.put("optimizer_jobs", snapshot["id"], snapshot)
    original = store.get("optimizer_jobs", snapshot["id"])
    manager = optimizer.OptimizerManager(store)
    restored = manager.get_job(snapshot["id"]).to_dict()
    result = restored["sides"]["long"] if paired else restored
    if tampered:
        assert result.get("training_summary") is None
        assert result.get("reporting_error")
    else:
        assert result.get("training_summary", {}).get("trade_count") == 10
        assert result["best_summary"]["trade_count"] == 3
        assert result["training_summary"]["metadata"]["reconstructed_from_saved_chart"] is True
    assert manager.get_job(snapshot["id"]).to_dict() == restored
    assert store.get("optimizer_jobs", snapshot["id"]) == original
