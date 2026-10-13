"""Offline regressions for event chronology, context identity and paper history."""
from datetime import datetime, timedelta, timezone

import joblib
import numpy as np
import pandas as pd
import pytest

from price_ai import TrainedPriceModel, _features, evaluate_price_model, train_price_model
from spot_paper import SpotPaperService
from spot_portfolio import D, SpotCosts, new_state
from spot_research import RESEARCH_VERSION, frames_for, replay
from storage import Store
from tests.test_price_ai import EXECUTION, candles


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def bar(asset, opened, opening, closing=None):
    close = opening if closing is None else closing
    return {"asset": asset, "open_time": opened.isoformat(),
            "available_at": (opened + timedelta(hours=1)).isoformat(),
            "open": opening, "close": close, "high": max(opening, close) + 1,
            "low": min(opening, close) - 1, "volume_base": 10000,
            "data_hash": f"{int(opened.timestamp()):064x}"}


def replay_gap(assets=("BTC",), ending=None):
    decision = BASE + timedelta(hours=4)
    signal = {a: [bar(a, BASE + timedelta(hours=i), 100) for i in range(7)] for a in assets}
    venue = {}
    for offset, asset in enumerate(assets):
        shift = timedelta(minutes=30 + offset * 15)
        venue[asset] = [bar(asset, BASE + timedelta(hours=i) + shift, 100 if i < 4 else 50)
                        for i in range(7)]
    predictions = {decision.isoformat(): {a: {4: {"return": .1, "downside": .01}} for a in assets}}
    report = replay(signal, venue, predictions, decision.isoformat(),
                    (ending or BASE + timedelta(hours=6)).isoformat(), method="basket",
                    costs=SpotCosts(fee_rate=D(0), slippage_rate=D(0), min_notional=D(1)))
    return report, decision


def test_future_open_gap_never_creates_an_old_price_gain_or_peak():
    report, decision = replay_gap()
    assert report["fills"][0]["at"] == (decision + timedelta(minutes=30)).isoformat()
    assert report["fills"][0]["decision_at"] == decision.isoformat()
    # Allocation quantity was sized from the known 100 mark, before seeing 50.
    assert D(report["fills"][0]["quantity"]) == D("1.98")
    assert max(point["equity_usdt"] for point in report["equity"]) <= 200
    assert report["return_pct"] <= 0


def test_multiple_venue_clocks_fill_at_each_actual_open_and_mark_at_boundary():
    ending = BASE + timedelta(hours=5, minutes=50)
    report, decision = replay_gap(("BTC", "ETH"), ending)
    assert {f["asset"]: f["at"] for f in report["fills"]} == {
        "BTC": (decision + timedelta(minutes=30)).isoformat(),
        "ETH": (decision + timedelta(minutes=45)).isoformat()}
    assert report["equity"][-1]["at"] == ending.isoformat()
    assert report["equity"][-1]["complete"] is True
    assert max(point["equity_usdt"] for point in report["equity"]) <= 200


def test_final_boundary_uses_observed_open_without_future_close():
    decision = BASE + timedelta(hours=4)
    signal = {"BTC": [bar("BTC", BASE + timedelta(hours=i), 100) for i in range(6)]}
    venue = {"BTC": [bar("BTC", BASE + timedelta(hours=i, minutes=30), 100 if i < 5 else 120,
                         100 if i < 5 else 900) for i in range(6)]}
    predictions = {decision.isoformat(): {"BTC": {4: {"return": .1, "downside": .01}}}}
    ending = BASE + timedelta(hours=5, minutes=45)
    report = replay(signal, venue, predictions, decision.isoformat(), ending.isoformat(),
                    method="basket", costs=SpotCosts(fee_rate=D(0), slippage_rate=D(0), min_notional=D(1)))
    final = report["equity"][-1]
    assert final["at"] == ending.isoformat()
    assert 230 < final["equity_usdt"] < 250


def test_price_ai_gap_invalidates_features_and_training_horizon():
    original = candles()
    gapped = original.drop(original.index[500])
    features = _features(gapped)
    assert features.loc[original.index[501]].isna().all()
    original_model = train_price_model(original, 700)
    gapped_model = train_price_model(gapped, 699)
    # 24 following feature windows, plus four labels before the gap, are invalid.
    assert gapped_model.metadata["training_rows"] == original_model.metadata["training_rows"] - 29


def test_price_ai_gap_abstains_in_direct_inference():
    class AlwaysLong:
        def predict(self, frame):
            return np.full(len(frame), 200.)
    original = candles(150)
    frame = original.drop(original.index[100])
    result = evaluate_price_model(frame, TrainedPriceModel(AlwaysLong(), {"model_version": "test"}),
                                  100, 1, EXECUTION, start_idx=100)
    assert result["decisions"][0]["action"] == "hold"
    assert result["decisions"][0]["forecast_return_bps"] is None


def test_missing_btc_never_substitutes_the_allocated_assets_return():
    eth = [bar("ETH", BASE + timedelta(hours=i), 100 + i) for i in range(120)]
    frame = frames_for({"ETH": eth})["ETH"]
    assert pd.notna(frame.iloc[-1]["return_24"])
    assert pd.isna(frame.iloc[-1]["btc_return_24"])


def test_paper_btc_context_is_fetched_without_allocation_or_book(tmp_path, monkeypatch):
    now = (BASE + timedelta(hours=124)).timestamp() + 60
    monkeypatch.setattr("spot_paper.time.time", lambda: now)
    called = []
    class Signals:
        def fetch(self, asset, *args, **kwargs):
            called.append(asset)
            return {"valid": 120, "candles": [bar(asset, BASE + timedelta(hours=4 + i),
                       100 if asset == "BTC" else 100 + i) for i in range(120)]}
    class Venue:
        def books(self):
            return {"ETH": {"lastUpdate": now * 1000, "bids": [["220", "100"]], "asks": [["221", "100"]]}}
        def rules(self):
            return {"min_notional": ".5", "quantity_steps": {"ETH": ".000001"}}
    service = SpotPaperService(Store(tmp_path / "paper.db"), signals=Signals(), venue=Venue())
    service.start(["ETH"])
    joblib.dump({"version": RESEARCH_VERSION, "method": "momentum", "models": None, "assets": ["ETH"]}, service.model_path)
    result = service.tick()
    assert "BTC" in called
    assert result["ok"] is True
    assert result["state"]["decisions"][0]["forecast"]["ETH"]["return"] > 0
    assert service.state()["assets"] == ["ETH"]


def test_embedded_history_migrates_without_loss_and_only_bounded_tails_are_rewritten(tmp_path):
    store = Store(tmp_path / "paper.db")
    old = new_state(200, ["BTC"])
    old["equity"] = [{"at": str(i), "complete": True, "equity_usdt": 200, "drawdown_pct": 0, "cash_usdt": 200}
                     for i in range(620)]
    old["fills"] = [{"at": str(i), "asset": "BTC", "side": "buy", "quantity": "1"} for i in range(620)]
    store.put("spot_manager", "paper", old)
    service = SpotPaperService(store)
    migrated = service.state()
    assert len(migrated["equity"]) <= 500
    assert len(migrated["fills"]) <= 500
    assert len(service.history("equity", limit=None)) == 620
    assert len(service.history("fills", limit=None)) == 620
    first = service.history("equity", limit=None)[0]
    migrated["equity"].append({**first, "at": "new"})
    assert service._save_tick(migrated, service.generation)
    restarted = SpotPaperService(Store(store.path))
    assert len(restarted.history("equity", limit=None)) == 621
    assert restarted.history("equity", limit=None)[0] == first
    assert len(store.get("spot_manager", "paper")["equity"]) <= 500
    reset = restarted.reset()
    assert reset["backup_id"]
    assert len(restarted.history("equity", session_id=migrated["history_id"], limit=None)) == 621


def test_unavailable_btc_abstains_even_when_eth_history_and_book_are_usable(tmp_path, monkeypatch):
    now = (BASE + timedelta(hours=124)).timestamp() + 60
    monkeypatch.setattr("spot_paper.time.time", lambda: now)
    called = []
    class Signals:
        def fetch(self, asset, *args, **kwargs):
            called.append(asset)
            if asset == "BTC":
                raise ValueError("BTC feed unavailable")
            return {"valid": 120, "candles": [bar(asset, BASE + timedelta(hours=4 + i), 100 + i) for i in range(120)]}
    class Venue:
        def books(self):
            return {"ETH": {"lastUpdate": now * 1000, "bids": [["220", "100"]], "asks": [["221", "100"]]}}
        def rules(self):
            return {"min_notional": ".5", "quantity_steps": {"ETH": ".000001"}}
    service = SpotPaperService(Store(tmp_path / "paper.db"), signals=Signals(), venue=Venue())
    service.start(["ETH"])
    joblib.dump({"version": RESEARCH_VERSION, "method": "momentum", "models": None, "assets": ["ETH"]}, service.model_path)
    result = service.tick()
    assert result["ok"] is False
    assert "BTC context unavailable" in result["reason"]
    assert service.state()["fills"] == []
    assert "BTC" in called


def test_historical_btc_context_remains_outside_the_allocation_universe(tmp_path, monkeypatch):
    captured = {}
    class Signals:
        def fetch(self, asset, *args, **kwargs):
            return {"valid": 220, "candles": [bar(asset, BASE + timedelta(hours=i), 100 + (i if asset == "ETH" else 0))
                                              for i in range(220)]}
    class Venue:
        def books(self):
            return {"ETH": {}}
        def history(self, asset, *args, **kwargs):
            assert asset == "ETH"
            return Signals().fetch(asset)
    def experiment(signal, venue, *, btc_context=None, **kwargs):
        captured.update(signal=signal, btc_context=btc_context)
        frame = frames_for(signal, btc_context)["ETH"]
        assert frame.iloc[-1]["btc_return_24"] == 0
        return {"selected": "momentum", "data_hash": "synthetic", "code_hash": "synthetic",
                "windows": ["a", "b"], "validation_outcome": {"passed": False}}
    monkeypatch.setattr("spot_paper.run_experiment", experiment)
    service = SpotPaperService(Store(tmp_path / "paper.db"), signals=Signals(), venue=Venue())
    report = service.research(["ETH"], days=90)
    assert set(captured["signal"]) == {"ETH"}
    assert all(row["asset"] == "BTC" for row in captured["btc_context"])
    assert report["eligible_assets"] == ["ETH"]
    assert report["btc_context"]["allocation_asset"] is False
    assert joblib.load(service.model_path)["assets"] == ["ETH"]


def test_four_hour_price_ai_feature_gap_recovers_only_after_a_full_window():
    original = candles(130)
    original.index = pd.date_range(BASE, periods=len(original), freq="4h")
    frame = original.drop(original.index[70])
    features = _features(frame, "240")
    assert features.loc[original.index[71]:original.index[94]].isna().all().all()
    assert features.loc[original.index[95]].notna().all()


def test_missing_final_venue_marks_rejects_metrics_instead_of_reusing_old_equity():
    decision = BASE + timedelta(hours=4)
    signal = {"BTC": [bar("BTC", BASE + timedelta(hours=i), 100) for i in range(8)]}
    venue = {"BTC": [bar("BTC", BASE + timedelta(hours=i, minutes=30), 100) for i in range(5)]}
    predictions = {decision.isoformat(): {"BTC": {4: {"return": .1, "downside": .01}}}}
    with pytest.raises(ValueError, match="final boundary.*unpriced"):
        replay(signal, venue, predictions, decision.isoformat(), (BASE + timedelta(hours=8)).isoformat(), method="basket")


def test_history_append_rolls_back_with_the_state_snapshot(tmp_path, monkeypatch):
    store = Store(tmp_path / "paper.db")
    service = SpotPaperService(store)
    state = service.start(["BTC"])
    before = store.get("spot_manager", "paper")
    state["equity"].append({"at": "new", "complete": True, "equity_usdt": 200})
    write = store.write
    def fail_snapshot(conn, namespace, key, value):
        if namespace == "spot_manager" and key == "paper":
            raise OSError("snapshot write failed")
        return write(conn, namespace, key, value)
    monkeypatch.setattr(store, "write", fail_snapshot)
    with pytest.raises(OSError, match="snapshot write failed"):
        service._save_tick(state, service.generation)
    assert service.history("equity", session_id=before["history_id"], limit=None) == []
    assert store.get("spot_manager", "paper") == before


def test_future_venue_gap_cannot_reallocate_the_missing_assets_frozen_budget():
    decision = BASE + timedelta(hours=4)
    signal = {a: [bar(a, BASE + timedelta(hours=i), 100) for i in range(7)] for a in ("BTC", "ETH")}
    venue = {
        "BTC": [bar("BTC", BASE + timedelta(hours=i, minutes=30), 100) for i in range(4)],
        "ETH": [bar("ETH", BASE + timedelta(hours=i, minutes=45), 100) for i in range(7)]}
    predictions = {decision.isoformat(): {a: {4: {"return": .1, "downside": .01}} for a in signal}}
    report = replay(signal, venue, predictions, decision.isoformat(), (BASE + timedelta(hours=6)).isoformat(),
                    method="basket", costs=SpotCosts(fee_rate=D(0), slippage_rate=D(0), min_notional=D(1)))
    assert report["decisions"][0]["targets"] == {"BTC": .495, "ETH": .495}
    assert report["fills"][0]["asset"] == "ETH"
    assert D(report["fills"][0]["quantity"]) <= D(".99")
    assert any(order["asset"] == "BTC" and order["status"] == "unfilled" for order in report["orders"])
    assert report["equity"][-1]["cash_usdt"] > 100
