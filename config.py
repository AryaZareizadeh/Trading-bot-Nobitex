from dataclasses import dataclass


@dataclass
class Config:
    symbol: str = "zecusdt"
    interval: str = "240"  # Nobitex resolution: minutes as string, or "D"/"W"/"M"
    limit: int = 2000

    rsi_length: int = 40
    stoch_length: int = 36
    k_smooth: int = 7
    d_smooth: int = 5

    rsi_fast_length: int = 14
    rsi_ma_length: int = 7

    rsi_oversold: int = 30
    rsi_overbought: int = 70

    stoch_oversold: int = 22
    stoch_overbought: int = 78

    ema_trend_length: int = 50
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    leverage: int = 5
    trade_size: int = 100
    initial_capital: int = 100

    # Commitment ratio = Total Assets / Debt.
    # Liquidation is triggered when this ratio falls to liquidation_ratio.
    # Long CR  = (asset_value + collateral) / borrowed_usdt
    # Short CR = (sale_proceeds + collateral) / current_debt_value
    liquidation_ratio: float = 1.1
