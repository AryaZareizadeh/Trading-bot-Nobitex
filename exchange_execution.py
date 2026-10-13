"""Durable order submission and conservative reconciliation.

Nobitex client-ID lookup is experimental and only searches active orders:
https://apidocs.nobitex.ir/?shell= . Missing lookup is never proof of no fill.
"""
import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

import requests

from position_settlement import position_is_settled, settled_position
from trading_audit import audit_record, record_event

# Explicit documented nonacceptance. DuplicateOrder can refer to an earlier
# accepted request, and unfamiliar/server errors must remain uncertain.
_ORDER_REJECTIONS = frozenset({
    "FeatureUnavailable", "ParseError", "TradeLimitation", "InvalidMarketPair",
    "BadPrice", "PriceConditionFailed", "NoOpenPosition", "ExceedLiability",
    "ExceedTotalAsset", "TooManyRequests",
})


def _transient_read_error(exc):
    from nobitex_client import NobitexAPIError
    return (isinstance(exc, (TimeoutError, requests.Timeout, requests.ConnectionError))
            or isinstance(exc, NobitexAPIError) and exc.transient
            or isinstance(exc, requests.HTTPError) and exc.response is not None
            and exc.response.status_code >= 500)


class OrderExecutor:
    def __init__(self, bot, client):
        if bot._store is None:
            raise RuntimeError("Live orders require durable storage")
        self.bot, self.client, self.store = bot, client, bot._store
        self.account = bot._account_key()

    async def call(self, method, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.client, method), *args, **kwargs)

    def save(self, intent):
        # Resolution and its resulting runtime must survive (or roll back) together.
        intent['updated_at'] = datetime.now(timezone.utc).isoformat()
        with self.store.connection() as conn:
            self.store.write(conn, "orders", intent["client_order_id"], intent)
            position = intent.get("exchange_position", {})
            if position_is_settled(position, in_past=intent.get("settlement_from_history", False)):
                self.store.write(conn, "closed_positions", f"{self.account}:{position['id']}",
                                 {"bot_id": self.bot.id, "position": position})
            runtime_key, snapshot = self.bot.runtime_record()
            self.store.write(conn, "runtime", runtime_key, snapshot)
            self._audit(intent, conn)

    def _audit(self, intent, conn):
        detail = json.dumps({key: intent.get(key) for key in
                             ("kind", "side", "position_direction", "state", "client_order_id", "order_id")})
        record_event(self.store, audit_record(self.bot.id, "order_state", detail), conn=conn)

    @staticmethod
    def _direction(intent):
        if intent.get("position_direction") in ("long", "short"):
            return intent["position_direction"]
        # Compatibility for close intents written before side meant execution.
        if intent["side"] in ("long", "short"):
            return intent["side"]
        side = intent["side"]
        return "long" if side == ("sell" if intent["kind"] == "close" else "buy") else "short"

    @classmethod
    def _execution_side(cls, intent):
        if intent["kind"] == "close":
            return "sell" if cls._direction(intent) == "long" else "buy"
        return intent["side"]

    def _clear_owned(self):
        self.bot._exchange_position_id = None
        self.bot._real_state = "none"
        self.bot._seed_position = None
        self.bot._pending_order = None
        self.bot._real_positions = []
        self.bot.open_positions = []
        self.bot._agent_signal = None
        if self.bot.df is not None and not self.bot.df.empty:
            self.bot._live_from_ts = self.bot._next_candle_open(self.bot.df.index[-1])

    async def _settled(self, intent, position):
        settled = await settled_position(self.client, position)
        if settled is not None:
            intent["settlement_from_history"] = not position_is_settled(position)
            intent["exchange_position"] = settled
        return settled is not None

    def _new(self, kind, src, dst, side, payload, existing_ids):
        intent = dict(client_order_id=uuid.uuid4().hex, bot_id=self.bot.id,
                      account=self.account, kind=kind, src=src, dst=dst, side=side,
                      state="prepared", payload=payload, existing_ids=existing_ids,
                      created_at=datetime.now(timezone.utc).isoformat(), order_id=None)
        intent["position_direction"] = self._direction(intent)
        intent["side"] = self._execution_side(intent)
        intent['updated_at'] = intent['created_at']
        # SQLite write lock makes market reservation and intent insertion atomic.
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for (raw,) in conn.execute("SELECT value FROM records WHERE namespace='orders'"):
                other = json.loads(raw)
                if (other["account"] == self.account and other["src"] == src and other["dst"] == dst
                        and other["state"] not in ("resolved", "rejected")):
                    raise RuntimeError("Market reserved by an unresolved order")
            if kind == "open":
                market = f"{src}{dst}".lower()
                for (raw,) in conn.execute("SELECT value FROM records WHERE namespace='runtime'"):
                    runtime = json.loads(raw)
                    for owned in [runtime, *runtime.get("side_runtimes", {}).values()]:
                        position_id = owned.get("_exchange_position_id")
                        if (owned.get("_account") == self.account and owned.get("market") == market
                                and position_id is not None):
                            position = {"id": position_id, "side": "buy" if owned.get("_real_state") == "long" else "sell"}
                            if not self.bot.can_share_position(position):
                                raise RuntimeError("Market is already owned by a bot")
            self.store.write(conn, "orders", intent["client_order_id"], intent)
            self._audit(intent, conn)
        return intent

    async def open(self, src, dst, side, amount, price, leverage, timeout=300):
        if self.bot.unresolved_intents():
            raise RuntimeError("Unresolved order must be reconciled before submission")
        active = await self.call("positions", "active")
        if any(p.get("srcCurrency") == src and p.get("dstCurrency") == dst and p.get("status") == "Open"
               and not self.bot.can_share_position(p) for p in active):
            raise RuntimeError("Market has an existing position; assign ownership and reconcile first")
        payload = dict(src_currency=src, dst_currency=dst, side=side, amount=amount,
                       price=price, leverage=leverage, execution="market")
        intent = self._new("open", src, dst, side, payload, [p["id"] for p in active])
        await self.submit(intent)
        await self.poll(intent, timeout)
        if float(intent["exchange_order"].get("matchedAmount") or 0) <= 0:
            raise RuntimeError(f"Order #{intent['order_id']} ended without a fill ({intent['exchange_order'].get('status')})")
        position = intent.get("exchange_position")
        return [position] if position and position.get("status") == "Open" else []

    async def close(self, position_id, amount, price, timeout=120):
        if self.bot.unresolved_intents():
            await self.reconcile()
            if self.bot.unresolved_intents():
                return False
        src, dst = self.bot._parse_symbol()
        if self.store.owner(self.account, position_id) != self.bot.id:
            raise RuntimeError("Cannot close an unowned position")
        payload = dict(position_id=position_id, amount=amount, price=price, execution="market")
        if self.bot._real_state not in ("long", "short"):
            raise RuntimeError("Owned position direction is unavailable")
        intent = self._new("close", src, dst, self.bot._real_state, payload, [position_id])
        await self.submit(intent)
        await self.poll(intent, timeout)
        return intent["state"] == "resolved" and self.bot._exchange_position_id is None

    async def submit(self, intent):
        # Mark before network I/O. A crash here stays uncertain rather than replaying.
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM records WHERE namespace='orders' AND key=?",
                               (intent["client_order_id"],)).fetchone()
            saved = json.loads(row[0]) if row else None
            if not saved or saved["state"] != "prepared":
                raise RuntimeError("Order already submitted or no durable prepared intent")
            if saved["account"] != self.account or saved["bot_id"] != self.bot.id:
                raise RuntimeError("Order belongs to another account or bot")
            intent.clear()
            intent.update(saved)
            intent["state"] = "submitted"
            intent['updated_at'] = datetime.now(timezone.utc).isoformat()
            self.store.write(conn, "orders", intent["client_order_id"], intent)
            runtime_key, snapshot = self.bot.runtime_record()
            self.store.write(conn, "runtime", runtime_key, snapshot)
            self._audit(intent, conn)
        try:
            if intent["kind"] == "open":
                order = await self.call("open_margin_order", **intent["payload"], client_order_id=intent["client_order_id"])
            else:
                order = await self.call("close_position", **intent["payload"])
            if order.get("id") is None:
                raise RuntimeError("Exchange returned no order ID")
        except BaseException as exc:
            from nobitex_client import NobitexAPIError
            # Only a definite exchange rejection establishes absence of acceptance.
            from bot import _FATAL_ORDER_ERRORS
            rejected = (isinstance(exc, NobitexAPIError)
                        and exc.code in (_FATAL_ORDER_ERRORS | _ORDER_REJECTIONS)
                        and (exc.http_status is None or exc.http_status < 500))
            intent["state"] = "rejected" if rejected else "uncertain"
            reason = str(exc) if isinstance(exc, NobitexAPIError) else type(exc).__name__
            intent["last_error"] = reason
            self.save(intent)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if rejected:
                raise RuntimeError(f"Order rejected: {reason}") from exc
            raise RuntimeError(f"Order outcome uncertain ({reason}); reconcile before another submission") from exc
        intent["order_id"] = order["id"]
        intent["exchange_order"] = order
        intent["state"] = "acknowledged"
        self.bot._pending_order = dict(order_id=order["id"], side=intent["side"],
                                       amount=float(intent["payload"]["amount"]), price=float(intent["payload"]["price"]),
                                       placed_at=intent["created_at"], matched_pct=0)
        self.save(intent)

    async def poll(self, intent, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                await self.refresh(intent)
            except Exception as exc:
                if not _transient_read_error(exc):
                    raise
                intent["last_check_error"] = type(exc).__name__
                self.save(intent)
                self.bot._log_event("warning", f"Order #{intent.get('order_id')} status temporarily unavailable ({type(exc).__name__})")
            if intent["state"] == "resolved":
                return
            if time.monotonic() >= deadline:
                # Cancel the remainder even on >50% partial fills. Keep reservation
                # until the exchange confirms terminal state.
                status = intent.get("exchange_order", {}).get("status", "").lower()
                if intent.get("order_id") and status not in ("done", "canceled", "cancelled", "rejected"):
                    await self.call("cancel_order", intent["order_id"])
                    await self.refresh(intent)
                if intent["state"] != "resolved":
                    raise RuntimeError("Order still unresolved after timeout; reconciliation required")
                return
            # Position-status has a much smaller budget than order-status.
            await asyncio.sleep(min(15, max(0, deadline - time.monotonic())))

    async def refresh(self, intent):
        if intent["account"] != self.account or intent["bot_id"] != self.bot.id:
            raise RuntimeError("Unresolved order belongs to another account/environment")
        intent["position_direction"] = self._direction(intent)
        intent["side"] = self._execution_side(intent)
        if not intent.get("order_id"):
            if intent["state"] == "prepared":
                intent["state"] = "rejected"  # no network call began
                self.save(intent)
                return
            if intent["kind"] == "close":
                position = await self._close_position(intent)
                if await self._settled(intent, position):
                    intent["state"] = "resolved"
                    self._clear_owned()
                    self.save(intent)
                else:
                    intent["exchange_position"] = position
                    self.save(intent)
                return
            try:
                found = await self.call("order_by_client_id", intent["client_order_id"])
            except Exception:
                return  # Not found may mean already filled, never resubmit.
            if not found.get("id"):
                return
            intent["order_id"] = found["id"]
            self.save(intent)
        info = await self.call("order_status", intent["order_id"])
        intent["exchange_order"] = info
        amount, matched = float(info.get("amount") or intent["payload"]["amount"]), float(info.get("matchedAmount") or 0)
        self.bot._pending_order = dict(order_id=intent["order_id"], side=intent["side"],
                                       amount=amount, price=float(intent["payload"]["price"]),
                                       placed_at=intent["created_at"],
                                       matched_pct=round(100 * matched / amount, 1) if amount else 0)
        # Preserve actual fees/fills even if the subsequent position query fails.
        self.save(intent)
        terminal = info.get("status", "").lower() in ("done", "canceled", "cancelled", "rejected")
        if intent["kind"] == "open" and matched > 0:
            position_id = info.get("positionId") or intent.get("exchange_position", {}).get("id")
            if position_id is not None:
                # After ownership is established, query that position directly
                # instead of spending the account-wide position-list budget.
                position = await self.call("position", position_id)
                if (str(position.get("id")) != str(position_id)
                        or position.get("srcCurrency") != intent["src"]
                        or position.get("dstCurrency") != intent["dst"]
                        or position.get("side") != intent["side"]):
                    raise RuntimeError("Exchange position does not match the recorded open order")
                self.store.claim_position(self.account, position["id"], self.bot.id)
                intent["exchange_position"] = position
                if terminal and await self._settled(intent, position):
                    intent["state"] = "resolved"
                    self._clear_owned()
                    self.save(intent)
                    return
                matches = [position] if position.get("status") == "Open" else []
            else:
                active = await self.call("positions", "active")
                matches = [p for p in active if p.get("srcCurrency") == intent["src"]
                           and p.get("dstCurrency") == intent["dst"] and p.get("side") == intent["side"]
                           and p.get("status") == "Open" and p["id"] not in intent["existing_ids"]]
            if len(matches) != 1:
                intent["state"] = "awaiting_position"
                self.save(intent)
                return
            position = matches[0]
            self.store.claim_position(self.account, position["id"], self.bot.id)
            self.bot._exchange_position_id = position["id"]
            self.bot._real_state = "long" if position["side"] == "buy" else "short"
            self.bot._real_positions = self.bot._normalize_positions([position])
            seed = dict(direction="Long" if position["side"] == "buy" else "Short",
                        entry_price=float(position["entryPrice"]), entry_time=position["openedAt"])
            previous = self.bot._seed_position
            if previous is None or any(previous.get(key) != value for key, value in seed.items()):
                if previous and previous.get("direction") == seed["direction"] and previous.get("entry_time") == seed["entry_time"]:
                    # Later partial fills can change the average price. Apply
                    # the new price only to future bars, preserving progress.
                    for key in ("last_evaluated_at", "touched_extreme", "exit_requested"):
                        if key in previous:
                            seed[key] = previous[key]
                self.bot._seed_position = seed
            intent["exchange_position"] = position
        if intent["kind"] == "close":
            position = await self._close_position(intent)
            intent["exchange_position"] = position
            self.bot._real_positions = self.bot._normalize_positions([position])
            if await self._settled(intent, position):
                self._clear_owned()
            elif position.get("status") != "Open":
                intent["state"] = "awaiting_settlement"
                self.save(intent)
                return
            # Done order with still-open position may be partial; no reopening.
        intent["state"] = "resolved" if terminal else "partial" if matched else "acknowledged"
        if terminal:
            self.bot._pending_order = None
        self.save(intent)

    async def _close_position(self, intent):
        position_id = intent["payload"]["position_id"]
        if self.store.owner(self.account, position_id) != self.bot.id:
            raise RuntimeError("Cannot reconcile an unowned position")
        position = await self.call("position", position_id)
        expected_side = "buy" if self._direction(intent) == "long" else "sell"
        if (str(position.get("id")) != str(position_id)
                or position.get("srcCurrency") != intent["src"]
                or position.get("dstCurrency") != intent["dst"]
                or position.get("side") != expected_side):
            raise RuntimeError("Exchange position does not match the recorded order")
        return position

    async def reconcile(self):
        for intent in self.bot.unresolved_intents():
            await self.refresh(intent)
        self.bot._persist()
        return not self.bot.unresolved_intents()

    async def bind_order(self, client_order_id, order_id):
        """Bind an uncertain local intent to an operator-supplied exchange order.

        This never guesses. The exchange record must match the local account,
        market, direction, amount, and creation time before reconciliation.
        """
        if not isinstance(order_id, int) or isinstance(order_id, bool) or order_id <= 0:
            raise ValueError("A positive exchange order ID is required")
        intent = self.store.get("orders", client_order_id)
        if not intent or intent.get("bot_id") != self.bot.id or intent.get("account") != self.account:
            raise ValueError("Unknown order intent")
        if intent.get("state") in ("resolved", "rejected"):
            raise ValueError("Order intent is already resolved")
        for other in self.store.list("orders").values():
            if (other.get("account") == self.account and other.get("order_id") == order_id
                    and other.get("client_order_id") != client_order_id):
                raise ValueError("Exchange order is already bound to another intent")
        info = await self.call("order_status", order_id)
        expected = intent["payload"]
        exchange_client_id = info.get("clientOrderId")
        if exchange_client_id and exchange_client_id != client_order_id:
            raise ValueError("Exchange order does not match the recorded client ID")
        try:
            created = datetime.fromisoformat(str(info.get("created_at", "")).replace("Z", "+00:00"))
            intended = datetime.fromisoformat(intent["created_at"].replace("Z", "+00:00"))
            time_matches = abs((created - intended).total_seconds()) <= 300
        except (TypeError, ValueError):
            time_matches = False
        amount_matches = abs(float(info.get("amount") or 0) - float(expected.get("amount") or 0)) <= 1e-10
        market_matches = (str(info.get("srcCurrency", "")).lower() == intent["src"]
                          and str(info.get("dstCurrency", "")).lower() == intent["dst"])
        side_matches = str(info.get("type") or info.get("side") or "").lower() == self._execution_side(intent)
        order_kind = str(info.get("side") or info.get("kind") or "").lower()
        kind_matches = order_kind not in ("open", "close") or order_kind == intent["kind"]
        position_id = info.get("positionId", info.get("position_id"))
        position_matches = (intent["kind"] != "close" or position_id is None
                            or str(position_id) == str(expected["position_id"]))
        if not kind_matches or not position_matches:
            raise ValueError("Exchange order does not match the recorded position or kind")
        if not (exchange_client_id == client_order_id or
                (market_matches and side_matches and amount_matches and time_matches)):
            raise ValueError("Exchange order does not match the recorded intent")
        intent["order_id"] = order_id
        intent["manual_binding"] = {
            "bound_at": datetime.now(timezone.utc).isoformat(),
            "validated_by": "client_order_id" if exchange_client_id else "market-side-amount-time",
        }
        self.save(intent)
        await self.refresh(intent)
        return intent["state"] == "resolved"
