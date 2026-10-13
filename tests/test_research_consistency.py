import numpy as np
import pandas as pd
import pytest

from execution_model import evaluate_research
from strategy import AdvRsiParams


def research_candles(count=420):
    x = np.arange(count)
    prices = 100 + 12*np.sin(x/9) + 3*np.sin(x/2)
    return pd.DataFrame({"Open": prices, "High": prices+1, "Low": prices-1,
                         "Close": prices, "Volume": 1.0},
                        index=pd.date_range("2025-01-01", periods=len(x), freq="h"))


def test_research_entrypoints_share_net_model():
    from backtester import run_backtest_on_df
    from optimizer import evaluate
    df = research_candles()
    execution = {"entry_fee_rate": .001, "exit_fee_rate": .001, "slippage_bps": 5}
    expected = evaluate_research(df, "AdvRSI", 100, 3, execution=execution)["summary"]
    assert run_backtest_on_df(df, "AdvRSI", 100, 3, execution=execution) == expected
    assert evaluate(df, "AdvRSI", AdvRsiParams(), 100, 3, execution=execution) == expected


def test_heldout_evaluation_and_trade_log_agree():
    from optimizer import evaluate, OptimizerJob
    df = research_candles()
    boundary = 350
    execution = {"entry_fee_rate": .002}
    expected = evaluate_research(df, "AdvRSI", 100, 3, execution=execution, start_idx=boundary)
    assert evaluate(df.iloc[boundary:], "AdvRSI", AdvRsiParams(), 100, 3,
                    warmup_df=df.iloc[:boundary], execution=execution) == expected["summary"]
    job = OptimizerJob("btcusdt", "AdvRSI", "60", 420, 100, 3, execution=execution)
    job.best_params = vars(AdvRsiParams())
    job._compute_test_results(df, boundary)
    assert job.test_summary == expected["summary"]
    assert job.test_trades == expected["trades"]


def test_warmup_does_not_drop_first_test_trade():
    from backtester import _summarize
    records = pd.DataFrame([{"P&L ($)": 19.58, "Position": "Long", "Price Change": "10%"}])
    assert _summarize(records, 0, 10)["trade_count"] == 1


def test_trailing_data_cannot_change_training_result():
    df = research_candles()
    original = evaluate_research(df.iloc[:350], "AdvRSI", 100, 3)
    df.iloc[350:, :4] *= 7
    after = evaluate_research(df.iloc[:350], "AdvRSI", 100, 3)
    assert after["summary"] == original["summary"]


@pytest.mark.parametrize("kind", ["tester", "optimizer"])
def test_research_preferences_and_interrupted_results_survive_restart(kind, tmp_path):
    from storage import Store
    from backtester import TesterManager, TesterJob
    from optimizer import OptimizerManager, OptimizerJob
    store = Store(tmp_path / "research.sqlite3")
    manager_cls = TesterManager if kind == "tester" else OptimizerManager
    store.put("prefs", kind, {"credit": 271.0})
    first = manager_cls(store)
    assert first.get_config()["credit"] == 271
    job = TesterJob([("btcusdt", "AdvRSI", "60")], 100, 3) if kind == "tester" else OptimizerJob("btcusdt", "AdvRSI", "60", 420, 100, 3)
    first._attach(job)
    job.status = "running"
    job._persist()
    second = manager_cls(Store(store.path))
    recovered = second.get_job(job.id).to_dict()
    assert recovered["status"] == "interrupted"
    assert store.get(kind + "_jobs", job.id)["status"] == "interrupted"
    if kind == "optimizer":
        assert isinstance(recovered["random_seed"], int)


def test_completed_tester_persists_costed_results(monkeypatch, tmp_path):
    import asyncio
    import backtester
    from storage import Store
    df = research_candles(800)
    monkeypatch.setattr(backtester, "_fetch_df", lambda *args: df)
    store = Store(tmp_path / "jobs.sqlite3")
    manager = backtester.TesterManager(store)
    async def run():
        job = manager.start_job([("btcusdt", "AdvRSI", "60")], 100, 3,
                                execution={"entry_fee_rate": .001})
        await job._task
        return job
    job = asyncio.run(run())
    assert job.status == "done"
    saved = backtester.TesterManager(Store(store.path)).get_job(job.id).to_dict()
    assert saved == job.to_dict()
    assert saved["results"][0]["metadata"]["start_idx"] == 300
    assert saved["results"][0]["trade_count"] > 0


def test_optimizer_split_excludes_warmup_and_uses_fixed_training_window(monkeypatch):
    import asyncio
    import optimizer
    job = optimizer.OptimizerJob("btcusdt", "AdvRSI", "60", 1000, 100, 3, train_pct=70, random_seed=42)
    warmup = optimizer.required_warmup({name: high for name, (_, high) in job._effective_bounds.items()})
    df = research_candles(warmup + job.candles)
    monkeypatch.setattr(optimizer, "_fetch_df", lambda *args: df)
    observed = []
    def fake_search(train_df):
        observed.append(train_df.copy())
        job.best_params = vars(AdvRsiParams())
        job.best_summary = optimizer.evaluate(train_df, "AdvRSI", AdvRsiParams(), 100, 3,
                                               start_idx=job.evaluation_start)
    monkeypatch.setattr(job, "_run_de", fake_search)
    asyncio.run(job.run())
    assert job.status == "done", job.error
    assert job.evaluation_start == warmup
    assert job.train_candle_count == 700
    assert job.test_candle_count == 300
    assert len(observed[0]) == warmup + 700
    assert job.baseline["candle_count"] == job.best_summary["candle_count"] == 700
    assert job.test_summary["metadata"]["start_idx"] == warmup + 700
    assert job.to_dict()["random_seed"] == 42


def test_optimizer_spawn_workers_use_same_cost_model():
    from optimizer import OptimizerJob, evaluate
    job = OptimizerJob("btcusdt", "AdvRSI", "60", 420, 100, 3, cores=2,
                       execution={"entry_fee_rate": .001}, random_seed=123)
    settings = job._de_kwargs
    job._de_kwargs = lambda *args: {**settings(*args), "maxiter": 1, "popsize": 1, "init": "random"}
    df = research_candles()
    job._run_de_parallel(df, 2)
    assert job.best_params is not None
    assert job.best_summary == evaluate(df, "AdvRSI", AdvRsiParams(**job.best_params), 100, 3,
                                         execution=job.execution)


def test_optimizer_snapshot_records_effective_search_ranges():
    from optimizer import OptimizerJob, PARAM_BOUNDS

    minimum = PARAM_BOUNDS["AdvRSI"]["rsi_length"][0]
    job = OptimizerJob("btcusdt", "AdvRSI", "60", 420, 100, 3,
                       custom_bounds={"rsi_length": [minimum - 1, minimum + 4]})
    assert job.to_dict()["search_bounds"]["rsi_length"] == (minimum, minimum + 4)


@pytest.mark.parametrize("parallel", [False, True])
def test_optimizer_publishes_best_trade_chart_during_search(monkeypatch, parallel):
    from types import SimpleNamespace
    import optimizer

    job = optimizer.OptimizerJob("btcusdt", "AdvRSI", "60", 420, 100, 3, random_seed=42)
    job.status = "running"
    df = research_candles()
    defaults = AdvRsiParams()
    vector = np.array([getattr(defaults, name) for name in job._effective_bounds])

    if parallel:
        class FakePool:
            def map(self, fn, vectors):
                return [fn(v) for v in vectors]
            def terminate(self):
                pass
            def join(self):
                pass
        monkeypatch.setattr(optimizer.mp, "get_context", lambda method: SimpleNamespace(Pool=lambda **kwargs: FakePool()))

    def fake_de(objective, **kwargs):
        if not parallel:
            objective(vector)
        kwargs["callback"](vector, convergence=0.5)
        snapshot = job.to_dict()
        assert snapshot["status"] == "running"
        assert snapshot["chart_data"]["candles"]
        assert snapshot["best_trades"]
        assert all(trade["_start_ts"] and trade["_end_ts"] for trade in snapshot["best_trades"])
        first_chart = snapshot["chart_data"]
        kwargs["callback"](vector, convergence=0.5)
        assert job.chart_data is first_chart
        return SimpleNamespace(x=vector)

    monkeypatch.setattr(optimizer, "differential_evolution", fake_de)
    if parallel:
        job._run_de_parallel(df, 2)
    else:
        job._run_de_serial(df)


def test_persistence_failure_prevents_start(monkeypatch, tmp_path):
    from storage import Store
    from backtester import TesterManager, TesterJob
    store = Store(tmp_path / "failure.sqlite3")
    manager = TesterManager(store)
    manager.load()
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(store, "put", fail)
    job = TesterJob([], 100, 3)
    with pytest.raises(OSError, match="disk full"):
        manager._attach(job)
    assert manager.get_job(job.id) is None


def test_seeded_optimizer_selection_ignores_changed_heldout_prices(monkeypatch):
    """Exercise the actual search, not just the evaluator's train slice."""
    import asyncio
    import optimizer
    original = research_candles(1300)
    original.attrs["data_source"] = "synthetic"
    changed = original.copy()
    changed.iloc[1000:, :4] *= 4
    selected = []
    for frame in (original, changed):
        monkeypatch.setattr(optimizer, "_fetch_df", lambda *args, frame=frame: frame)
        defaults = vars(AdvRsiParams())
        bounds = {name: [defaults[name], defaults[name]] for name in optimizer.PARAM_BOUNDS["AdvRSI"]}
        bounds.update(oversold=[10, 40], rsi_length=[10, 30])
        job = optimizer.OptimizerJob("btcusdt", "AdvRSI", "60", 1000, 100, 3,
                                     cores=1, custom_bounds=bounds, train_pct=70,
                                     execution={"entry_fee_rate": .001}, random_seed=912)
        settings = job._de_kwargs
        job._de_kwargs = lambda *args, settings=settings: {
            **settings(*args), "maxiter": 1, "popsize": 1, "init": "random"}
        asyncio.run(job.run())
        assert job.status == "done", job.error
        assert job.training_summary["metadata"]["data_source"] == "synthetic"
        assert all(fold["metadata"]["data_source"] == "synthetic"
                   for fold in job.best_summary["fold_summaries"])
        assert job.test_summary["metadata"]["data_source"] == "synthetic"
        selected.append((job.best_params, job.best_summary, job.history))
    assert selected[0] == selected[1]
