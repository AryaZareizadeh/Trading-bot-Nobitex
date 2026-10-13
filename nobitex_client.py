"""
Nobitex API client for live margin (futures) trading.

Auth — choose one:
  Option A (recommended): API Key with Ed25519 signing.
    Set NOBITEX_API_PUBLIC_KEY and NOBITEX_API_PRIVATE_KEY in .env.
    Scope to READ,TRADE only; add an IP whitelist in the Nobitex panel.
  Option B: Session token (simpler, expires in up to 30 days).
    Set NOBITEX_TOKEN in .env.

Never store credentials in source code or commit .env to git.
"""

import base64
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from typing import Any, Literal

import requests

from nobitex_http import direct_nobitex_session

BASE_URL = "https://apiv2.nobitex.ir"
TESTNET_URL = "https://testnetapiv2.nobitex.ir"
_LOG = logging.getLogger(__name__)
_RATE_LOCK = threading.Lock()
_COOLDOWNS: dict[tuple[str, str], float] = {}
_REQUEST_WINDOWS: dict[tuple[str, str], deque] = {}
# Shared by all clients in this process: Nobitex limits are also applied by IP.
_REQUEST_LIMITS = {
    "/positions/list": (30, 600),
    "/positions/:id/status": (100, 600),
    "/margin/v2/delegation-limit": (12, 60),
    "/v2/wallets": (15, 60),
    "/margin/markets/list": (30, 60),
    "/market/orders/list": (30, 60),
    "/market/orders/status": (300, 60),
    "/market/orders/update-status": (90, 60),
    "/wallets/transfer": (10, 60),
    "order-placement": (300, 600),
}


def _rate_group(path: str) -> str:
    route = path.split("?", 1)[0]
    if route == "/margin/orders/add" or re.fullmatch(r"/positions/\d+/close", route):
        return "order-placement"
    return re.sub(r"/positions/\d+/status$", "/positions/:id/status", route)


class NobitexAPIError(Exception):
    def __init__(self, code: str, message: str, *, http_status=None, retry_after=None):
        self.code = code
        self.http_status = http_status
        self.retry_after = retry_after
        super().__init__(f"[{code}] {message}")

    @property
    def transient(self):
        return self.code == "TooManyRequests" or self.http_status == 429 or (
            self.http_status is not None and self.http_status >= 500)


class NobitexClient:
    """
    Thread-safe Nobitex REST client.

    Priority: API Key (Ed25519) > Session Token.
    Raises ValueError at construction time if no credentials are available.
    """

    def __init__(
        self,
        *,
        public_key: str | None = None,
        private_key: str | None = None,
        token: str | None = None,
        testnet: bool = False,
        allow_environment: bool = True,
    ):
        self._public_key = public_key or (os.getenv("NOBITEX_API_PUBLIC_KEY") if allow_environment else None)
        raw_private = private_key or (os.getenv("NOBITEX_API_PRIVATE_KEY") if allow_environment else None)
        self._token = token or (os.getenv("NOBITEX_TOKEN") if allow_environment else None)
        self._base = TESTNET_URL if testnet else BASE_URL
        self._session = direct_nobitex_session()
        self._session.headers["User-Agent"] = "TraderBot/NobitexBot"

        self._ed_key = None
        if self._public_key and raw_private:
            try:
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
                # Base64url keys from Nobitex may or may not have padding
                padded = raw_private + "=" * (-len(raw_private) % 4)
                key_bytes = base64.urlsafe_b64decode(padded)
                self._ed_key = Ed25519PrivateKey.from_private_bytes(key_bytes)
            except ImportError:
                raise ImportError(
                    "pip install cryptography  (required for API Key auth)"
                )

        if not self._ed_key and not self._token:
            raise ValueError(
                "No Nobitex credentials found. "
                "Set NOBITEX_API_PUBLIC_KEY + NOBITEX_API_PRIVATE_KEY "
                "or NOBITEX_TOKEN in .env"
            )

    # ── Auth ───────────────────────────────────────────────────────────────────

    def _auth_headers(self, method: str, path: str, body: str) -> dict[str, str]:
        if self._ed_key and self._public_key:
            ts = str(int(time.time()))
            msg = (ts + method.upper() + path + body).encode("utf-8")
            sig = self._ed_key.sign(msg)
            return {
                "Nobitex-Key": self._public_key,
                "Nobitex-Signature": base64.urlsafe_b64encode(sig).decode(),
                "Nobitex-Timestamp": ts,
            }
        return {"Authorization": f"Token {self._token}"}

    def _request_slot(self, path: str, *, allow_wait: bool) -> None:
        key = (self._base, _rate_group(path))
        while True:
            with _RATE_LOCK:
                now = time.monotonic()
                wait = max(0, _COOLDOWNS.get(key, 0) - now)
                budget = _REQUEST_LIMITS.get(key[1])
                if budget:
                    limit, window = budget
                    calls = _REQUEST_WINDOWS.setdefault(key, deque())
                    while calls and now - calls[0] >= window:
                        calls.popleft()
                    if len(calls) >= limit:
                        wait = max(wait, calls[0] + window - now)
                if not wait:
                    if budget:
                        calls.append(now)
                    return
            # Long waits return control to the bot so it stays cancellable and
            # keeps its durable order reservation for subsequent reconciliation.
            if not allow_wait or wait > 30:
                raise NobitexAPIError("TooManyRequests", f"{key[1]}: retry after {math.ceil(wait)}s",
                                      http_status=429, retry_after=math.ceil(wait))
            time.sleep(wait)

    def _call(self, method: str, path: str, **kwargs) -> dict:
        method = method.upper()
        raw_body = ""
        if method in ("POST", "PUT", "PATCH") and "json" in kwargs:
            raw_body = json.dumps(kwargs.pop("json"), separators=(",", ":"))
            kwargs["data"] = raw_body
        # The status endpoint uses POST but only reads. Never retry mutations:
        # a lost response may conceal an accepted order, cancel, or transfer.
        read_only = method == "GET" or (method == "POST" and path == "/market/orders/status")
        attempts = 3 if read_only else 1
        timeout = kwargs.pop("timeout", (5, 15 if read_only else 30))
        route = path.split("?", 1)[0]
        for attempt in range(attempts):
            self._request_slot(path, allow_wait=read_only)
            # Sign after rate-limit waits; Nobitex rejects timestamps >30s old.
            headers = self._auth_headers(method, path, raw_body)
            if raw_body:
                headers["Content-Type"] = "application/json"
            started = time.monotonic()
            try:
                resp = self._session.request(method, self._base + path, headers=headers,
                                             timeout=timeout, **kwargs)
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if resp.status_code == 429 or (isinstance(data, dict) and data.get("code") == "TooManyRequests"):
                    raw_wait = data.get("backOff") if isinstance(data, dict) else None
                    try:
                        delay = float(raw_wait if raw_wait is not None else resp.headers.get("Retry-After", 30))
                    except (TypeError, ValueError):
                        delay = 30
                    if not math.isfinite(delay) or delay <= 0:
                        delay = 30
                    with _RATE_LOCK:
                        key = (self._base, _rate_group(path))
                        _COOLDOWNS[key] = max(_COOLDOWNS.get(key, 0), time.monotonic() + delay)
                    raise NobitexAPIError("TooManyRequests", f"{route}: retry after {math.ceil(delay)}s",
                                          http_status=resp.status_code, retry_after=delay)
                # Decode exchange rejections even on HTTP 400; HTTP 5xx remains
                # ambiguous for submissions regardless of the JSON error code.
                if isinstance(data, dict) and data.get("status") not in ("ok", "success"):
                    raise NobitexAPIError(data.get("code", "error"), data.get("message", "Request failed"),
                                          http_status=resp.status_code)
                resp.raise_for_status()
                if not isinstance(data, dict):
                    raise NobitexAPIError("InvalidResponse", f"{route}: expected a JSON object",
                                          http_status=resp.status_code)
                _LOG.debug("Nobitex %s %s completed in %.2fs", method, route, time.monotonic() - started)
                return data
            except (requests.RequestException, NobitexAPIError) as exc:
                transient = (isinstance(exc, (requests.Timeout, requests.ConnectionError))
                             or isinstance(exc, NobitexAPIError) and exc.transient
                             or isinstance(exc, requests.HTTPError) and exc.response is not None
                             and exc.response.status_code >= 500)
                _LOG.warning("Nobitex %s %s failed after %.2fs (%s), attempt %s/%s",
                             method, route, time.monotonic() - started,
                             exc.code if isinstance(exc, NobitexAPIError) else type(exc).__name__,
                             attempt + 1, attempts)
                if not transient or attempt + 1 == attempts or (
                        isinstance(exc, NobitexAPIError) and (exc.retry_after or 0) > 30):
                    raise
                if not isinstance(exc, NobitexAPIError) or exc.code != "TooManyRequests":
                    time.sleep(2 ** attempt)

    # ── Market info ────────────────────────────────────────────────────────────

    def profile(self) -> dict:
        """Account fee options from the documented user profile endpoint."""
        return self._call("GET", "/users/profile")["profile"]

    def margin_markets(self) -> dict[str, dict]:
        """Available margin markets keyed by symbol (e.g. 'BTCUSDT')."""
        return self._call("GET", "/margin/markets/list")["markets"]

    def delegation_limit(self, market: str) -> dict:
        """Remaining delegation capacity for a market symbol (e.g. 'BTCUSDT')."""
        return self._call("GET", f"/margin/v2/delegation-limit?market={market}")["limits"]

    # ── Wallet ─────────────────────────────────────────────────────────────────

    def wallets(self, wallet_type: Literal["spot", "margin"] = "spot") -> dict[str, dict]:
        return self._call("GET", f"/v2/wallets?type={wallet_type}")["wallets"]

    def transfer_spot_to_margin(self, currency: str, amount: str) -> None:
        """Move funds from spot wallet to margin wallet."""
        self._call("POST", "/wallets/transfer", json={
            "currency": currency,
            "amount": amount,
            "src": "spot",
            "dst": "margin",
        })

    # ── Margin orders ──────────────────────────────────────────────────────────

    def open_margin_order(
        self,
        *,
        src_currency: str,
        dst_currency: str,
        side: Literal["buy", "sell"],
        amount: str,
        price: str,
        leverage: int = 1,
        execution: Literal["limit", "market"] = "market",
        client_order_id: str | None = None,
    ) -> dict:
        """
        Place a margin order.

        Long  (buy BTC with leverage): side='buy',  src='btc', dst='usdt'
        Short (sell BTC with leverage): side='sell', src='btc', dst='usdt'

        amount  — srcCurrency quantity (e.g. BTC).
                  Calculate as: credit_usdt * leverage / current_price
        price   — send current price even for market orders (acts as a slippage cap).
        leverage — 1 to max allowed for the market.
        """
        payload: dict[str, Any] = {
            "srcCurrency": src_currency,
            "dstCurrency": dst_currency,
            "type": side,
            "amount": amount,
            "price": price,
            "leverage": str(leverage),
            "execution": execution,
        }
        if client_order_id:
            payload["clientOrderId"] = client_order_id
        return self._call("POST", "/margin/orders/add", json=payload)["order"]

    def live_price(self, symbol: str) -> float:
        """Last-traded price for a symbol from the public orderbook endpoint."""
        data = self._call("GET", f"/v3/orderbook/{symbol.upper()}")
        return float(data["lastTradePrice"])

    def orderbook(self, symbol: str) -> dict:
        """Public depth used only to bound indicative portfolio rebalances."""
        return self._call("GET", f"/v3/orderbook/{symbol.upper()}")

    def order_status(self, order_id: int) -> dict:
        """Fetch current status and fill info for an order."""
        return self._call("POST", "/market/orders/status", json={"id": order_id})["order"]

    def order_by_client_id(self, client_id: str) -> dict:
        """Experimental active-order lookup; absence does NOT rule out a fill."""
        return self._call("POST", "/market/orders/status", json={"clientOrderId": client_id})["order"]

    def cancel_order(self, order_id: int) -> None:
        """Cancel an active order."""
        self._call("POST", "/market/orders/update-status", json={
            "order": order_id,
            "status": "canceled",
        })

    def active_orders(self) -> list[dict]:
        """Fetch all active (open/pending) margin orders."""
        orders, page = [], 1
        while True:
            data = self._call("GET", f"/market/orders/list?status=open&tradeType=margin&details=2&page={page}")
            orders.extend(data.get("orders", []))
            if not data.get("hasNext"):
                return orders
            page += 1

    # ── Positions ──────────────────────────────────────────────────────────────

    def positions(self, status: Literal["active", "past"] = "active") -> list[dict]:
        result: list[dict] = []
        page = 1
        while True:
            data = self._call("GET", f"/positions/list?status={status}&page={page}")
            result.extend(data["positions"])
            if not data.get("hasNext"):
                break
            page += 1
        return result

    def position(self, position_id: int) -> dict:
        return self._call("GET", f"/positions/{position_id}/status")["position"]

    def close_position(
        self,
        position_id: int,
        *,
        amount: str,
        price: str,
        execution: Literal["limit", "market"] = "market",
    ) -> dict:
        """
        Close (or partially close) an open margin position.

        amount — srcCurrency quantity to close. Use position['delegatedAmount']
                 for long or position['liability'] for short to close fully.
        """
        return self._call(
            "POST",
            f"/positions/{position_id}/close",
            json={"amount": amount, "price": price, "execution": execution},
        )["order"]
