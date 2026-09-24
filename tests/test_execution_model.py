import pandas as pd
import pytest


def candles(prices):
    return pd.DataFrame({"Open": prices, "High": prices, "Low": prices,
                         "Close": prices, "Volume": 1.0},
                        index=pd.date_range("2025-01-01", periods=len(prices), freq="D"))


def simulate(df, entries, exits=None, **kwargs):
    from execution_model import simulate_signals
    return simulate_signals(df, lambda i, positions: (entries.get(i), (exits or {}).get(i, [])), **kwargs)


@pytest.mark.parametrize("direction,prices", [("Long", [90, 100, 110, 110]), ("Short", [120, 110, 100, 100])])
def test_frozen_quantity_cost_oracle_and_first_trade_retained(direction, prices):
    # A margin fraction fixes quantity at exactly 2 even though fees reduce cash.
    result = simulate(candles(prices), {0: direction}, {2: [direction]}, credit=1000,
                      leverage=1, execution={"margin_fraction": prices[1]*2/1000,
                      "entry_fee_rate": .001, "exit_fee_rate": .001})
    trade = result["trades"][0]
    assert trade["Quantity"] == pytest.approx(2)
    assert trade["Gross P&L ($)"] == pytest.approx(20)
    assert trade["Fees ($)"] == pytest.approx(.42)
    assert trade["P&L ($)"] == pytest.approx(19.58)
    assert result["summary"]["trade_count"] == 1


def test_next_open_final_signal_and_pre_entry_range():
    df = candles([100, 100, 100])
    df.loc[df.index[0], "Low"] = 1
    result = simulate(df, {0: "Long", 2: "Long"}, credit=100, leverage=5)
    assert len(result["open_positions"]) == 1
    assert result["trades"] == []
    assert result["open_positions"][0]["entry_time"] == str(df.index[1])


@pytest.mark.parametrize("direction,extreme,gap,expected", [("Long", "Low", 80, 80), ("Short", "High", 130, 130)])
def test_intrabar_and_gap_liquidation(direction, extreme, gap, expected):
    df = candles([100, 100, 100])
    df.loc[df.index[2], extreme] = gap
    result = simulate(df, {0: direction}, credit=100, leverage=5)
    assert result["trades"][0]["Liquidated"] is True
    df.loc[df.index[2], "Open"] = gap
    result = simulate(df, {0: direction}, credit=100, leverage=5)
    assert float(result["trades"][0]["End Price"]) == pytest.approx(expected)


def test_reservations_holding_costs_equity_and_slippage():
    result = simulate(candles([100]*5), {0: "Long", 1: "Long"}, {3: ["Long"]}, credit=100,
                      leverage=2, execution={"position_mode": "multi", "slippage_bps": 10,
                      "holding_policy": "entry_notional_daily", "holding_rate_per_day": .001})
    assert result["summary"]["rejected_entries"] == 1
    trade = result["trades"][0]
    assert float(trade["Start Price"]) > 100
    assert float(trade["End Price"]) < 100
    assert trade["Borrowing ($)"] == pytest.approx(.6)
    assert result["summary"]["max_drawdown_pct"] > 0


def test_heldout_starts_flat_and_rejects_invalid_candles():
    result = simulate(candles([100]*5), {0: "Long", 2: "Short"}, start_idx=2, credit=100, leverage=1)
    assert len(result["open_positions"]) == 1
    assert result["open_positions"][0]["direction"] == "Short"
    bad = candles([100, 0])
    with pytest.raises(ValueError, match="positive"):
        simulate(bad, {}, credit=100, leverage=1)


def test_trade_balance_is_cash_including_starting_capital():
    result = simulate(candles([100, 100, 110, 110]), {0: "Long"}, {2: ["Long"]},
                      credit=1000, leverage=1, execution={"margin_fraction": .2,
                      "entry_fee_rate": .001, "exit_fee_rate": .001})
    assert result["trades"][0]["Balance ($)"] == pytest.approx(1019.58)
    assert result["equity_curve"][-1]["balance"] == pytest.approx(1019.58)


def test_full_capital_entry_reserves_fee_allowance_and_keeps_frozen_quantity():
    result = simulate(candles([100, 100, 110, 110]), {0: "Long"}, {2: ["Long"]},
                      credit=100, leverage=2,
                      execution={"entry_fee_rate": .01, "exit_fee_rate": .01})
    trade = result["trades"][0]
    assert trade["Quantity"] == pytest.approx(200 / 1.02 / 100)
    assert result["equity_curve"][1]["available"] == pytest.approx(0)
    assert trade["Gross P&L ($)"] == pytest.approx(trade["Quantity"] * 10)


def test_zero_cost_defaults_are_explicitly_labeled():
    result = simulate(candles([100]), {}, credit=100, leverage=1)
    assert result["metadata"]["costs_configured"] is False
    assert "zero" in result["metadata"]["cost_assumption"].lower()


def test_nan_candle_timestamp_is_rejected():
    df = candles([100])
    df.index = pd.DatetimeIndex([pd.NaT])
    with pytest.raises(ValueError, match="DatetimeIndex"):
        simulate(df, {}, credit=100, leverage=1)
