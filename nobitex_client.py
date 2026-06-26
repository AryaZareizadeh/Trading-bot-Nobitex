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
import os
import time
from typing import Any, Literal

import requests

BASE_URL = "https://apiv2.nobitex.ir"
TESTNET_URL = "https://testnetapiv2.nobitex.ir"


class NobitexAPIError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


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
    ):
        self._public_key = public_key or os.getenv("NOBITEX_API_PUBLIC_KEY")
        raw_private = private_key or os.getenv("NOBITEX_API_PRIVATE_KEY")
        self._token = token or os.getenv("NOBITEX_TOKEN")
        self._base = TESTNET_URL if testnet else BASE_URL
        self._session = requests.Session()
        self._session.trust_env = False
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

    def _call(self, method: str, path: str, **kwargs) -> dict:
        raw_body = ""
        if method.upper() in ("POST", "PUT", "PATCH") and "json" in kwargs:
            raw_body = json.dumps(kwargs.pop("json"), separators=(",", ":"))
            kwargs["data"] = raw_body
        headers = self._auth_headers(method, path, raw_body)
        if raw_body:
            headers["Content-Type"] = "application/json"
        resp = self._session.request(
            method, self._base + path, headers=headers, timeout=15, **kwargs
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") not in ("ok", "success"):
            raise NobitexAPIError(
                data.get("code", "error"), data.get("message", repr(data))
            )
        return data

    # ── Market info ────────────────────────────────────────────────────────────

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

    def order_status(self, order_id: int) -> dict:
        """Fetch current status and fill info for an order."""
        return self._call("POST", "/market/orders/status", json={"id": order_id})["order"]

    def cancel_order(self, order_id: int) -> None:
        """Cancel an active order."""
        self._call("POST", "/market/orders/update-status", json={
            "order": order_id,
            "status": "canceled",
        })

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
