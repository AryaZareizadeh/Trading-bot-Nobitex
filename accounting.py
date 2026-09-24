"""Accounting identities shared by simulated fills (quote-currency amounts)."""


def gross_pnl(direction: str, quantity: float, entry: float, exit_price: float) -> float:
    return float((1 if direction == "Long" else -1) * quantity * (exit_price - entry))


def adverse_fill(price: float, direction: str, entering: bool, slippage_bps: float) -> float:
    buy = (direction == "Long") == entering
    return price * (1 + (1 if buy else -1) * slippage_bps / 10000)


def liquidation_price(direction: str, entry: float, leverage: float, ratio: float) -> float:
    return entry * (ratio - 1 / leverage) if direction == "Long" else entry * (leverage + 1) / (ratio * leverage)
