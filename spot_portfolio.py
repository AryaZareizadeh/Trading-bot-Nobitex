"""Isolated spot-only accounting, allocation and simulated execution.

Both historical replay and live paper use this ledger. No method submits an order.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import math
import time

from spot_data import APPROVED_ASSETS


def D(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite monetary value")
    return result


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SpotCosts:
    fee_rate: Decimal = Decimal("0.0025")
    slippage_rate: Decimal = Decimal("0.001")
    min_notional: Decimal = Decimal("11.01")
    max_book_fraction: Decimal = Decimal("0.25")


@dataclass(frozen=True)
class SpotRisk:
    drawdown_target: Decimal = Decimal("0.10")
    reduce_at: Decimal = Decimal("0.07")
    max_asset_weight: Decimal = Decimal("0.40")
    max_turnover_fraction: Decimal = Decimal("0.50")
    no_trade_fraction: Decimal = Decimal("0.02")
    reentry_seconds: int = 86400


def new_state(budget=200, assets=APPROVED_ASSETS):
    budget = D(budget)
    if budget <= 0:
        raise ValueError("Budget must be positive")
    selected = tuple(dict.fromkeys(str(asset).upper() for asset in assets))
    if not selected or any(asset not in APPROVED_ASSETS for asset in selected):
        raise ValueError("Select approved risky assets")
    return {"budget_usdt": str(budget), "assets": list(selected), "cash_usdt": str(budget),
            "holdings": {}, "peak_adjusted_usdt": str(budget), "flow_offset_usdt": "0",
            "halted_at": None, "halt_floor_usdt": None, "running": False, "orders": [], "fills": [],
            "decisions": [], "equity": [], "model_version": None}


def mark(state, prices, at=None, *, liquidation_prices=None):
    """A missing price for any owned asset makes the valuation incomplete."""
    holdings = {asset: qty for asset, qty in state["holdings"].items() if D(qty) != 0}
    missing = [asset for asset, qty in holdings.items()
               if D(qty) > 0 and (asset not in prices or D(prices[asset]) <= 0)]
    if missing:
        return {"at": at or _now(), "complete": False, "unpriced": missing}
    cash = D(state["cash_usdt"])
    value = cash + sum((D(qty) * D(prices[asset]) for asset, qty in holdings.items()), Decimal(0))
    liquidation = None
    if liquidation_prices is not None and all(asset in liquidation_prices for asset in holdings):
        liquidation = cash + sum((D(qty) * D(liquidation_prices[asset]) for asset, qty in holdings.items()), Decimal(0))
    adjusted = value - D(state["flow_offset_usdt"])
    peak = max(D(state["peak_adjusted_usdt"]), adjusted)
    state["peak_adjusted_usdt"] = str(peak)
    drawdown = (peak - adjusted) / peak if peak > 0 else Decimal(0)
    point = {"at": at or _now(), "complete": True, "equity_usdt": float(value),
             "liquidation_usdt": float(liquidation) if liquidation is not None else None,
             "drawdown_pct": float(drawdown * 100), "cash_usdt": float(cash)}
    state["equity"].append(point)
    return point


def external_flow(state, amount):
    amount = D(amount)
    if D(state["cash_usdt"]) + amount < 0:
        raise ValueError("Insufficient virtual cash")
    state["cash_usdt"] = str(D(state["cash_usdt"]) + amount)
    state["flow_offset_usdt"] = str(D(state["flow_offset_usdt"]) + amount)


def target_weights(forecasts, state, equity, risk=SpotRisk(), costs=SpotCosts(), *, now=None):
    """Cost-aware positive-edge allocation; leftover weight always stays in USDT.

    Forecasts are {asset: {return: fraction, downside: fraction}}. The policy
    ignores uncalibrated probability language and never spends more than equity.
    """
    now = now or time.time()
    adjusted = D(equity) - D(state["flow_offset_usdt"])
    peak = D(state["peak_adjusted_usdt"])
    drawdown = (peak - adjusted) / peak if peak > 0 else Decimal(0)
    if drawdown >= risk.drawdown_target and state["halted_at"] is None:
        state["halted_at"] = now
        state["halt_floor_usdt"] = str(adjusted)
        return {}, "portfolio drawdown target reached; reduce exposure"
    if state["halted_at"] is not None:
        if drawdown <= risk.reduce_at / 2:
            state["halted_at"] = None
            state["halt_floor_usdt"] = None
        elif adjusted < D(state["halt_floor_usdt"]) * D("0.98"):
            state["halted_at"] = now
            state["halt_floor_usdt"] = str(adjusted)
            return {}, "further loss during cautious re-entry"
        elif now - float(state["halted_at"]) < risk.reentry_seconds:
            return {}, "drawdown cooldown"
    cap = Decimal("0.10") if state["halted_at"] is not None else Decimal("0.25") if drawdown >= risk.reduce_at else Decimal(1)
    candidates = []
    for asset in state["assets"]:
        prediction = forecasts.get(asset)
        if not prediction:
            continue
        expected = D(prediction["return"])
        downside = max(D(prediction["downside"]), Decimal("0.001"))
        # The buffer covers both sides of a complete USDT-routed switch.
        net = expected - 2 * (costs.fee_rate + costs.slippage_rate)
        if net > 0:
            candidates.append((net / downside, asset, net))
    candidates.sort(reverse=True)
    weights = {}
    remaining = cap
    for _, asset, net in candidates:
        if remaining <= 0:
            break
        weight = min(remaining, risk.max_asset_weight, max(Decimal("0.05"), net / Decimal("0.05")))
        weights[asset] = weight
        remaining -= weight
    return weights, "positive forecast after round-trip costs" if weights else "no forecast clears costs"


def book_quote(book, *, at=None):
    """Validate Nobitex USDT depth, returning a conservative executable quote."""
    timestamp = float(book["lastUpdate"]) / 1000
    if (at or time.time()) - timestamp > 60 or timestamp > (at or time.time()) + 5:
        raise ValueError("Stale Nobitex order book")
    asks, bids = book["asks"], book["bids"]
    if not asks or not bids:
        raise ValueError("Empty Nobitex order book")
    ask, bid = D(asks[0][0]), D(bids[0][0])
    if bid <= 0 or ask < bid or (ask - bid) / ask > Decimal("0.03"):
        raise ValueError("Invalid or excessive spread")
    return {"ask": ask, "bid": bid,
            "ask_depth": sum((D(p) * D(q) for p, q in asks[:5]), Decimal(0)),
            "bid_depth": sum((D(p) * D(q) for p, q in bids[:5]), Decimal(0)),
            "at": timestamp}


def execute_targets(state, weights, quotes, prices, *, costs=SpotCosts(), risk=SpotRisk(),
                    quantity_steps=None, at=None, reason="allocation decision", model_version=None,
                    liquidation_prices=None):
    """Apply a single shared budget, sell before buy, and persist every estimate.

    Quotes supply bid/ask and visible notional depth. Unfilled remainder is
    recorded as cancelled. An unavailable quote prevents any new risk-taking.
    """
    at = at or _now()
    before = mark(state, prices, at, liquidation_prices=liquidation_prices)
    if not before["complete"]:
        state["decisions"].append({"at": at, "reason": "missing Nobitex valuation", "rejected": before["unpriced"]})
        return before
    equity = D(before["equity_usdt"])
    owned = {a for a, q in state["holdings"].items() if D(q) > 0}
    requested = {a for a, w in weights.items() if D(w) > 0}
    if any(a not in state["assets"] or D(w) < 0 for a, w in weights.items()) or sum((D(w) for w in weights.values()), Decimal(0)) > 1:
        raise ValueError("Invalid target weights")
    missing = sorted((owned | requested) - set(quotes) | ((owned | requested) - set(prices)))
    if missing:
        state["decisions"].append({"at": at, "reason": "missing executable Nobitex book; no orders", "rejected": missing})
        return before
    steps = quantity_steps or {}
    turnover_left = equity * risk.max_turnover_fraction
    changes = []
    for asset in owned | requested:
        current = D(state["holdings"].get(asset, 0)) * D(prices[asset])
        gap = equity * D(weights.get(asset, 0)) - current
        if abs(gap) >= max(costs.min_notional, equity * risk.no_trade_fraction):
            changes.append((asset, gap))
    changes.sort(key=lambda item: (item[1] >= 0, -abs(item[1]), item[0]))
    for asset, gap in changes:
        side = "buy" if gap > 0 else "sell"
        quote = quotes[asset]
        price = D(quote["ask"] if side == "buy" else quote["bid"])
        depth = D(quote["ask_depth"] if side == "buy" else quote["bid_depth"])
        if price <= 0 or depth <= 0 or D(quote["ask"]) < D(quote["bid"]):
            state["orders"].append({"at": at, "asset": asset, "side": side, "status": "rejected", "reason": "invalid depth"})
            continue
        price *= 1 + costs.slippage_rate if side == "buy" else 1 - costs.slippage_rate
        requested_notional = min(abs(gap), turnover_left)
        executable = min(requested_notional, depth * costs.max_book_fraction)
        if side == "buy":
            executable = min(executable, D(state["cash_usdt"]) / (1 + costs.fee_rate))
        else:
            executable = min(executable, D(state["holdings"].get(asset, 0)) * price)
        step = D(steps.get(asset, "0.00000001"))
        if step <= 0:
            raise ValueError("Invalid quantity step")
        qty = (executable / price / step).to_integral_value(rounding=ROUND_DOWN) * step
        notional = qty * price
        status = "filled" if notional >= requested_notional - step * price else "partial"
        if notional < costs.min_notional:
            state["orders"].append({"at": at, "asset": asset, "side": side, "status": "unfilled", "reason": "minimum notional, cash or depth"})
            continue
        fee = notional * costs.fee_rate
        if side == "buy":
            state["cash_usdt"] = str(D(state["cash_usdt"]) - notional - fee)
            state["holdings"][asset] = str(D(state["holdings"].get(asset, 0)) + qty)
        else:
            state["cash_usdt"] = str(D(state["cash_usdt"]) + notional - fee)
            state["holdings"][asset] = str(D(state["holdings"].get(asset, 0)) - qty)
        order = {"at": at, "asset": asset, "side": side, "status": status,
                 "requested_usdt": float(requested_notional), "filled_usdt": float(notional),
                 "quantity": str(qty), "price_usdt": str(price), "fee_usdt": float(fee),
                 "reason": reason, "estimated": True, "model_version": model_version}
        state["orders"].append(order)
        state["fills"].append(order.copy())
        turnover_left -= notional
        if turnover_left < costs.min_notional:
            break
    state["model_version"] = model_version
    state["decisions"].append({"at": at, "reason": reason, "targets": {a: float(D(w)) for a, w in weights.items()},
                               "orders": len(state["orders"]), "model_version": model_version})
    return mark(state, prices, at, liquidation_prices=liquidation_prices)


def plan_targets(state, weights, prices, equity, *, costs=SpotCosts(), risk=SpotRisk(),
                 at=None, reason="allocation decision", model_version=None):
    """Freeze quantities and spending limits using only the decision's marks.

    Historical execution may happen on distinct venue clocks. A later opening
    price can bound a fill, but cannot increase the decision's quantity/budget.
    """
    if any(a not in state["assets"] or D(w) < 0 for a, w in weights.items()) or sum((D(w) for w in weights.values()), Decimal(0)) > 1:
        raise ValueError("Invalid target weights")
    owned = {a for a, qty in state["holdings"].items() if D(qty) > 0}
    requested = {a for a, w in weights.items() if D(w) > 0}
    if not (owned | requested).issubset(prices):
        return []
    equity = D(equity)
    remaining = equity * risk.max_turnover_fraction
    changes = []
    for asset in owned | requested:
        price = D(prices[asset])
        gap = equity * D(weights.get(asset, 0)) - D(state["holdings"].get(asset, 0)) * price
        if abs(gap) >= max(costs.min_notional, equity * risk.no_trade_fraction):
            changes.append((asset, gap))
    changes.sort(key=lambda item: (item[1] >= 0, -abs(item[1]), item[0]))
    planned = []
    for asset, gap in changes:
        notional = min(abs(gap), remaining)
        if notional < costs.min_notional:
            continue
        planned.append({"asset": asset, "side": "buy" if gap > 0 else "sell",
                        "requested_quantity": str(notional / D(prices[asset])),
                        "planned_notional_usdt": str(notional), "decision_at": at or _now(),
                        "reason": reason, "model_version": model_version})
        remaining -= notional
    return planned


def execute_planned(state, planned, quotes, prices, *, costs=SpotCosts(), at=None,
                    quantity_steps=None):
    """Fill frozen quantities at their venue event without re-sizing targets."""
    at = at or _now()
    before = mark(state, prices, at)
    if not before["complete"]:
        for order in planned:
            state["orders"].append({**order, "at": at, "status": "rejected", "reason": "missing contemporaneous valuation"})
        return before
    steps = quantity_steps or {}
    for order in sorted(planned, key=lambda row: (row["side"] == "buy", row["asset"])):
        asset, side = order["asset"], order["side"]
        quote = quotes.get(asset)
        if quote is None:
            state["orders"].append({**order, "at": at, "status": "unfilled", "reason": "missing venue opening quote"})
            continue
        price = D(quote["ask"] if side == "buy" else quote["bid"])
        depth = D(quote["ask_depth"] if side == "buy" else quote["bid_depth"])
        if price <= 0 or depth <= 0 or D(quote["ask"]) < D(quote["bid"]):
            state["orders"].append({**order, "at": at, "status": "rejected", "reason": "invalid depth"})
            continue
        price *= 1 + costs.slippage_rate if side == "buy" else 1 - costs.slippage_rate
        requested = D(order["requested_quantity"])
        quantity = min(requested, D(order["planned_notional_usdt"]) / price,
                       depth * costs.max_book_fraction / price)
        if side == "buy":
            quantity = min(quantity, D(state["cash_usdt"]) / (price * (1 + costs.fee_rate)))
        else:
            quantity = min(quantity, D(state["holdings"].get(asset, 0)))
        step = D(steps.get(asset, "0.00000001"))
        if step <= 0:
            raise ValueError("Invalid quantity step")
        quantity = (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
        notional = quantity * price
        if notional < costs.min_notional:
            state["orders"].append({**order, "at": at, "status": "unfilled", "reason": "minimum notional, cash or depth"})
            continue
        fee = notional * costs.fee_rate
        direction = 1 if side == "buy" else -1
        state["cash_usdt"] = str(D(state["cash_usdt"]) - direction * notional - fee)
        state["holdings"][asset] = str(D(state["holdings"].get(asset, 0)) + direction * quantity)
        fill = {**order, "at": at, "status": "filled" if quantity >= requested - step else "partial",
                "requested_usdt": float(requested * price), "filled_usdt": float(notional),
                "quantity": str(quantity), "price_usdt": str(price), "fee_usdt": float(fee), "estimated": True}
        state["orders"].append(fill)
        state["fills"].append(fill.copy())
        state["model_version"] = order["model_version"]
    return mark(state, prices, at)
