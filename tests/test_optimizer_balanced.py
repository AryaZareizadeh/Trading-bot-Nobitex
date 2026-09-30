import numpy as np
import pytest


def _summary(pnl, win_rate, trades):
    return {"total_pnl": pnl, "win_rate": win_rate, "trade_count": trades}


def test_balanced_requires_both_floors_then_ranks_uncapped_pnl():
    from optimizer import _balanced_eligible, _compute_score

    small_sample = _summary(5000, 100, 3)
    low_win_rate = _summary(5000, 49.9, 50)
    eligible = _summary(400, 50, 30)
    higher_profit = _summary(5000, 50, 30)

    assert not _balanced_eligible(small_sample, 50, 30)
    assert not _balanced_eligible(low_win_rate, 50, 30)
    assert _balanced_eligible(eligible, 50, 30)
    assert _compute_score(small_sample, "balanced", 100, min_win_rate=50, min_closed_trades=30) > 0
    assert _compute_score(low_win_rate, "balanced", 100, min_win_rate=50, min_closed_trades=30) > 0
    assert (_compute_score(higher_profit, "balanced", 100, min_win_rate=50, min_closed_trades=30)
            < _compute_score(eligible, "balanced", 100, min_win_rate=50, min_closed_trades=30))
    assert (_compute_score(_summary(-10000, 50, 30), "balanced", 100, min_win_rate=50, min_closed_trades=30)
            < _compute_score(low_win_rate, "balanced", 100, min_win_rate=50, min_closed_trades=30))


@pytest.mark.parametrize("eligible_found", [False, True])
def test_serial_balanced_never_publishes_ineligible_candidate(monkeypatch, eligible_found):
    import optimizer

    job = optimizer.OptimizerJob("btcusdt", "AdvRSI", "60", 420, 100, 3,
                                 objective="balanced", min_win_rate=60, min_closed_trades=30)
    monkeypatch.setattr(job, "_refresh_best_chart", lambda df: None)
    monkeypatch.setattr(optimizer, "evaluate", lambda df, strategy, params, credit, leverage, **kwargs:
                        _summary(10000, 100, 2) if params.rsi_length == 10 else _summary(500, 60, 30))

    def fake_de(objective, **kwargs):
        vector = np.array([10 if name == "rsi_length" else low
                           for name, (low, high) in zip(job._effective_bounds, kwargs["bounds"])])
        objective(vector)
        if eligible_found:
            vector[0] = 20
            objective(vector)
        kwargs["callback"](vector, convergence=0.5)

    monkeypatch.setattr(optimizer, "differential_evolution", fake_de)
    job._run_de_serial(None)
    job.status = "done"
    assert job.to_dict()["no_eligible_result"] is not eligible_found
    assert (job.best_pnl == 500) if eligible_found else (job.best_pnl is None)


def test_balanced_request_validates_selectable_floors():
    from pydantic import ValidationError
    from server import OptimizerRunRequest

    base = {"symbol": "btcusdt", "strategy": "AdvRSI", "timeframe": "60"}
    request = OptimizerRunRequest(**base, objective="balanced", min_win_rate=62.5, min_closed_trades=40)
    assert (request.min_win_rate, request.min_closed_trades) == (62.5, 40)
    with pytest.raises(ValidationError):
        OptimizerRunRequest(**base, objective="balanced", min_win_rate=101)
    with pytest.raises(ValidationError):
        OptimizerRunRequest(**base, objective="balanced", min_closed_trades=3)
