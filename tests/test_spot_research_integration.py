"""Exercise the chronological research and shared spot simulator end to end."""

from datetime import datetime, timedelta, timezone

from spot_research import run_experiment


def test_synthetic_research_keeps_final_test_separate():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    signal, venue = [], []
    for i in range(1300):
        opened = start + timedelta(hours=i)
        price = 100 + i * .02 + (i % 48) * .03
        base = {"asset": "BTC", "quote": "USDT", "interval": "1h",
                "open_time": opened.isoformat(), "available_at": (opened + timedelta(hours=1)).isoformat(),
                "open": price, "high": price * 1.002, "low": price * .998,
                "close": price, "volume_base": 100, "data_hash": f"{i:064x}"}
        signal.append({**base, "exchange": "binance"})
        venue.append({**base, "exchange": "nobitex",
                      "open_time": (opened + timedelta(minutes=30)).isoformat(),
                      "available_at": (opened + timedelta(hours=1, minutes=30)).isoformat()})
    report = run_experiment({"BTC": signal}, {"BTC": venue})
    assert report["windows"][1] < report["windows"][2] < report["windows"][3]
    assert report["selected"] in {"hgb", "momentum", "lightgbm"}
    assert report["test"]["trade_count"] >= 0
    assert report["benchmarks"]["usdt"]["net_pnl_usdt"] == 0
    assert report["benchmarks"]["basket"]["trade_count"] > 0
    assert report["data_hash"]
