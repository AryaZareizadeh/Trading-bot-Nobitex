"""Chronological, cost-aware research for the isolated spot portfolio.

The final test is inspected once. Model selection uses only two earlier folds.
All features are indexed by candle completion, and fills use the next open.
"""

from collections import defaultdict
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from spot_data import APPROVED_ASSETS, SECONDS
from spot_portfolio import D, SpotCosts, SpotRisk, execute_targets, mark, new_state, target_weights


RESEARCH_VERSION = "spot-research-v1"
HORIZONS = (4, 24, 72)  # hourly bars; selected at each decision from current forecasts
FEATURES = ("return_1", "return_4", "return_24", "return_72", "volatility_24",
            "volatility_72", "range", "volume_ratio", "btc_return_24")
SEED = 42


def feature_frame(candles, btc_context=None):
    if not candles:
        return pd.DataFrame()
    bars = sorted(candles, key=lambda c: c["available_at"])
    frame = pd.DataFrame(bars).set_index("available_at")
    frame.index = pd.to_datetime(frame.index, utc=True)
    frame = frame[~frame.index.duplicated(keep=False)].sort_index()
    close = frame["close"].astype(float)
    volume = frame["volume_base"].astype(float)
    # Missing hourly candles cannot be treated as adjacent observations.
    continuous = frame.index.to_series().diff().dt.total_seconds().eq(3600)
    for n in (1, 4, 24, 72):
        elapsed = (frame.index.to_series() - frame.index.to_series().shift(n)).dt.total_seconds()
        frame[f"return_{n}"] = (close / close.shift(n) - 1).where(elapsed.eq(n * 3600))
    frame["volatility_24"] = frame["return_1"].rolling(24, min_periods=24).std()
    frame["volatility_72"] = frame["return_1"].rolling(72, min_periods=72).std()
    frame["range"] = (frame["high"].astype(float) - frame["low"].astype(float)) / close
    frame["volume_ratio"] = volume / volume.rolling(24, min_periods=24).median().shift(1).replace(0, np.nan)
    if btc_context is None:
        frame["btc_return_24"] = frame["return_24"]
    else:
        context = btc_context["return_24"].reindex(frame.index, method="ffill", tolerance=pd.Timedelta(hours=1))
        frame["btc_return_24"] = context
    for horizon in HORIZONS:
        future = frame.index.to_series().shift(-horizon)
        elapsed = (future - frame.index.to_series()).dt.total_seconds()
        frame[f"label_{horizon}"] = (close.shift(-horizon) / close - 1).where(elapsed.eq(horizon * 3600))
        frame[f"future_{horizon}"] = future
    return frame.replace([np.inf, -np.inf], np.nan)


def frames_for(signal_candles):
    btc = feature_frame(signal_candles.get("BTC", []))
    return {asset: feature_frame(bars, btc if asset != "BTC" and not btc.empty else None)
            for asset, bars in signal_candles.items() if bars}


def _matrix(frames, start, end, horizon, *, training):
    chunks = []
    for asset, frame in frames.items():
        if frame.empty:
            continue
        selected = frame.loc[(frame.index >= start) & (frame.index < end)].copy()
        if training:
            selected = selected.loc[selected[f"future_{horizon}"] < end]
        selected = selected.dropna(subset=list(FEATURES) + ([f"label_{horizon}"] if training else []))
        if selected.empty:
            continue
        selected["asset"] = asset
        chunks.append(selected)
    if not chunks:
        return pd.DataFrame()
    matrix = pd.concat(chunks).sort_index()
    for asset in APPROVED_ASSETS:
        matrix[f"asset_{asset}"] = (matrix["asset"] == asset).astype(int)
    return matrix


MODEL_FEATURES = FEATURES + tuple(f"asset_{asset}" for asset in APPROVED_ASSETS)


def fit_models(frames, train_start, train_end, method):
    models = {}
    quality = {}
    for horizon in HORIZONS:
        rows = _matrix(frames, train_start, train_end, horizon, training=True)
        if len(rows) < 400:
            raise ValueError(f"Insufficient purged training rows for {horizon}h horizon")
        x = rows[list(MODEL_FEATURES)]
        y = rows[f"label_{horizon}"].to_numpy()
        if method == "hgb":
            from sklearn.ensemble import HistGradientBoostingRegressor
            factory = lambda: HistGradientBoostingRegressor(max_iter=100, max_leaf_nodes=9,
                                                             min_samples_leaf=40, l2_regularization=1,
                                                             random_state=SEED)
        elif method == "lightgbm":
            from lightgbm import LGBMRegressor
            factory = lambda: LGBMRegressor(n_estimators=100, num_leaves=9, min_child_samples=40,
                                            reg_lambda=1, random_state=SEED, verbosity=-1)
        else:
            raise ValueError("Unknown model method")
        returns = factory().fit(x, y)
        downside = factory().fit(x, np.maximum(-y, 0))
        models[horizon] = (returns, downside)
        quality[horizon] = {"training_rows": len(rows), "training_end": str(train_end),
                            "label_end_before": str(train_end)}
    return models, quality


def forecasts_for(frames, start, end, method, models=None):
    result = defaultdict(dict)
    for horizon in HORIZONS:
        rows = _matrix(frames, start, end, horizon, training=False)
        if rows.empty:
            continue
        if method == "momentum":
            estimates = rows[f"return_{horizon}"].to_numpy() if horizon in (4, 24) else rows["return_72"].to_numpy()
            downsides = rows["volatility_72"].to_numpy() * math.sqrt(horizon)
        else:
            estimates = models[horizon][0].predict(rows[list(MODEL_FEATURES)])
            downsides = models[horizon][1].predict(rows[list(MODEL_FEATURES)])
        for (timestamp, row), estimate, downside in zip(rows.iterrows(), estimates, downsides):
            result[timestamp.isoformat()].setdefault(row["asset"], {})[horizon] = {
                "return": float(estimate), "downside": max(0.001, float(downside))}
    return result


def _best_forecasts(predictions):
    """Current forecast selects the horizon; future realized returns are absent."""
    result = {}
    for asset, horizons in predictions.items():
        if not horizons:
            continue
        horizon, value = max(horizons.items(), key=lambda item:
                             (item[1]["return"] - 0.5 * item[1]["downside"]) /
                             max(math.sqrt(item[0]), 1))
        result[asset] = {**value, "horizon_hours": horizon}
    return result


def _report(state, capital):
    points = [p for p in state["equity"] if p["complete"]]
    last = points[-1]["equity_usdt"] if points else capital
    fills = state["fills"]
    monthly = {}
    for point in points:
        key = point["at"][:7]
        monthly.setdefault(key, [point["equity_usdt"], point["equity_usdt"]])[1] = point["equity_usdt"]
    by_asset = defaultdict(lambda: {"trades": 0, "fees_usdt": 0, "turnover_usdt": 0})
    open_lots = defaultdict(list)
    durations = []
    for fill in fills:
        row = by_asset[fill["asset"]]
        row["trades"] += 1
        row["fees_usdt"] += fill["fee_usdt"]
        row["turnover_usdt"] += fill["filled_usdt"]
        quantity = float(fill["quantity"])
        if fill["side"] == "buy":
            open_lots[fill["asset"]].append([quantity, datetime.fromisoformat(fill["at"])])
        else:
            remaining = quantity
            while remaining > 1e-10 and open_lots[fill["asset"]]:
                lot = open_lots[fill["asset"]][0]
                used = min(remaining, lot[0])
                durations.append((datetime.fromisoformat(fill["at"]) - lot[1]).total_seconds() / 3600)
                lot[0] -= used
                remaining -= used
                if lot[0] < 1e-10:
                    open_lots[fill["asset"]].pop(0)
    return {"net_pnl_usdt": round(last - capital, 4), "return_pct": round(100 * (last / capital - 1), 4),
            "max_drawdown_pct": round(max((p["drawdown_pct"] for p in points), default=0), 4),
            "fees_usdt": round(sum(f["fee_usdt"] for f in fills), 4),
            "turnover_usdt": round(sum(f["filled_usdt"] for f in fills), 4),
            "trade_count": len(fills), "mean_closed_holding_hours": round(sum(durations) / len(durations), 2) if durations else None,
            "time_in_usdt_pct": round(100 * sum(
                p["cash_usdt"] / p["equity_usdt"] for p in points if p["equity_usdt"] > 0) /
                max(len(points), 1), 2),
            "per_period": {key: round(100 * (end / start - 1), 3) for key, (start, end) in monthly.items()},
            "per_asset": dict(by_asset), "equity": points, "decisions": state["decisions"],
            "fills": fills, "orders": state["orders"]}


def replay(signal, venue, predictions, start, end, *, method, capital=200,
           costs=SpotCosts(), risk=SpotRisk()):
    """At completion t, use known venue marks; fill at its next open.

    Nobitex hourly candles currently open at :30 UTC. A Binance close at :00
    therefore cannot be matched by timestamp equality. The first following
    Nobitex open is eligible, while marks and volume come from bars complete
    by the signal time.
    """
    state = new_state(capital, tuple(signal))
    venue_index = {}
    for asset, bars in venue.items():
        ordered = sorted(bars, key=lambda bar: bar["open_time"])
        venue_index[asset] = (
            [datetime.fromisoformat(bar["open_time"]) for bar in ordered],
            [datetime.fromisoformat(bar["available_at"]) for bar in ordered], ordered)
    times = sorted({bar["available_at"] for bars in signal.values() for bar in bars
                    if str(start) <= bar["available_at"] < str(end)})
    fixed = None
    for timestamp in times:
        decision_at = datetime.fromisoformat(timestamp)
        current, previous = {}, {}
        for asset, (opens, closes, bars) in venue_index.items():
            next_idx = bisect_left(opens, decision_at)
            if next_idx < len(opens) and (opens[next_idx] - decision_at).total_seconds() < 3600:
                current[asset] = bars[next_idx]
            prior_idx = bisect_right(closes, decision_at) - 1
            if prior_idx >= 0 and (decision_at - closes[prior_idx]).total_seconds() <= 3600:
                previous[asset] = bars[prior_idx]
        prices = {asset: bar["close"] for asset, bar in previous.items()}
        owned = {a for a, qty in state["holdings"].items() if D(qty) > 0}
        if not owned.issubset(prices):
            state["decisions"].append({"at": timestamp, "reason": "missing Nobitex mark; paused"})
            continue
        if not prices:
            continue
        if int(datetime.fromisoformat(timestamp).timestamp() // 3600) % 4 or timestamp not in predictions:
            mark(state, prices, timestamp)
            continue
        quotes = {}
        for asset, bar in current.items():
            completed = previous.get(asset)
            if completed is None:
                continue
            opening = D(bar["open"])
            spread = D("0.001")
            # Only volume complete at the signal time may bound liquidity.
            depth = D(completed["volume_base"]) * opening * D("0.10")
            quotes[asset] = {"ask": opening * (1 + spread / 2),
                             "bid": opening * (1 - spread / 2),
                             "ask_depth": depth, "bid_depth": depth}
        observed = mark(state, prices, timestamp)
        if not observed["complete"]:
            continue
        if method == "basket":
            if fixed is None:
                eligible = sorted(a for a in predictions[timestamp] if a in quotes)
                fixed = {a: .99 / len(eligible) for a in eligible} if eligible else {}
            targets, reason = fixed, "equal-weight entry basket, held without rebalancing"
            if state["fills"]:
                targets = {a: D(state["holdings"].get(a, 0)) * D(prices[a]) / D(observed["equity_usdt"])
                           for a in state["holdings"] if a in prices}
        else:
            forecast = _best_forecasts(predictions[timestamp])
            targets, reason = target_weights(forecast, state, observed["equity_usdt"], risk, costs,
                                             now=datetime.fromisoformat(timestamp).timestamp())
        # Historical depth and spread are assumptions, never reconstructed fills.
        execution_risk = SpotRisk(max_turnover_fraction=D(1), no_trade_fraction=D(0)) if method == "basket" else risk
        execute_targets(state, targets, quotes, prices, costs=costs, risk=execution_risk, at=timestamp,
                        reason=reason, model_version=method + ":" + RESEARCH_VERSION)
    return _report(state, capital)


def forecast_quality(frames, start, end, method, models):
    """Held-out error metrics; downside is a magnitude forecast, not a probability."""
    result = {}
    for horizon in HORIZONS:
        rows = _matrix(frames, start, end, horizon, training=True)
        if rows.empty:
            continue
        actual = rows[f"label_{horizon}"].to_numpy()
        downside_actual = np.maximum(-actual, 0)
        if method == "momentum":
            estimates = rows[f"return_{horizon}"].to_numpy()
            downside = rows["volatility_72"].to_numpy() * math.sqrt(horizon)
        else:
            estimates = models[horizon][0].predict(rows[list(MODEL_FEATURES)])
            downside = models[horizon][1].predict(rows[list(MODEL_FEATURES)])
        result[str(horizon)] = {"rows": len(rows),
                                "return_mae_bps": round(float(np.mean(np.abs(actual - estimates))) * 10000, 3),
                                "zero_return_mae_bps": round(float(np.mean(np.abs(actual))) * 10000, 3),
                                "downside_mae_bps": round(float(np.mean(np.abs(downside_actual - downside))) * 10000, 3),
                                "sign_accuracy_pct": round(float(np.mean(np.sign(actual) == np.sign(estimates))) * 100, 2)}
    return result


def run_experiment(signal, venue, *, capital=200, costs=SpotCosts(), risk=SpotRisk()):
    """Two forward validation folds, then one untouched final test."""
    frames = frames_for(signal)
    times = sorted({t for frame in frames.values() for t in frame.index if not frame.empty})
    if len(times) < 1200:
        raise ValueError("Need at least 1,200 hourly timestamps for chronological evaluation")
    cuts = [times[int(len(times) * fraction)].isoformat() for fraction in (0, .50, .65, .80)]
    cuts.append((times[-1] + pd.Timedelta(hours=1)).isoformat())
    methods = ["momentum", "hgb"]
    try:
        import lightgbm  # noqa: F401
        methods.append("lightgbm")
    except ImportError:
        pass
    validation = {}
    quality = {}
    for method in methods:
        folds = []
        for train_end, val_end in ((1, 2), (2, 3)):
            model, metadata = (None, {}) if method == "momentum" else fit_models(frames, cuts[0], cuts[train_end], method)
            predicted = forecasts_for(frames, cuts[train_end], cuts[val_end], method, model)
            result = replay(signal, venue, predicted, cuts[train_end], cuts[val_end],
                            method=method, capital=capital, costs=costs, risk=risk)
            folds.append({"return_pct": result["return_pct"], "max_drawdown_pct": result["max_drawdown_pct"],
                          "trade_count": result["trade_count"]})
            quality[f"{method}_{train_end}"] = metadata
        validation[method] = folds
    # Selection rule is fixed before the final period is ever scored.
    selected = max(methods, key=lambda method: (
        sum(f["return_pct"] for f in validation[method]) -
        sum(max(0, f["max_drawdown_pct"] - 10) for f in validation[method]), method))
    model, metadata = (None, {}) if selected == "momentum" else fit_models(frames, cuts[0], cuts[3], selected)
    predicted = forecasts_for(frames, cuts[3], cuts[4], selected, model)
    test = replay(signal, venue, predicted, cuts[3], cuts[4], method=selected,
                  capital=capital, costs=costs, risk=risk)
    stress_costs = SpotCosts(fee_rate=costs.fee_rate * 2,
                             slippage_rate=costs.slippage_rate * 3,
                             min_notional=costs.min_notional,
                             max_book_fraction=costs.max_book_fraction / 2)
    stress = replay(signal, venue, predicted, cuts[3], cuts[4], method=selected,
                    capital=capital, costs=stress_costs, risk=risk)
    benchmark = {}
    benchmark["momentum"] = replay(signal, venue, forecasts_for(frames, cuts[3], cuts[4], "momentum"),
                                    cuts[3], cuts[4], method="momentum", capital=capital, costs=costs, risk=risk)
    benchmark["basket"] = replay(signal, venue, predicted, cuts[3], cuts[4],
                                  method="basket", capital=capital, costs=costs, risk=risk)
    benchmark["usdt"] = {"return_pct": 0, "net_pnl_usdt": 0, "max_drawdown_pct": 0}
    digest = hashlib.sha256(json.dumps({a: [b["data_hash"] for b in bars] for a, bars in signal.items()},
                                    sort_keys=True).encode()).hexdigest()
    venue_digest = hashlib.sha256(json.dumps({a: [b["data_hash"] for b in bars] for a, bars in venue.items()},
                                          sort_keys=True).encode()).hexdigest()
    code_hash = hashlib.sha256(b"".join((Path(__file__).with_name(name).read_bytes() for name in
                                         ("spot_research.py", "spot_portfolio.py", "spot_data.py",
                                          "spot_paper.py")))).hexdigest()
    quality_test = forecast_quality(frames, cuts[3], cuts[4], selected, model)
    criteria = {
        "minimum_history_days": (times[-1] - times[0]).total_seconds() + 3600 >= 365 * 86400,
        "both_validation_folds_positive": all(f["return_pct"] > 0 for f in validation[selected]),
        "test_beats_cash_and_basket_by_2pp": test["return_pct"] > max(0, benchmark["basket"]["return_pct"]) + 2,
        "test_drawdown_at_most_10pct": test["max_drawdown_pct"] <= 10,
        "stress_net_positive": stress["net_pnl_usdt"] > 0,
        "forecast_beats_zero_in_two_horizons": sum(
            row["return_mae_bps"] < row["zero_return_mae_bps"] for row in quality_test.values()) >= 2,
    }
    return {"version": RESEARCH_VERSION, "data_hash": digest, "venue_data_hash": venue_digest,
            "seed": SEED,
            "code_hash": code_hash, "costs": {k: str(v) for k, v in costs.__dict__.items()},
            "risk": {k: str(v) for k, v in risk.__dict__.items()},
            "capital_usdt": capital, "windows": cuts, "validation": validation,
            "selected": selected, "model_quality": {**quality, "final_fit": metadata},
            "forecast_quality": quality_test,
            "test": test, "stress": stress, "benchmarks": benchmark,
            "validation_outcome": {"passed": all(criteria.values()), "checks": criteria},
            "execution_note": "Historical Nobitex fills use OHLC open plus assumed spread, slippage and previous-bar volume. Depth, queue position and limit fills are unavailable.",
            "acceptance": {"minimum_history_days": 365, "test_edge_over_cash_and_basket_pp": 2,
                           "target_drawdown_pct": 10, "stress_costs_required": True}}
