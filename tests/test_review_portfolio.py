import json
from datetime import datetime, timedelta, timezone

from portfolio import PortfolioHistory
from storage import Store


def test_performance_keeps_original_baseline_beyond_display_limit(tmp_path):
    store = Store(tmp_path / 'trader.sqlite3')
    history = PortfolioHistory(store)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(5001):
        at = (start + timedelta(hours=index)).isoformat()
        equity = 100 if index == 0 else 200
        snapshot = dict(captured_at=at, equity_usdt=equity, valuation_complete=True)
        rows.append((at, equity, 1, json.dumps(snapshot)))
    with store.connection() as connection:
        connection.executemany('INSERT INTO portfolio_snapshots VALUES(?,?,?,?)', rows)
    result = history.performance()
    assert result['return_pct'] == 100
    assert result['net_pnl_usdt'] == 100
    assert result['period_start'] == start.isoformat()
    assert result['period_end'] == rows[-1][0]
    assert len(result['points']) <= 500
    assert result['points'][0]['time'] == rows[0][0]
    assert result['points'][-1]['time'] == rows[-1][0]


def test_full_performance_retains_flows_and_incomplete_samples(tmp_path):
    history = PortfolioHistory(Store(tmp_path / 'trader.sqlite3'))
    for at, equity, complete in [('2026-01-01T00:00:00+00:00', 100, True),
                                 ('2026-01-02T00:00:00+00:00', 900, False),
                                 ('2026-01-03T00:00:00+00:00', 160, True)]:
        history.save_snapshot(dict(captured_at=at, equity_usdt=equity, valuation_complete=complete))
    history.add_flow(50, '2026-01-02T00:00:00+00:00')
    result = history.performance()
    assert result['return_pct'] == 8
    assert result['net_pnl_usdt'] == 10
    assert len(result['points']) == 2
