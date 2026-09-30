from dataclasses import dataclass, field
from position_limits import current_max_hold_days


@dataclass
class Config:
    symbol: str = "zecusdt"
    interval: str = "240"  # Nobitex resolution: minutes as string, or "D"/"W"/"M"
    limit: int = 2000

    # Per-strategy indicator parameters live on each strategy's params dataclass
    # (see strategy.py). These remain only for chart annotation thresholds.
    rsi_oversold: int = 30
    rsi_overbought: int = 70

    leverage: int = 5
    trade_size: int = 100
    initial_capital: int = 100

    # Commitment ratio = Total Assets / Debt.
    # Liquidation is triggered when this ratio falls to liquidation_ratio.
    # Long CR  = (asset_value + collateral) / borrowed_usdt
    # Short CR = (sale_proceeds + collateral) / current_debt_value
    liquidation_ratio: float = 1.1
    max_hold_days: int = field(default_factory=current_max_hold_days)
