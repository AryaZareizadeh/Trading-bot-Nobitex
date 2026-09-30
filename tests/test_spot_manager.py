"""Spot manager invariants; all market data and execution are synthetic."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from spot_data import audit_candles, normalize_candles
from spot_paper import SpotPaperService
from spot_portfolio import D, SpotCosts, SpotRisk, execute_targets, external_flow, mark, new_state, target_weights
from spot_research import _matrix, feature_frame
from storage import Store


def quote(price=100, depth=1000):
    return {"bid": D(price) * D("0.995"), "ask": D(price) * D("1.005"),
            "bid_depth": D(depth), "ask_depth": D(depth)}


def test_source_schema_and_gap_are_explicit():
    t = 1_750_000_000
    binance = normalize_candles("binance", "BTC", "1h", [[t * 1000, "10", "12", "9", "11", "4"]], t + 7200)[0]
    kucoin = normalize_candles("kucoin", "HYPE", "1h", [[t, "10", "11", "12", "9", "4", "44"]], t + 7200)[0]
    bitfinex = normalize_candles("bitfinex", "XAUT", "1h", [[t * 1000, "10", "11", "12", "9", "4"]], t + 7200)[0]
    for bar in (binance, kucoin, bitfinex):
        assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (10, 12, 9, 11)
        assert bar["quote"] == "USDT"
        assert len(bar["data_hash"]) == 64
    later = normalize_candles("binance", "BTC", "1h", [[(t + 7200) * 1000, "10", "12", "9", "11", "4"]], t + 14400)[0]
    audit = audit_candles([binance, binance, later], "1h", as_of=t + 14400)
    assert [issue["kind"] for issue in audit["issues"]] == ["duplicate", "gap"]
    assert audit["issues"][1]["missing_intervals"] == 1


def test_shared_cash_fees_minimum_and_rounding():
    state = new_state(200, ["BTC", "ETH"])
    costs = SpotCosts(fee_rate=D("0.01"), slippage_rate=D("0"), min_notional=D(11))
    risk = SpotRisk(max_turnover_fraction=D(1))
    execute_targets(state, {"BTC": D("0.6"), "ETH": D("0.4")},
                    {"BTC": quote(), "ETH": quote()}, {"BTC": 100, "ETH": 100},
                    costs=costs, risk=risk, quantity_steps={"BTC": "0.001", "ETH": "0.001"})
    cash = D(state["cash_usdt"])
    assert cash >= 0
    assert len(state["fills"]) == 2
    assert cash + sum(D(fill["filled_usdt"]) + D(fill["fee_usdt"]) for fill in state["fills"]) <= D(200) + D("0.0000001")
    marked = mark(state, {"BTC": 100, "ETH": 100})
    assert marked["equity_usdt"] < 200
    assert all(D(fill["quantity"]) % D("0.001") == 0 for fill in state["fills"])
    assert all(fill["estimated"] for fill in state["fills"])


def test_switch_pays_sell_and_buy_and_depth_can_block():
    state = new_state(200, ["BTC", "ETH"])
    costs = SpotCosts(fee_rate=D("0.01"), slippage_rate=D(0), min_notional=D(11), max_book_fraction=D(1))
    risk = SpotRisk(max_turnover_fraction=D(1))
    execute_targets(state, {"BTC": D("0.5")}, {"BTC": quote()}, {"BTC": 100}, costs=costs, risk=risk)
    first_fees = sum(fill["fee_usdt"] for fill in state["fills"])
    execute_targets(state, {"ETH": D("0.5")}, {"BTC": quote(), "ETH": quote()},
                    {"BTC": 100, "ETH": 100}, costs=costs, risk=risk)
    assert {fill["side"] for fill in state["fills"]} == {"buy", "sell"}
    assert sum(fill["fee_usdt"] for fill in state["fills"]) > first_fees * 2
    count = len(state["fills"])
    execute_targets(state, {"BTC": D("0.5")}, {"BTC": quote(depth=1), "ETH": quote()},
                    {"BTC": 100, "ETH": 100}, costs=costs, risk=risk)
    assert any(order["status"] == "unfilled" for order in state["orders"])
    assert len(state["fills"]) >= count


def test_portfolio_drawdown_reduces_and_halts_new_exposure():
    state = new_state(200, ["BTC"])
    state["cash_usdt"] = "0"
    state["holdings"] = {"BTC": "2"}
    mark(state, {"BTC": 100})
    at_seven = mark(state, {"BTC": 93})
    weights, _ = target_weights({"BTC": {"return": .1, "downside": .01}}, state,
                                at_seven["equity_usdt"], now=1000)
    assert sum(weights.values()) <= D("0.25")
    at_ten = mark(state, {"BTC": 90})
    weights, reason = target_weights({"BTC": {"return": .1, "downside": .01}}, state,
                                     at_ten["equity_usdt"], now=1001)
    assert weights == {}
    assert "drawdown" in reason
    assert at_ten["drawdown_pct"] == 10
    cautious, _ = target_weights({"BTC": {"return": .1, "downside": .01}}, state,
                                 at_ten["equity_usdt"], now=1001 + 86401)
    assert sum(cautious.values()) <= D("0.10")


def test_external_flow_does_not_create_a_false_peak_or_return():
    state = new_state(200, ["BTC"])
    mark(state, {})
    external_flow(state, 50)
    after = mark(state, {})
    assert after["equity_usdt"] == 250
    assert after["drawdown_pct"] == 0
    assert D(state["peak_adjusted_usdt"]) == 200
    external_flow(state, -30)
    assert mark(state, {})["drawdown_pct"] == 0


def test_features_and_labels_respect_completion_and_purged_boundary():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    for i in range(220):
        opening = start + timedelta(hours=i)
        candles.append({"open_time": opening.isoformat(), "available_at": (opening + timedelta(hours=1)).isoformat(),
                        "open": 100 + i, "high": 101 + i, "low": 99 + i,
                        "close": 100 + i, "volume_base": 10})
    frame = feature_frame(candles)
    boundary = pd.Timestamp(start + timedelta(hours=180))
    rows = _matrix({"BTC": frame}, frame.index[0], boundary, 24, training=True)
    assert not rows.empty
    assert (rows["future_24"] < boundary).all()
    old = frame.loc[frame.index[100], "return_24"]
    candles[-1]["close"] = 999999
    assert feature_frame(candles).loc[frame.index[100], "return_24"] == old


def test_paper_state_survives_restart_and_is_tenant_local(tmp_path):
    first = SpotPaperService(Store(tmp_path / "user1" / "data.db"))
    second = SpotPaperService(Store(tmp_path / "user2" / "data.db"))
    first.start(["BTC"], 200)
    assert second.state() is None
    restarted = SpotPaperService(Store(first.store.path))
    restarted.pause_on_restart()
    assert restarted.state()["running"] is False
    assert restarted.state()["cash_usdt"] == "200"
    restarted.start(["BTC"], 200)
    assert restarted.state()["running"] is True
    with pytest.raises(ValueError):
        restarted.start(["ETH"], 200)


def test_manager_api_requires_auth_and_never_needs_exchange_credentials(tmp_path):
    from fastapi.testclient import TestClient
    from auth import AuthState, hash_password
    from bot_manager import BotManager
    from server import create_app

    manager = BotManager(Store(tmp_path / "api.db"), tmp_path)
    app = create_app(manager=manager, offline=True)
    app.state.auth = AuthState(password_hash=hash_password("test password sufficiently long"),
                               allowed_hosts={"testserver"})
    with TestClient(app) as client:
        assert client.get("/api/portfolio-manager").status_code == 401
        login = client.post("/api/auth/login", json={"password": "test password sufficiently long"})
        headers = {"X-CSRF-Token": login.json()["csrf_token"], "Origin": "http://testserver"}
        assert client.post("/api/portfolio-manager/paper/start",
                           json={"assets": ["BTC"], "budget_usdt": 200}).status_code == 403
        started = client.post("/api/portfolio-manager/paper/start", headers=headers,
                              json={"assets": ["BTC"], "budget_usdt": 200})
        assert started.status_code == 200
        assert started.json()["paper"]["cash_usdt"] == "200.0"
        assert client.get("/api/portfolio-manager").json()["paper"]["assets"] == ["BTC"]
        assert client.post("/api/portfolio-manager/paper/stop", headers=headers).json()["paper"]["running"] is False


def test_live_book_paper_tick_persists_estimated_fill_once(tmp_path, monkeypatch):
    import joblib
    from spot_research import RESEARCH_VERSION

    boundary = datetime(2026, 9, 27, 16, tzinfo=timezone.utc)
    now = boundary.timestamp() + 60
    monkeypatch.setattr("spot_paper.time.time", lambda: now)
    bars = []
    for i in range(120):
        opened = boundary - timedelta(hours=120-i)
        price = 100 + i
        bars.append({"asset": "BTC", "exchange": "binance", "quote": "USDT", "interval": "1h",
                     "open_time": opened.isoformat(), "available_at": (opened + timedelta(hours=1)).isoformat(),
                     "open": price, "high": price + 1, "low": price - 1, "close": price,
                     "volume_base": 100, "data_hash": f"{i:064x}"})

    class Signals:
        def fetch(self, *args, **kwargs):
            return {"valid": len(bars), "candles": bars}

    class Venue:
        def books(self):
            return {"BTC": {"lastUpdate": (now - 5) * 1000,
                            "asks": [["220", "10"]], "bids": [["219", "10"]]}}

        def rules(self):
            return {"min_notional": "0.5", "quantity_steps": {"BTC": "0.000001"}}

    store = Store(tmp_path / "user" / "state.db")
    service = SpotPaperService(store, signals=Signals(), venue=Venue())
    service.start(["BTC"], 200)
    joblib.dump({"version": RESEARCH_VERSION, "method": "momentum", "models": None,
                 "assets": ["BTC"]}, service.model_path)
    first = service.tick()
    assert first["ok"] is True
    assert len(first["state"]["fills"]) == 1
    assert first["state"]["fills"][0]["estimated"] is True
    restarted = SpotPaperService(Store(store.path), signals=Signals(), venue=Venue())
    assert len(restarted.state()["fills"]) == 1
    assert restarted.tick()["reason"] == "Already processed this closed hour"
