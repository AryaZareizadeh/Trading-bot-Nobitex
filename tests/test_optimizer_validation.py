"""Frozen selection and source transfer use distinct chronological windows."""
import asyncio
from dataclasses import asdict

import pandas as pd
import pytest

import optimizer
from storage import Store


def candles(count=1600, price=100.):
    df = pd.DataFrame({name: price for name in ("Open", "High", "Low", "Close", "Volume")},
                      index=pd.date_range("2026-01-01", periods=count, freq="h", tz="UTC"))
    df.attrs["data_source"] = "binance"
    return df


def summary(pnl=0.):
    return {"total_pnl": pnl, "trade_count": 40, "win_rate": 60., "price_changes": [1.] * 40}


def prepared_run(monkeypatch, *, evaluation_mode="chronological", transfer=None):
    df = candles()
    options = {"evaluation_mode": evaluation_mode}
    job = optimizer.OptimizerJob("btcusdt", "AdvRSI", "60", 1000, 100, 3,
                                 random_seed=12, **options)
    monkeypatch.setattr(optimizer, "_fetch_df", lambda *args: df)
    monkeypatch.setattr(optimizer, "evaluate", lambda df, *args, **kwargs: summary())
    seen = []
    def selected(train):
        seen.append(train.index[-1])
        job.best_params = asdict(optimizer.PARAMS_CLASS["AdvRSI"]())
        job.best_params["rsi_length"] = 6
        job.best_pnl, job.best_summary = 3., summary(3.)
    monkeypatch.setattr(job, "_run_search", selected)
    monkeypatch.setattr(job, "_refresh_best_chart", lambda *args: None)
    def final_test(*args):
        job.test_summary, job.test_baseline = summary(2.), summary(0.)
    monkeypatch.setattr(job, "_compute_test_results", final_test)
    if transfer is not None:
        monkeypatch.setattr(optimizer, "_fetch_transfer_df", lambda *args: transfer)
    asyncio.run(job.run())
    return job, df, seen


def test_explicit_chronological_optimizer_keeps_train_and_holdout(monkeypatch):
    job, df, seen = prepared_run(monkeypatch)
    assert job.status == "done", job.error
    assert job.train_pct == 70.
    assert (job.train_candle_count, job.test_candle_count) == (700, 300)
    assert seen == [df.index[-301]]
    result = job.to_dict()
    assert result["frozen_params"] == job.best_params
    assert result["evaluation_metadata"]["source"] == "binance"
    assert result["evaluation_metadata"]["random_seed"] == 12
    assert result["evaluation_metadata"]["selection_rule"]
    assert len(result["validation_folds"]) == 3
    assert all(pd.Timestamp(fold["end"]) < df.index[-300] for fold in result["validation_folds"])


@pytest.mark.parametrize("method", ["de", "tpe"])
@pytest.mark.parametrize("cores", [1, 2])
@pytest.mark.parametrize("paired", [False, True])
def test_default_optimizer_scores_and_displays_all_selected_candles(monkeypatch, method, cores, paired):
    if method == "tpe":
        pytest.importorskip("optuna")
    import numpy as np
    df = candles()
    prices = 100 + 20 * np.sin(np.arange(len(df)) / 9)
    for column in ("Open", "Close"):
        df[column] = prices
    df["High"], df["Low"] = prices + 1, prices - 1
    # A reused data frame must not bring a previous run's validation split along.
    df.attrs["optimizer_validation_folds"] = [(1100, 1200), (1200, 1400), (1400, 1600)]
    monkeypatch.setattr(optimizer, "_fetch_df", lambda *args: df)
    monkeypatch.setattr(optimizer, "_DE_POPSIZE", 1)
    strategies = ("AdvRSILong", "AdvRSIShort") if paired else ("AdvRSI",)
    for strategy in strategies:
        defaults = optimizer._defaults_for(strategy)
        monkeypatch.setitem(optimizer.PARAM_BOUNDS, strategy,
                            {name: (value, value) for name, value in defaults.items()})
    settings = dict(cores=cores, maxiter=5, method=method, n_trials=10, random_seed=12)
    job = (optimizer.IndependentOptimizerJob("btcusdt", "60", 1000, 100, 100, 3, **settings)
           if paired else optimizer.OptimizerJob("btcusdt", "AdvRSI", "60", 1000, 100, 3, **settings))
    assert job.evaluation_mode == "in_sample"
    asyncio.run(job.run())
    assert job.status == "done", job.error
    result = job.to_dict()
    assert result["evaluation_mode"] == "in_sample"
    assert result["train_pct"] is None
    for side in (result["sides"].values() if paired else [result]):
        assert side["candle_count"] == side["train_candle_count"] == 1000
        assert side["test_candle_count"] == 0
        assert side["validation_folds"] == []
        assert side["test_summary"] is None
        assert side["baseline"]["candle_count"] == side["best_summary"]["candle_count"] == 1000
        assert side["training_summary"] == side["best_summary"]
        assert side["best_pnl"] == side["training_summary"]["total_pnl"]
        assert len(side["chart_data"]["candles"]) == 1000
        assert side["chart_data"]["candles"][0]["time"] == int(df.index[-1000].timestamp())
        assert side["chart_data"]["candles"][-1]["time"] == int(df.index[-1].timestamp())
        for series in side["chart_data"]["indicators"].values():
            if isinstance(series, list):
                assert len(series) == 1000
        assert side["training_summary"]["trade_count"] == len(side["best_trades"])
        assert all(item["pnl"] == side["best_pnl"] for item in side["history"])
        assert side["evaluation_metadata"]["test_window"] is None
    assert "optimizer_validation_folds" in df.attrs  # Caller-owned history is untouched.


def test_explicit_in_sample_search_is_labeled_and_cannot_promote(monkeypatch):
    job, df, seen = prepared_run(monkeypatch, evaluation_mode="in_sample")
    assert job.status == "done", job.error
    assert job.train_pct is None
    assert seen == [df.index[-1]]
    assert job.to_dict()["evaluation_mode"] == "in_sample"
    assert job.to_dict()["promotion"]["eligible"] is False


def test_forward_validation_never_passes_future_fold_or_holdout_to_candidate(monkeypatch):
    df = candles(30)
    df.attrs["optimizer_validation_folds"] = [(15, 20), (20, 25), (25, 30)]
    seen = []
    def evaluate(frame, strategy, params, *args, **kwargs):
        seen.append((len(frame), kwargs["start_idx"]))
        return summary(2. if params.rsi_length == 6 else 1.)
    monkeypatch.setattr(optimizer, "evaluate", evaluate)
    result = optimizer.evaluate_selection(df, "AdvRSI", optimizer.PARAMS_CLASS["AdvRSI"](rsi_length=6), 100, 3, start_idx=0)
    assert seen == [(20, 15), (25, 20), (30, 25)]
    assert result["total_pnl"] == 6.
    assert result["trade_count"] == 120


def test_transfer_evaluates_frozen_params_on_aligned_nobitex_holdout(monkeypatch):
    target = candles(price=777.)
    target.attrs["data_source"] = "nobitex"
    job, df, _ = prepared_run(monkeypatch, transfer=target)
    observed = []
    def evaluate(frame, strategy, params, *args, **kwargs):
        observed.append((frame.attrs["data_source"], frame.index[kwargs["start_idx"]], frame.Close.iloc[-1], params.rsi_length))
        return summary(4. if params.rsi_length == 6 else 0.)
    monkeypatch.setattr(optimizer, "evaluate", evaluate)
    job.validation_summary, job.validation_baseline = summary(2.), summary(0.)
    job._compute_transfer_results(df, len(df)-300)
    result = job.to_dict()["promotion"]
    assert result["eligible"] is True
    assert result["target_source"] == "nobitex"
    assert observed == [("nobitex", df.index[-300], 777., 6), ("nobitex", df.index[-300], 777., 40)]
    assert job.best_params == job.frozen_params


@pytest.mark.parametrize("missing", [True, False])
def test_missing_transfer_data_preserves_draft_and_blocks_real_promotion(monkeypatch, missing):
    target = candles().drop(candles().index[-20]) if missing else pd.DataFrame()
    target.attrs["data_source"] = "nobitex"
    job, _, _ = prepared_run(monkeypatch, transfer=target)
    assert job.status == "done", job.error
    assert job.best_params
    assert job.to_dict()["promotion"]["eligible"] is False
    assert job.to_dict()["promotion"]["status"] == "unavailable"


def test_legacy_results_remain_readable_and_are_labeled_without_validation(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.put("optimizer_jobs", "legacy", {"id": "legacy", "status": "done", "created_at": 1,
                                           "best_params": {"rsi_length": 6}, "train_pct": None})
    result = optimizer.OptimizerManager(store).get_job("legacy").to_dict()
    assert result["best_params"] == {"rsi_length": 6}
    assert result["evaluation_mode"] == "legacy_in_sample"
    assert result["promotion"]["eligible"] is False
