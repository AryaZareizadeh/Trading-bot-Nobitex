import matplotlib.pyplot as plt
import uvicorn


from config import Config
from data import DataFetcher
from indicators import IndicatorSet
from strategy import ComboStrategy, AdvRSICombinedStrategy, ClaudeSuggestedStrategy
from plotting import Chart


def main():
    config = Config()

    print(f"Fetching OHLCV data for {config.symbol}...")
    df = DataFetcher(config).fetch()
    print(f"Loaded {len(df)} candles from {df.index[0]} to {df.index[-1]}")

    indicators = IndicatorSet(df, config)

    # print("\n--- Combined RSI + SRSI strategy ---")
    # combo_trades = ComboStrategy(config).run(df, indicators)
    # print(combo_trades.to_string(index=False))

    print("\n--- Advanced RSI Combined strategy ---")
    adv_trades = AdvRSICombinedStrategy(config).run(df, indicators)
    print(adv_trades.to_string(index=False))

    print("\n--- Claude Suggested strategy ---")
    claude_trades = ClaudeSuggestedStrategy(config).run(df, indicators)
    print(claude_trades.to_string(index=False))

    chart = Chart(df, config)
    chart.plot(
        symbol="ZEC/USDT",
        title="Indicators",
        subplots=[
            chart.srsi_subplot(indicators.stoch_k, indicators.stoch_d),
            chart.rsi_subplot(indicators.rsi, indicators.rsi_ma),
            chart.kd_diff_subplot(indicators.kd_diff),
        ],
    )
    plt.show()


if __name__ == "__main__":
    main()
#     uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)