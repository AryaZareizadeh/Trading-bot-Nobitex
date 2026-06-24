import matplotlib.pyplot as plt
import uvicorn


from config import Config
from data import DataFetcher
from indicators import Indicators
from strategy import (
    AdvRSICombinedStrategy,
    AdvRsiParams,
    ExtremeReversalStrategy,
    TrendFilteredMomentumStrategy,
)
from plotting import Chart


def main():
    config = Config()

    print(f"Fetching OHLCV data for {config.symbol}...")
    df = DataFetcher(config).fetch()
    print(f"Loaded {len(df)} candles from {df.index[0]} to {df.index[-1]}")

    indicators = Indicators(df)

    print("\n--- Advanced RSI Combined strategy ---")
    adv_trades = AdvRSICombinedStrategy(config).run(df, indicators)
    print(adv_trades.to_string(index=False))

    print("\n--- Trend-Filtered Momentum strategy ---")
    trend_trades = TrendFilteredMomentumStrategy(config).run(df, indicators)
    print(trend_trades.to_string(index=False))

    print("\n--- Extreme Reversal strategy ---")
    extreme_trades = ExtremeReversalStrategy(config).run(df, indicators)
    print(extreme_trades.to_string(index=False))

    # Chart series, derived with the same parameters the strategies use.
    p = AdvRsiParams()
    srsi = indicators.stoch_rsi(p.rsi_length, p.stoch_length, p.k_smooth, p.d_smooth)
    rsi, rsi_ma = indicators.rsi_with_ma(p.rsi_fast_length, p.rsi_ma_length)
    kd_diff = (srsi.k - srsi.d).fillna(0)

    chart = Chart(df, config)
    chart.plot(
        symbol="ZEC/USDT",
        title="Indicators",
        subplots=[
            chart.srsi_subplot(srsi.k, srsi.d),
            chart.rsi_subplot(rsi, rsi_ma),
            chart.kd_diff_subplot(kd_diff),
        ],
    )
    plt.show()


if __name__ == "__main__":
    main()
#     uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)