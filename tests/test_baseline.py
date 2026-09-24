import pandas as pd
import pytest

from config import Config
from data import DataFetcher
from indicators import Indicators
from strategy import (AdvRSICombinedStrategy, AdvRsiParams, ExtremeReversalStrategy,
                      TrendFilteredMomentumStrategy)
from tests.fakes import momentum_candles, synthetic_candles


@pytest.mark.parametrize("strategy_class", [AdvRSICombinedStrategy,
    ExtremeReversalStrategy, TrendFilteredMomentumStrategy])
def test_strategies_are_deterministic_with_input_history(strategy_class):
    candles = momentum_candles() if strategy_class is TrendFilteredMomentumStrategy else synthetic_candles()
    outputs = [strategy_class(Config()).run(candles, Indicators(candles)) for _ in range(2)]
    pd.testing.assert_frame_equal(*outputs)
    assert not outputs[0].empty
    assert {"Position", "P&L ($)", "Start Time"}.issubset(outputs[0].columns)


def test_parameterized_indicators_propagate_and_cache_independently():
    candles = synthetic_candles()
    indicators = Indicators(candles)
    assert indicators.rsi(14) is indicators.rsi(14)
    assert not indicators.rsi(14).equals(indicators.rsi(40))
    params = AdvRsiParams(rsi_length=14, stoch_length=8)
    strategy = AdvRSICombinedStrategy(Config(), params)
    assert strategy.params is params
    changed = strategy.run(candles, indicators)
    baseline = AdvRSICombinedStrategy(Config()).run(candles, indicators)
    assert not changed.equals(baseline)


@pytest.mark.parametrize("exchange", ["nobitex", "binance"])
def test_fetch_deduplicates_sorts_and_excludes_open_candle(monkeypatch, exchange):
    config = Config(interval="1", limit=2)
    config.exchange = exchange
    fetcher = DataFetcher(config)
    timestamps = [120, 60, 120, 180]
    payload = ({"s": "ok", "t": timestamps, "o": [100]*4, "h": [102]*4,
                "l": [99]*4, "c": [101]*4, "v": [10]*4} if exchange == "nobitex"
               else [[t*1000, "100", "102", "99", "101", "10"] for t in timestamps])
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return payload
    monkeypatch.setattr(fetcher._session, "get", lambda *args, **kwargs: Response())
    monkeypatch.setattr("data.time.time", lambda: 200)
    candles = fetcher.fetch()
    assert list(candles.index) == list(pd.to_datetime([60, 120], unit="s"))
    assert candles["Close"].tolist() == [101.0, 101.0]


def test_launcher_never_enables_reload_or_multiple_workers(monkeypatch):
    import main
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    main.main([])
    assert calls[0][0] == ("server:app",)
    assert calls[0][1]["host"] == "127.0.0.1"
    assert calls[0][1]["workers"] == 1
    assert calls[0][1]["reload"] is False


def test_backup_command_preserves_database_and_refuses_overwrite(tmp_path):
    import main
    from storage import Store

    source = Store(tmp_path / "trader.sqlite3")
    source.put("ledger", "trade-1", {"net_pnl": "19.58"})
    output = tmp_path / "backups" / "snapshot.sqlite3"
    main.main(["backup", "--output", str(output)])
    assert Store(output).get("ledger", "trade-1") == {"net_pnl": "19.58"}
    source.put("ledger", "trade-1", {"net_pnl": "20"})
    with pytest.raises(SystemExit):
        main.main(["backup", "--output", str(output)])
    assert Store(output).get("ledger", "trade-1") == {"net_pnl": "19.58"}
