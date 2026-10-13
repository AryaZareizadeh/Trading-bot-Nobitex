"""Read-only portfolio valuation and cash-flow-adjusted history."""

import json
import math
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def _number(value):
    try:
        result = Decimal(str(value).replace("−", "-"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid exchange amount") from exc
    if not result.is_finite():
        raise ValueError("Non-finite exchange amount")
    return result


def _utc(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    elif isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Timestamp must include a time zone")
    return value.astimezone(timezone.utc).isoformat()


def value_portfolio(spot, margin, positions, price_lookup, captured_at=None):
    """Balances already include blocked collateral; add only unrealized PnL."""
    prices = {"USDT": Decimal(1)}
    unpriced = set()

    def rate(code):
        code = code.upper()
        if code == "RLS":
            code = "IRT"
        if code not in prices:
            try:
                if code == "IRT":
                    prices[code] = Decimal(1) / _number(price_lookup("USDTIRT"))
                else:
                    prices[code] = _number(price_lookup(code + "USDT"))
                if prices[code] <= 0:
                    raise ValueError("Non-positive market price")
            except Exception:
                prices[code] = None
                unpriced.add(code)
        return prices[code]

    holdings = []
    totals = {}
    for kind, wallets in (("spot", spot), ("margin", margin)):
        subtotal = Decimal(0)
        for currency, wallet in wallets.items():
            amount = _number(wallet.get("balance", 0) or 0)
            if amount == 0:
                continue
            blocked = _number(wallet.get("blocked", 0) or 0)
            code = currency.upper().replace("RLS", "IRT")
            price = rate(code)
            value = amount * price if price is not None else None
            if value is not None:
                subtotal += value
            holdings.append({"wallet": kind, "currency": code, "amount": float(amount),
                             "blocked": float(blocked), "price_usdt": float(price) if price is not None else None,
                             "value_usdt": float(value) if value is not None else None})
        totals[kind] = subtotal

    open_positions = []
    pnl_total = Decimal(0)
    exposure_total = Decimal(0)
    for position in positions:
        if position.get("status") not in (None, "Open"):
            continue
        quote = str(position.get("dstCurrency", "USDT")).upper().replace("RLS", "IRT")
        conversion = rate(quote)
        pnl = _number(position.get("unrealizedPNL", 0) or 0)
        amount = _number(position.get("delegatedAmount", 0) or 0)
        mark = _number(position.get("markPrice", 0) or 0)
        if conversion is not None:
            pnl_total += pnl * conversion
            exposure_total += abs(amount * mark * conversion)
        open_positions.append({"id": position.get("id"), "symbol": (
            str(position.get("srcCurrency", "")) + str(position.get("dstCurrency", ""))).upper(),
            "side": position.get("side"), "collateral": float(_number(position.get("collateral", 0) or 0)),
            "unrealized_pnl_usdt": float(pnl * conversion) if conversion is not None else None,
            "exposure_usdt": float(abs(amount * mark * conversion)) if conversion is not None else None,
            "liquidation_price": float(_number(position.get("liquidationPrice", 0) or 0))})

    equity = totals["spot"] + totals["margin"] + pnl_total
    for holding in holdings:
        holding["weight_pct"] = round(100 * holding["value_usdt"] / float(equity), 2) if equity > 0 and holding["value_usdt"] is not None else None
    return {"captured_at": _utc(captured_at), "currency": "USDT", "equity_usdt": float(equity),
            "spot_usdt": float(totals["spot"]), "margin_usdt": float(totals["margin"]),
            "margin_unrealized_pnl_usdt": float(pnl_total), "margin_exposure_usdt": float(exposure_total),
            "valuation_complete": not unpriced, "unpriced": sorted(unpriced),
            "holdings": sorted(holdings, key=lambda h: h["value_usdt"] or 0, reverse=True),
            "positions": open_positions}


class PortfolioHistory:
    def __init__(self, store):
        self.store = store

    def save_snapshot(self, snapshot):
        with self.store.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO portfolio_snapshots VALUES(?,?,?,?)",
                         (snapshot["captured_at"], snapshot["equity_usdt"],
                          int(snapshot["valuation_complete"]), json.dumps(snapshot, allow_nan=False)))

    def snapshots(self, limit=5000):
        with self.store.connection() as conn:
            rows = conn.execute("SELECT data FROM portfolio_snapshots ORDER BY captured_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def add_flow(self, amount_usdt, occurred_at=None, note=""):
        amount = float(_number(amount_usdt))
        if not math.isfinite(amount) or amount == 0:
            raise ValueError("Cash flow must be nonzero")
        when = _utc(occurred_at)
        with self.store.connection() as conn:
            cursor = conn.execute("INSERT INTO portfolio_cashflows(occurred_at,amount_usdt,note) VALUES(?,?,?)",
                                  (when, amount, note[:120]))
            return cursor.lastrowid

    def delete_flow(self, flow_id):
        with self.store.connection() as conn:
            return conn.execute("DELETE FROM portfolio_cashflows WHERE id=?", (flow_id,)).rowcount > 0

    def flows(self):
        with self.store.connection() as conn:
            rows = conn.execute("SELECT id,occurred_at,amount_usdt,note FROM portfolio_cashflows ORDER BY occurred_at,id").fetchall()
        return [dict(id=r[0], occurred_at=r[1], amount_usdt=r[2], note=r[3]) for r in rows]

    def performance(self):
        # Use one consistent read snapshot and stream every observation. Only
        # chart output is sampled; the baseline and accounting never roll away.
        with self.store.connection() as conn:
            conn.execute("BEGIN")
            count = conn.execute("SELECT COUNT(*) FROM portfolio_snapshots WHERE valuation_complete=1").fetchone()[0]
            if not count:
                return {"points": [], "return_pct": None, "net_pnl_usdt": None,
                        "max_drawdown_pct": None, "period_start": None, "period_end": None}
            flows = [(datetime.fromisoformat(at), amount) for at, amount in conn.execute(
                "SELECT occurred_at,amount_usdt FROM portfolio_cashflows ORDER BY occurred_at,id")]
            stride = max(1, math.ceil((count - 1) / 499))
            points, base, previous = [], None, None
            growth = peak = 1.0
            max_dd = net_flows = 0.0
            flow_index = 0
            for index, (raw,) in enumerate(conn.execute(
                    "SELECT data FROM portfolio_snapshots WHERE valuation_complete=1 ORDER BY captured_at")):
                current = json.loads(raw)
                t1 = datetime.fromisoformat(current["captured_at"])
                if base is None:
                    base = current
                    while flow_index < len(flows) and flows[flow_index][0] <= t1:
                        flow_index += 1
                else:
                    t0 = datetime.fromisoformat(previous["captured_at"])
                    net = weighted = 0.0
                    seconds = max((t1 - t0).total_seconds(), 1)
                    while flow_index < len(flows) and flows[flow_index][0] <= t1:
                        at, amount = flows[flow_index]
                        net += amount
                        weighted += amount * (t1 - at).total_seconds() / seconds
                        flow_index += 1
                    net_flows += net
                    denominator = previous["equity_usdt"] + weighted
                    if denominator > 0:
                        growth *= max(0.0, 1 + (current["equity_usdt"] - previous["equity_usdt"] - net) / denominator)
                    peak = max(peak, growth)
                    max_dd = max(max_dd, (peak - growth) / peak)
                if index % stride == 0 or index == count - 1:
                    points.append({"time": current["captured_at"], "equity_usdt": current["equity_usdt"], "growth": growth})
                previous = current
        return {"points": points, "return_pct": round((growth - 1) * 100, 2) if count > 1 else None,
                "net_pnl_usdt": round(previous["equity_usdt"] - base["equity_usdt"] - net_flows, 2),
                "max_drawdown_pct": round(max_dd * 100, 2) if count > 1 else None,
                "period_start": base["captured_at"], "period_end": previous["captured_at"],
                "note": "Returns assume every external deposit and withdrawal was recorded."}


def validate_targets(targets):
    if not isinstance(targets, dict) or len(targets) > 20:
        raise ValueError("Provide at most 20 target assets")
    result = {}
    for symbol, weight in targets.items():
        code = str(symbol).upper()
        if not code.isascii() or not code.isalnum() or not 2 <= len(code) <= 12 or code in {"USDT", "IRT", "RLS"}:
            raise ValueError("Invalid target asset")
        value = float(_number(weight))
        if value < 0 or value > .35:
            raise ValueError("Target weights must be between 0 and 35%")
        if value:
            result[code] = value
    if sum(result.values()) > .8 + 1e-9:
        raise ValueError("Keep at least 20% of spot value in cash")
    return result


def rebalance_ideas(snapshot, targets, orderbook_lookup, fee_rate=.002, slippage_rate=.002):
    """Indicative spot-only ideas; missing depth suppresses a proposal."""
    if not snapshot["valuation_complete"] or snapshot["spot_usdt"] <= 0:
        return []
    spot_value = snapshot["spot_usdt"]
    holdings = {h["currency"]: h for h in snapshot["holdings"] if h["wallet"] == "spot"}
    turnover_left = .10 * spot_value
    cash_left = max(0, (holdings.get("USDT", {}).get("value_usdt") or 0) - .20 * spot_value)
    result = []
    codes = set(targets) | (set(holdings) - {"USDT", "IRT"})
    for code in sorted(codes, key=lambda c: (-abs(spot_value * targets.get(c, 0) -
                                                  (holdings.get(c, {}).get("value_usdt") or 0)), c)):
        current = holdings.get(code, {}).get("value_usdt") or 0
        gap = spot_value * targets.get(code, 0) - current
        if abs(gap) < max(10, spot_value * .01):
            continue
        side = "buy" if gap > 0 else "sell"
        try:
            book = orderbook_lookup(code + "USDT")
            if book.get("lastUpdate") and time.time() - float(book["lastUpdate"]) / 1000 > 60:
                continue
            best_bid, best_ask = _number(book["bids"][0][0]), _number(book["asks"][0][0])
            if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid or (best_ask - best_bid) / best_ask > Decimal("0.02"):
                continue
            levels = book["asks" if side == "buy" else "bids"][:5]
            depth = sum(float(_number(price) * _number(size)) for price, size in levels)
            if depth <= 0:
                continue
        except Exception:
            continue
        amount = min(abs(gap), turnover_left, .25 * depth)
        if side == "buy":
            amount = min(amount, cash_left)
        cost = amount * (fee_rate + slippage_rate)
        if amount < 10 or amount - cost < 10:
            continue
        result.append({"asset": code, "side": side, "notional_usdt": round(amount, 2),
                       "estimated_cost_usdt": round(cost, 2), "top5_depth_usdt": round(depth, 2),
                       "target_pct": round(targets.get(code, 0) * 100, 2),
                       "reason": "Allocation drift; capped by cash, turnover and visible order-book depth"})
        turnover_left -= amount
        if side == "buy":
            cash_left -= amount
    return result
