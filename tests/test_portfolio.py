"""Portfolio accounting and API boundaries with no network access."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from portfolio import PortfolioHistory, rebalance_ideas, validate_targets, value_portfolio
from storage import Store


def test_valuer_counts_blocked_collateral_once_and_reports_missing_prices():
    spot = {"USDT": {"balance": "1000", "blocked": "0"}, "BTC": {"balance": "1", "blocked": "0"}}
    margin = {"USDT": {"balance": "100", "blocked": "50"}}
    positions = [{"id": 1, "status": "Open", "srcCurrency": "btc", "dstCurrency": "usdt",
                  "side": "buy", "collateral": "50", "delegatedAmount": "1",
                  "markPrice": "100", "unrealizedPNL": "20", "liquidationPrice": "60"}]
    snapshot = value_portfolio(spot, margin, positions, lambda _: 100)
    assert snapshot["equity_usdt"] == 1220
    assert snapshot["margin_usdt"] == 100
    assert snapshot["margin_unrealized_pnl_usdt"] == 20
    assert snapshot["margin_exposure_usdt"] == 100
    missing = value_portfolio({"XYZ": {"balance": "2"}}, {}, [], lambda _: (_ for _ in ()).throw(ValueError()))
    assert missing["valuation_complete"] is False
    assert missing["unpriced"] == ["XYZ"]


def test_snapshot_and_cashflow_performance_survive_restart(tmp_path):
    store = Store(tmp_path / "portfolio.db")
    history = PortfolioHistory(store)
    first = value_portfolio({"USDT": {"balance": "100"}}, {}, [], lambda _: 1,
                            "2026-09-01T00:00:00+00:00")
    second = value_portfolio({"USDT": {"balance": "160"}}, {}, [], lambda _: 1,
                             "2026-09-03T00:00:00+00:00")
    history.save_snapshot(first)
    history.add_flow(50, "2026-09-02T00:00:00+00:00", "external deposit")
    history.save_snapshot(second)
    restored = PortfolioHistory(Store(store.path))
    performance = restored.performance()
    assert performance["net_pnl_usdt"] == 10
    assert performance["return_pct"] == 8.0  # Modified Dietz: 10 / (100 + 25).
    assert performance["max_drawdown_pct"] == 0
    assert len(restored.snapshots()) == 2
    assert restored.delete_flow(restored.flows()[0]["id"])
    assert restored.flows() == []


def test_existing_version_one_database_upgrades_without_losing_records(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript((Path(__file__).resolve().parents[1] /
                            "migrations" / "001_initial.sql").read_text())
        conn.execute("INSERT INTO records(namespace,key,value) VALUES('bots','saved','{}')")
    store = Store(path)
    assert store.get("bots", "saved") == {}
    with store.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_rebalance_limits_by_cash_depth_and_cost():
    snapshot = value_portfolio({"USDT": {"balance": "900"}, "BTC": {"balance": "1"}}, {}, [], lambda _: 100)
    targets = validate_targets({"BTC": .30})
    ideas = rebalance_ideas(snapshot, targets, lambda _: {"asks": [["100", "1"]] * 5, "bids": [["99", "1"]]})
    assert len(ideas) == 1
    assert ideas[0]["side"] == "buy"
    assert ideas[0]["notional_usdt"] == 100  # 10% portfolio turnover cap.
    assert ideas[0]["estimated_cost_usdt"] == .4
    assert rebalance_ideas(snapshot, targets, lambda _: {}) == []
    two_assets = value_portfolio({"USDT": {"balance": "900"}, "BTC": {"balance": "1"},
                                  "ETH": {"balance": "1"}}, {}, [], lambda _: 100)
    joint = rebalance_ideas(two_assets, validate_targets({"BTC": .3, "ETH": .3}),
                            lambda _: {"asks": [["100", "10"]], "bids": [["99", "10"]]})
    assert sum(row["notional_usdt"] for row in joint) <= 110


def test_portfolio_routes_are_authenticated(tmp_path):
    from auth import AuthState, hash_password
    from bot_manager import BotManager
    from server import create_app

    class Exchange:
        def wallets(self, kind):
            return {"USDT": {"balance": "900"}, "BTC": {"balance": "1"}} if kind == "spot" else {"USDT": {"balance": "100", "blocked": "50"}}

        def positions(self, status):
            return []

        def live_price(self, symbol):
            return 100

        def orderbook(self, symbol):
            return {"asks": [["100", "10"]], "bids": [["99", "10"]]}

    manager = BotManager(Store(tmp_path / "api.db"), tmp_path)
    app = create_app(manager=manager, exchange_client=Exchange(), offline=True)
    app.state.auth = AuthState(password_hash=hash_password("test password sufficiently long"), allowed_hosts={"testserver"})
    with TestClient(app) as client:
        assert client.get("/api/portfolio").status_code == 401
        login = client.post("/api/auth/login", json={"password": "test password sufficiently long"})
        headers = {"X-CSRF-Token": login.json()["csrf_token"], "Origin": "http://testserver"}
        assert client.get("/api/portfolio").json()["snapshot"]["equity_usdt"] == 1100
        assert client.post("/api/portfolio/cashflows", json={"kind": "deposit", "amount_usdt": 50}).status_code == 403
        assert client.post("/api/portfolio/cashflows", headers=headers,
                           json={"kind": "deposit", "amount_usdt": 50}).status_code == 200
        assert client.post("/api/portfolio/targets", headers=headers,
                           json={"targets": {"BTC": .3}}).status_code == 200
        view = client.get("/api/portfolio").json()
        assert view["targets"] == {"BTC": .3}
        assert view["rebalance_ideas"][0]["notional_usdt"] == 100
