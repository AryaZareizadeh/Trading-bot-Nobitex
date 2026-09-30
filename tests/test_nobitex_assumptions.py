from fastapi.testclient import TestClient
import pytest

from auth import AuthState, hash_password
from bot_manager import BotManager
from nobitex_assumptions import market_renewal_rate, profile_fee
from server import create_app
from storage import Store
from tests.test_dashboard_flow import login


def client_for(tmp_path, exchange):
    manager = BotManager(Store(tmp_path / "trader.sqlite3"), tmp_path)
    app = create_app(manager=manager, exchange_client=exchange, offline=True)
    app.state.auth = AuthState(password_hash=hash_password("test password sufficiently long"),
                               allowed_hosts={"testserver"})
    return TestClient(app)


class Exchange:
    def profile(self):
        return {"options": {"feeUsdt": "0.2", "fee": "0.35"},
                "email": "must-not-leak@example.com"}

    def margin_markets(self):
        return {"BTCUSDT": {"positionFeeRate": "0.001"},
                "BTCIRT": {"positionFeeRate": "0.002"}}


def test_documented_fee_units_and_market_renewal_are_distinct():
    exchange = Exchange()
    assert profile_fee(exchange.profile(), "BTCUSDT")[0] == pytest.approx(0.002)
    assert profile_fee(exchange.profile(), "BTCIRT")[0] == pytest.approx(0.0035)
    assert market_renewal_rate(exchange.margin_markets(), "BTCUSDT")[0] == pytest.approx(0.001)
    with pytest.raises(ValueError):
        profile_fee({"options": {"feeUsdt": "NaN"}}, "BTCUSDT")


def test_margin_market_limits_keep_account_values_and_mark_fallback_unverified(tmp_path):
    class Markets(Exchange):
        def margin_markets(self):
            return {
                "BTCUSDT": {"srcCurrency": "btc", "dstCurrency": "usdt", "maxLeverage": "2.5",
                            "buyEnabled": True, "sellEnabled": True},
                "DOGEUSDT": {"srcCurrency": "doge", "dstCurrency": "usdt", "maxLeverage": "3",
                             "buyEnabled": True, "sellEnabled": True},
            }

    with client_for(tmp_path / "live", Markets()) as client:
        login(client)
        response = client.get("/api/margin-markets").json()
        assert response["source"] == "live_auth"
        assert {market["symbol"]: market["maxLeverage"] for market in response["markets"]} == {
            "BTCUSDT": 2.5, "DOGEUSDT": 3.0}

    class Unavailable(Exchange):
        def margin_markets(self):
            raise RuntimeError("exchange unavailable")

    with client_for(tmp_path / "fallback", Unavailable()) as client:
        login(client)
        response = client.get("/api/margin-markets").json()
        assert response["source"] == "fallback"
        assert all(market["maxLeverage"] is None for market in response["markets"])


def test_selected_market_uses_live_delegation_tiers_instead_of_market_listing(tmp_path):
    class Markets(Exchange):
        def margin_markets(self):
            return {"ZECUSDT": {"srcCurrency": "zec", "dstCurrency": "usdt",
                                "maxLeverage": "5", "buyEnabled": True, "sellEnabled": True}}

        def delegation_limit(self, market):
            assert market == "ZECUSDT"
            return {"buy": [{"leverage": "5", "limit": "100"},
                            {"leverage": "10", "limit": "25"}],
                    "sell": [{"leverage": "5", "limit": "1"},
                             {"leverage": "10", "limit": "0.2"},
                             {"leverage": "12", "limit": "0"}]}

    with client_for(tmp_path, Markets()) as client:
        login(client)
        listing = client.get("/api/margin-markets").json()
        assert listing["markets"][0]["maxLeverage"] == 5
        response = client.get("/api/margin-markets/ZECUSDT/leverage")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "symbol": "ZECUSDT", "buyMax": 10,
                                   "sellMax": 10, "source": "delegation_limit"}


def test_research_autofill_uses_live_fields_without_exposing_profile(tmp_path):
    client = client_for(tmp_path, Exchange())
    with client:
        login(client)
        response = client.get("/api/settings/execution/nobitex?symbol=btcusdt")
        assert response.status_code == 200, response.text
        result = response.json()
        from position_limits import current_max_hold_days
        assert result["values"].pop("max_hold_days") == current_max_hold_days()
        assert result["values"] == {
            "entry_fee_rate": 0.002, "exit_fee_rate": 0.002,
            "holding_rate_per_day": 0.001,
            "holding_policy": "entry_notional_daily", "liquidation_ratio": 1.1,
        }
        assert result["sources"]["entry_fee_rate"]["field"] == "profile.options.feeUsdt"
        assert result["sources"]["holding_rate_per_day"]["field"] == "markets.BTCUSDT.positionFeeRate"
        assert "must-not-leak" not in response.text
        assert "slippage_bps" not in result["values"]
        assert "margin_fraction" not in result["values"]
        assert client.get("/api/settings/execution").json()["entry_fee_rate"] == 0


def test_research_autofill_keeps_available_rate_when_profile_fails(tmp_path):
    class Partial(Exchange):
        def profile(self):
            raise RuntimeError("private upstream detail")

    client = client_for(tmp_path, Partial())
    with client:
        login(client)
        response = client.get("/api/settings/execution/nobitex?symbol=BTCUSDT")
        assert response.status_code == 200
        assert "entry_fee_rate" not in response.json()["values"]
        assert response.json()["values"]["holding_rate_per_day"] == 0.001
        assert "private upstream detail" not in response.text


def test_research_autofill_omits_unknown_market_rate(tmp_path):
    client = client_for(tmp_path, Exchange())
    with client:
        login(client)
        response = client.get("/api/settings/execution/nobitex?symbol=ETHUSDT")
        assert response.status_code == 200
        assert response.json()["values"]["entry_fee_rate"] == 0.002
        assert "holding_rate_per_day" not in response.json()["values"]
        assert client.get("/api/settings/execution/nobitex?symbol=../../bad").status_code == 400


def test_research_autofill_failure_does_not_change_saved_assumptions(tmp_path):
    class Unavailable(Exchange):
        def profile(self):
            raise RuntimeError("profile unavailable")

        def margin_markets(self):
            raise RuntimeError("markets unavailable")

    client = client_for(tmp_path, Unavailable())
    with client:
        headers = login(client)
        saved = client.post("/api/settings/execution", headers=headers,
                            json={"entry_fee_rate": 0.004, "slippage_bps": 8}).json()
        response = client.get("/api/settings/execution/nobitex?symbol=BTCUSDT")
        assert response.status_code == 503
        assert client.get("/api/settings/execution").json() == saved
