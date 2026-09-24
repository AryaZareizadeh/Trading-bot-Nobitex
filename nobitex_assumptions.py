"""Map documented Nobitex response fields to research execution settings.

These values are snapshots, not historical fees. Other execution settings are
simulator choices and must not be presented as exchange-provided values.
"""

import math


PROFILE_DOC = "https://github.com/nobitex/docs-api/blob/master/source/includes/_user_data.md"
MARGIN_DOC = "https://github.com/nobitex/docs-api/blob/master/source/includes/_position.md"


def _decimal(value, *, maximum=1.0):
    number = float(value)
    if not math.isfinite(number) or not 0 <= number < maximum:
        raise ValueError("Invalid Nobitex rate")
    return number


def profile_fee(profile: dict, symbol: str) -> tuple[float, dict]:
    """The profile options are percentages; the simulator stores fractions."""
    field = "feeUsdt" if symbol.upper().endswith("USDT") else "fee"
    rate = _decimal(profile["options"][field], maximum=100.0) / 100.0
    return rate, {"kind": "api", "endpoint": "GET /users/profile",
                  "field": f"profile.options.{field}", "documentation": PROFILE_DOC}


def market_renewal_rate(markets: dict, symbol: str) -> tuple[float, dict]:
    info = markets[symbol.upper()]
    rate = _decimal(info["positionFeeRate"])
    return rate, {"kind": "api", "endpoint": "GET /margin/markets/list",
                  "field": f"markets.{symbol.upper()}.positionFeeRate", "documentation": MARGIN_DOC}
