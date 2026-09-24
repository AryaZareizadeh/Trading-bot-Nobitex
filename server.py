import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import asdict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bot import Bot, BotConfig, BotStatus
from bot_manager import BotManager
from backtester import TesterManager
from optimizer import OptimizerManager
from auth import install_auth
from settings import update_environment, data_dir, load_environment
from execution_model import ExecutionConfig, execution_config
from nobitex_assumptions import MARGIN_DOC, profile_fee, market_renewal_rate
from portfolio import PortfolioHistory, value_portfolio, validate_targets, rebalance_ideas

# ── REST Models ────────────────────────────────────────────────────────────────
class CreateBotRequest(BaseModel):
    name: str = ""
    symbol: str
    strategy_name: str
    timeframe: str
    credit: float = Field(gt=0)
    leverage: int = Field(default=5, ge=1, le=100)
    mode: str = "simulation"
    custom_params: Optional[dict] = None
    execution: Optional[ExecutionConfig] = None
    ai_timeframes: Optional[list[str]] = None


class PatchBotRequest(BaseModel):
    name: Optional[str] = None
    symbol: Optional[str] = None
    strategy_name: Optional[str] = None
    timeframe: Optional[str] = None
    ai_timeframes: Optional[list[str]] = None
    credit: Optional[float] = Field(default=None, gt=0)
    leverage: Optional[int] = Field(default=None, ge=1, le=100)
    mode: Optional[str] = None
    restart: bool = False


class CredentialsRequest(BaseModel):
    token: str = ""
    public_key: str = ""
    private_key: str = ""
    testnet: bool = False


class TesterPrefsRequest(BaseModel):
    favorites: list[str] = []
    strategies: list[str] = []
    timeframes: list[str] = []
    credit: float = 100.0
    leverage: int = Field(default=5, ge=1, le=100)


class TesterRunRequest(BaseModel):
    symbols: list[str]
    strategies: list[str]
    timeframes: list[str]
    credit: float = Field(default=100.0, gt=0)
    leverage: int = Field(default=5, ge=1, le=100)
    execution: Optional[ExecutionConfig] = None


class OptimizerPrefsRequest(BaseModel):
    symbol: str = ""
    strategy: str = ""
    timeframe: str = "240"
    candles: int = 1000
    credit: float = 100.0
    leverage: int = Field(default=5, ge=1, le=100)
    cores: int = Field(default=0, ge=0)   # 0 = Auto
    maxiter: int = Field(default=30, ge=5, le=5000)
    objective: str = "profit"             # profit | winrate | balanced | large_move
    custom_bounds: Optional[dict] = None  # param_name -> [low, high]
    min_price_change: float = Field(default=5.0, gt=0, le=100)
    balanced_cap: float = Field(default=100.0, gt=0, le=5000)
    balanced_weight: float = Field(default=0.5, ge=0.05, le=0.95)


class OptimizerRunRequest(BaseModel):
    symbol: str
    strategy: str
    timeframe: str
    candles: int = Field(default=1000, ge=50)
    credit: float = Field(default=100.0, gt=0)
    leverage: int = Field(default=5, ge=1, le=100)
    cores: int = Field(default=0, ge=0)   # 0 = Auto
    maxiter: int = Field(default=30, ge=5, le=5000)
    objective: str = "profit"             # profit | winrate | balanced | large_move
    custom_bounds: Optional[dict] = None  # param_name -> [low, high]
    min_price_change: float = Field(default=5.0, gt=0, le=100)
    balanced_cap: float = Field(default=100.0, gt=0, le=5000)
    balanced_weight: float = Field(default=0.5, ge=0.05, le=0.95)
    execution: Optional[ExecutionConfig] = None
    random_seed: Optional[int] = Field(default=None, ge=0, le=4294967295)


class OptimizerTesterRunRequest(OptimizerRunRequest):
    train_pct: float = Field(default=70.0, ge=50.0, le=90.0)  # % of candles for training


class PortfolioFlowRequest(BaseModel):
    kind: str
    amount_usdt: float = Field(gt=0)
    occurred_at: Optional[str] = None
    note: str = Field(default="", max_length=120)


class PortfolioTargetsRequest(BaseModel):
    targets: dict[str, float]


def create_app(*, manager=None, exchange_client=None, offline=None, credential_store=None, security=True,
               optimizer_slots=None):
    """Create an isolated dashboard; tests inject storage/client and disable polling."""
    if offline is None:
        offline = os.getenv("TRADER_OFFLINE", "false").lower() == "true"
    bot_manager = manager or BotManager(legacy_dir=data_dir() if os.getenv("TRADER_DATA_DIR") else None)
    portfolio_history = PortfolioHistory(bot_manager.store)
    tester_manager = TesterManager(store=manager.store if manager is not None else None)
    optimizer_manager = OptimizerManager(store=manager.store if manager is not None else None)
    def _client():
        if exchange_client is not None:
            return exchange_client
        from nobitex_client import NobitexClient
        if credential_store is not None:
            values = credential_store.get()
            return NobitexClient(token=values.get("token"), public_key=values.get("public_key"),
                                 private_key=values.get("private_key"), testnet=values.get("testnet", False),
                                 allow_environment=False)
        return NobitexClient(testnet=os.getenv("NOBITEX_TESTNET", "false").lower() == "true")

    def _portfolio_snapshot(force=False):
        client = _client()
        snapshot = value_portfolio(client.wallets("spot"), client.wallets("margin"),
                                   client.positions("active"), client.live_price)
        previous = portfolio_history.snapshots(limit=1)
        if force or not previous or (datetime.fromisoformat(snapshot["captured_at"]) -
                                     datetime.fromisoformat(previous[-1]["captured_at"])).total_seconds() >= 3600:
            portfolio_history.save_snapshot(snapshot)
        return snapshot

    async def _portfolio_snapshot_loop():
        while True:
            try:
                await asyncio.to_thread(_portfolio_snapshot)
            except Exception:
                pass  # The Portfolio tab reports errors when explicitly opened.
            await asyncio.sleep(3600)

    if credential_store is not None:
        bot_manager.client_factory = _client
    # ── WebSocket client registry ──────────────────────────────────────────────────
    _clients: list[WebSocket] = []

    # ── Exchange-level position/order cache (refreshed every 30 s) ────────────────
    _exchange_state: dict = {
        "positions": [],
        "orders": [],
        "last_synced": None,
        "error": None,
    }


    def _normalize_exchange_position(raw: dict) -> dict:
        src = raw.get("srcCurrency", "")
        dst = raw.get("dstCurrency", "")
        return {
            "exchange_id":         raw.get("id"),
            "direction":           "Long" if raw.get("side") == "buy" else "Short",
            "entry_time":          raw.get("openedAt", ""),
            "entry_price":         float(raw.get("entryPrice") or 0),
            "unrealized_pnl":      float(raw.get("unrealizedPNL") or 0),
            "unrealized_pnl_pct":  float(raw.get("unrealizedPNLPercent") or 0),
            "liquidation_price":   float(raw.get("liquidationPrice") or 0),
            "mark_price":          float(raw.get("markPrice") or 0),
            "margin_ratio":        float(raw.get("marginRatio") or 0),
            "collateral":          float(raw.get("collateral") or 0),
            "delegated_amount":    float(raw.get("delegatedAmount") or 0),
            "src_currency":        src,
            "dst_currency":        dst,
            "symbol":              f"{src}{dst}".upper(),
            "attributed_bot_id":   None,
            "attributed_bot_name": None,
        }


    _CURRENCY_FULL_TO_CODE: dict[str, str] = {
        "bitcoin": "btc", "tether": "usdt", "ethereum": "eth",
        "litecoin": "ltc", "ripple": "xrp", "bitcoin cash": "bch",
        "zcash": "zec", "dogecoin": "doge", "solana": "sol",
        "chainlink": "link", "uniswap": "uni", "binance coin": "bnb",
        "shiba inu": "shib", "aave": "aave", "sonic": "s",
        "cosmos": "atom", "near protocol": "near", "worldcoin": "wld",
        "pepe": "pepe", "tether gold": "xaut", "gram": "gram",
        "sui": "sui", "hyperliquid": "hype", "cardano": "ada",
    }


    def _currency_code(name: str) -> str:
        return _CURRENCY_FULL_TO_CODE.get(name.lower(), name.lower())


    def _normalize_exchange_order(raw: dict) -> dict:
        amount  = float(raw.get("amount") or 0)
        matched = float(raw.get("matchedAmount") or 0)
        matched_pct = round(matched / amount * 100, 1) if amount > 0 else 0.0
        src = _currency_code(raw.get("srcCurrency", ""))
        dst = _currency_code(raw.get("dstCurrency", ""))
        return {
            "order_id":            raw.get("id"),
            "side":                raw.get("type"),
            "amount":              amount,
            "price":               float(raw.get("price") or 0),
            "matched_pct":         matched_pct,
            "placed_at":           raw.get("created_at", ""),
            "symbol":              f"{src}{dst}".upper(),
            "attributed_bot_id":   None,
            "attributed_bot_name": None,
        }


    def _attribute_orders(orders: list[dict]) -> None:
        claimed: dict[int, tuple[str, str]] = {}
        for bot in bot_manager.get_all():
            po = bot._pending_order
            if po is not None and po.get("order_id") is not None:
                claimed[po["order_id"]] = (bot.id, bot.cfg.name or bot.id)
        for order in orders:
            oid = order.get("order_id")
            if oid in claimed:
                order["attributed_bot_id"], order["attributed_bot_name"] = claimed[oid]


    def _attribute_positions(positions: list[dict]) -> None:
        claimed: dict[int, tuple[str, str]] = {}
        for bot in bot_manager.get_all():
            if bot.cfg.mode == "real" and bot._exchange_position_id is not None:
                claimed[bot._exchange_position_id] = (bot.id, bot.cfg.name or bot.id)
        for pos in positions:
            exc_id = pos.get("exchange_id")
            if exc_id in claimed:
                pos["attributed_bot_id"], pos["attributed_bot_name"] = claimed[exc_id]


    async def _exchange_sync_loop() -> None:
        while True:
            try:
                client = _client()
                raw = await asyncio.to_thread(client.positions, "active")
                normalized = [
                    _normalize_exchange_position(p)
                    for p in raw
                    if p.get("status") == "Open"
                ]
                _attribute_positions(normalized)
                _exchange_state["positions"] = normalized

                raw_orders = await asyncio.to_thread(client.active_orders)
                norm_orders = [_normalize_exchange_order(o) for o in raw_orders]
                _attribute_orders(norm_orders)
                _exchange_state["orders"] = norm_orders

                _exchange_state["last_synced"] = datetime.now(timezone.utc).isoformat()
                _exchange_state["error"] = None
            except ValueError:
                _exchange_state["error"] = "no_credentials"
            except Exception as exc:
                _exchange_state["error"] = str(exc)
            await asyncio.sleep(120)


    async def _broadcast_loop() -> None:
        while True:
            if _clients:
                payload = json.dumps({
                    "bots": bot_manager.get_all_states(),
                    "exchange": _exchange_state,
                })
                dead: list[WebSocket] = []
                for ws in list(_clients):
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    _clients.remove(ws)
            await asyncio.sleep(1)


    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = bot_manager.store
        store.acquire_runtime()
        tasks = []
        try:
            bot_manager.load()
            tester_manager.load()
            optimizer_manager.load()
            for bot in bot_manager.get_all():
                if exchange_client is not None:
                    bot._exchange_client = exchange_client
            tasks.append(asyncio.create_task(_broadcast_loop()))
            if not offline:
                tasks.append(asyncio.create_task(_exchange_sync_loop()))
                tasks.append(asyncio.create_task(_portfolio_snapshot_loop()))
            yield
        finally:
            for task in tasks:
                task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.gather(*(bot.stop() for bot in bot_manager.get_all()))
                await tester_manager.shutdown()
                await optimizer_manager.shutdown()
            finally:
                store.release_runtime()


    # ── App ────────────────────────────────────────────────────────────────────────
    app = FastAPI(title="Trading Bot Dashboard", lifespan=lifespan)
    if security:
        install_auth(app)
    else:
        @app.exception_handler(RequestValidationError)
        async def safe_tenant_validation(request: Request, exc: RequestValidationError):
            if request.url.path == "/api/settings/credentials":
                return JSONResponse({"error": "Invalid credential request"}, status_code=422)
            from fastapi.exception_handlers import request_validation_exception_handler
            return await request_validation_exception_handler(request, exc)

    _static_dir = Path(__file__).parent / "static"
    _static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


    # ── WebSocket ──────────────────────────────────────────────────────────────────
    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        _clients.append(websocket)
        try:
            await websocket.send_text(json.dumps({
                "bots": bot_manager.get_all_states(),
                "exchange": _exchange_state,
            }))
        except Exception:
            pass
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            if websocket in _clients:
                _clients.remove(websocket)


    # ── HTML ───────────────────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def root():
        html_path = _static_dir / "index.html"
        return html_path.read_text(encoding="utf-8")


    # ── Margin Markets ─────────────────────────────────────────────────────────────
    @app.get("/api/margin-markets")
    async def get_margin_markets():
        def _parse(markets_raw: dict) -> list[dict]:
            result = [
                {
                    "symbol": sym,
                    "srcCurrency": info["srcCurrency"],
                    "dstCurrency": info["dstCurrency"],
                    "maxLeverage": int(info.get("maxLeverage", 5)),
                    "buyEnabled": bool(info.get("buyEnabled", True)),
                    "sellEnabled": bool(info.get("sellEnabled", True)),
                    "feeRate": info.get("positionFeeRate", "0.001"),
                }
                for sym, info in markets_raw.items()
                if (info.get("buyEnabled") or info.get("sellEnabled"))
                and (not sym.upper().endswith("IRT") or sym.upper() == "USDTIRT")
            ]
            result.sort(key=lambda x: x["symbol"])
            return result

        # Try authenticated (margin/markets/list requires a token)
        try:
            client = _client()
            raw = await asyncio.to_thread(client.margin_markets)
            return {"ok": True, "markets": _parse(raw), "source": "live_auth"}
        except ValueError:
            pass
        except Exception:
            pass

        # Hardcoded fallback — known Nobitex margin symbols as of mid-2025
        fallback = [
            {"symbol": "BTCUSDT",     "srcCurrency": "btc",     "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "ETHUSDT",     "srcCurrency": "eth",     "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "LTCUSDT",     "srcCurrency": "ltc",     "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "BCHUSDT",     "srcCurrency": "bch",     "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "XRPUSDT",     "srcCurrency": "xrp",     "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "ZECUSDT",     "srcCurrency": "zec",     "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "DOGEUSDT",    "srcCurrency": "doge",    "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "SOLUSDT",     "srcCurrency": "sol",     "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "LINKUSDT",    "srcCurrency": "link",    "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "UNIUSDT",     "srcCurrency": "uni",     "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "BNBUSDT",     "srcCurrency": "bnb",     "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": False, "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "1KSHIBUSDT",  "srcCurrency": "1k_shib", "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "AAVEUSDT",    "srcCurrency": "aave",    "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "SUSDT",       "srcCurrency": "s",       "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "ATOMUSDT",    "srcCurrency": "atom",    "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "NEARUSDT",    "srcCurrency": "near",    "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "WLDUSDT",     "srcCurrency": "wld",     "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "1MPEPEUSDT",  "srcCurrency": "1m_pepe", "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "XAUTUSDT",    "srcCurrency": "xaut",    "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "GRAMUSDT",    "srcCurrency": "gram",    "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "SUIUSDT",     "srcCurrency": "sui",     "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "HYPEUSDT",    "srcCurrency": "hype",    "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
            {"symbol": "ADAUSDT",     "srcCurrency": "ada",     "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
        ]
        return {"ok": True, "markets": fallback, "source": "fallback"}


    # ── Wallet ─────────────────────────────────────────────────────────────────────
    # ── Live-bot margin USDT budgeting ───────────────────────────────────────────
    def _margin_usdt_from(margin: dict) -> float:
        """Free USDT in a margin-wallet dict: balance - blocked."""
        usdt = margin.get("USDT") or margin.get("usdt") or {}
        try:
            return float(usdt.get("balance") or 0) - float(usdt.get("blocked") or 0)
        except (TypeError, ValueError):
            return 0.0


    def _margin_usdt_free() -> Optional[float]:
        """Free margin (تعهدی) USDT, or None if it can't be read (no creds / API error)."""
        try:
            client = _client()
            return _margin_usdt_from(client.wallets("margin"))
        except Exception:
            return None


    def _committed_real_credit(exclude_id: Optional[str] = None) -> float:
        """Sum of credit committed by real-mode bots that are currently running."""
        total = 0.0
        for b in bot_manager.get_all():
            if b.id == exclude_id:
                continue
            if b.cfg.mode == "real" and b.status == BotStatus.RUNNING:
                total += float(b.cfg.credit)
        return total



    @app.get("/api/wallet")
    async def get_wallet():
        try:
            client = _client()
            spot = await asyncio.to_thread(client.wallets, "spot")
            margin = await asyncio.to_thread(client.wallets, "margin")
            usdt_data = margin.get("USDT") or margin.get("usdt") or {}
            balance_raw = float(usdt_data.get("balance", 0) or 0)
            blocked_raw = float(usdt_data.get("blocked", 0) or 0)
            available = balance_raw - blocked_raw
            committed = _committed_real_credit()
            budget = {
                "available": round(available, 2),
                "committed": round(committed, 2),
                "remaining": round(available - committed, 2),
            }
            return {
                "ok": True,
                "spot": spot,
                "margin": margin,
                "usdt_budget": budget,
                "usdt_available": round(available, 2),
                "usdt_locked_in": round(blocked_raw, 2),
            }
        except ValueError:
            return {"ok": False, "error": "no_credentials"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/portfolio")
    async def get_portfolio():
        try:
            snapshot = await asyncio.to_thread(_portfolio_snapshot)
            error = None
        except ValueError:
            snapshot, error = None, "Connect exchange credentials to value the portfolio"
        except Exception:
            snapshot, error = None, "Could not refresh exchange balances or prices"
        targets = bot_manager.store.get("portfolio_settings", "targets", {})
        ideas = []
        if snapshot and targets:
            assumptions = execution_config(bot_manager.store.get("prefs", "execution", {}))
            fee = max(.002, assumptions.entry_fee_rate)
            slippage = max(.002, assumptions.slippage_bps / 10000)
            ideas = await asyncio.to_thread(rebalance_ideas, snapshot, targets, _client().orderbook, fee, slippage)
        return {"ok": snapshot is not None, "error": error, "snapshot": snapshot,
                "history": portfolio_history.performance(), "cashflows": portfolio_history.flows(),
                "targets": targets, "rebalance_ideas": ideas}

    @app.post("/api/portfolio/snapshot")
    async def save_portfolio_snapshot():
        try:
            return {"ok": True, "snapshot": await asyncio.to_thread(_portfolio_snapshot, True)}
        except Exception:
            return JSONResponse({"error": "Could not capture portfolio snapshot"}, status_code=503)

    @app.post("/api/portfolio/cashflows")
    async def add_portfolio_cashflow(request: PortfolioFlowRequest):
        if request.kind not in {"deposit", "withdrawal"}:
            return JSONResponse({"error": "Choose deposit or withdrawal"}, status_code=400)
        try:
            amount = request.amount_usdt if request.kind == "deposit" else -request.amount_usdt
            flow_id = portfolio_history.add_flow(amount, request.occurred_at, request.note)
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid cash-flow timestamp or amount"}, status_code=400)
        return {"ok": True, "id": flow_id}

    @app.delete("/api/portfolio/cashflows/{flow_id}")
    async def delete_portfolio_cashflow(flow_id: int):
        if not portfolio_history.delete_flow(flow_id):
            return JSONResponse({"error": "Cash flow not found"}, status_code=404)
        return {"ok": True}

    @app.post("/api/portfolio/targets")
    async def save_portfolio_targets(request: PortfolioTargetsRequest):
        try:
            targets = validate_targets(request.targets)
        except (ValueError, TypeError):
            return JSONResponse({"error": "Targets must be 0–35% each, at most 80% in total"}, status_code=400)
        bot_manager.store.put("portfolio_settings", "targets", targets)
        return {"ok": True, "targets": targets}

    # ── Settings ───────────────────────────────────────────────────────────────────
    @app.get("/api/settings/credentials")
    async def get_credentials():
        values = credential_store.get() if credential_store is not None else {
            "token": os.getenv("NOBITEX_TOKEN"), "public_key": os.getenv("NOBITEX_API_PUBLIC_KEY"),
            "private_key": os.getenv("NOBITEX_API_PRIVATE_KEY"),
            "testnet": os.getenv("NOBITEX_TESTNET", "false").lower() == "true"}
        has_token = bool(values.get("token"))
        has_api_key = bool(values.get("public_key") and values.get("private_key"))
        testnet = bool(values.get("testnet"))
        if has_api_key:
            auth_type = "api_key"
        elif has_token:
            auth_type = "token"
        else:
            auth_type = None
        return {"has_credentials": bool(auth_type), "auth_type": auth_type, "testnet": testnet}


    @app.post("/api/settings/credentials")
    async def set_credentials(req: CredentialsRequest):
        for bot in bot_manager.get_all():
            if bot.cfg.mode == "real" and (bot.status == BotStatus.RUNNING
                    or bot._exchange_position_id is not None or bot.unresolved_intents()):
                return JSONResponse({"error": "Stop real bots and reconcile or close their positions and pending orders before changing credentials"}, status_code=409)
        values = (req.token, req.public_key, req.private_key)
        # Validate the complete replacement before touching either disk or process state.
        if any(len(v) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in v) for v in values):
            return JSONResponse({"error": "Invalid credential format"}, status_code=400)
        token_mode = bool(req.token) and not req.public_key and not req.private_key
        key_mode = not req.token and bool(req.public_key) and bool(req.private_key)
        if not (token_mode or key_mode):
            return JSONResponse({"error": "Provide a token or a public/private key pair"}, status_code=400)
        if key_mode:
            try:
                import base64
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
                raw = base64.b64decode(req.private_key + "=" * (-len(req.private_key) % 4), altchars=b"-_", validate=True)
                Ed25519PrivateKey.from_private_bytes(raw)
            except (ValueError, TypeError):
                return JSONResponse({"error": "Invalid credential format"}, status_code=400)
        try:
            if credential_store is not None:
                credential_store.set({"token": req.token, "public_key": req.public_key,
                                      "private_key": req.private_key, "testnet": req.testnet})
            else:
                update_environment({
                    "NOBITEX_TOKEN": req.token or None,
                    "NOBITEX_API_PUBLIC_KEY": req.public_key or None,
                    "NOBITEX_API_PRIVATE_KEY": req.private_key or None,
                    "NOBITEX_TESTNET": str(req.testnet).lower(),
                })
            for bot in bot_manager.get_all():
                bot._exchange_client = None
                bot._account = None
        except (OSError, ValueError):
            return JSONResponse({"error": "Could not save credentials; previous settings retained"}, status_code=500)
        return {"ok": True, **await get_credentials()}


    # ── Strategy Tester ────────────────────────────────────────────────────────────
    @app.get("/api/tester/config")
    async def tester_config():
        """Saved preferences plus the available strategies/timeframes and run limits."""
        return tester_manager.get_config()


    @app.post("/api/tester/prefs")
    async def tester_save_prefs(req: TesterPrefsRequest):
        tester_manager.save_prefs(
            req.favorites, req.strategies, req.timeframes, req.credit, req.leverage
        )
        return {"ok": True}


    @app.post("/api/tester/run")
    async def tester_run(req: TesterRunRequest):
        combos = tester_manager.build_combos(req.symbols, req.strategies, req.timeframes)
        if not combos:
            return JSONResponse(
                status_code=400,
                content={"error": "Select at least one asset, strategy, and timeframe."},
            )
        from backtester import _MAX_COMBOS
        if len(combos) > _MAX_COMBOS:
            return JSONResponse(
                status_code=400,
                content={"error": f"{len(combos)} combinations exceeds the limit of {_MAX_COMBOS}."},
            )
        # Persist this selection so the tab reopens where the user left off.
        tester_manager.save_prefs(req.symbols, req.strategies, req.timeframes, req.credit, req.leverage)
        job = tester_manager.start_job(combos, req.credit, req.leverage, execution=selected_execution(req.execution))
        return {"job_id": job.id, "total": job.total}


    @app.get("/api/tester/run/{job_id}")
    async def tester_run_status(job_id: str):
        job = tester_manager.get_job(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"error": "Job not found"})
        return job.to_dict()


    @app.post("/api/tester/run/{job_id}/cancel")
    async def tester_run_cancel(job_id: str):
        job = tester_manager.get_job(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"error": "Job not found"})
        job.cancel()
        return {"ok": True}


    # ── Optimizer ──────────────────────────────────────────────────────────────────
    def _start_optimizer_job(*args, **kwargs):
        if optimizer_slots is not None and not optimizer_slots.try_start():
            return None
        try:
            job = optimizer_manager.start_job(*args, **kwargs)
        except Exception:
            if optimizer_slots is not None:
                optimizer_slots.finish()
            raise
        if optimizer_slots is not None:
            job._task.add_done_callback(lambda task: optimizer_slots.finish())
        return job

    @app.get("/api/optimizer/config")
    async def optimizer_config():
        """Saved preferences plus available strategies (with param search ranges)."""
        return optimizer_manager.get_config()


    @app.post("/api/optimizer/prefs")
    async def optimizer_save_prefs(req: OptimizerPrefsRequest):
        optimizer_manager.save_prefs(
            req.symbol, req.strategy, req.timeframe, req.candles, req.credit, req.leverage,
            req.cores, req.maxiter, req.objective, req.custom_bounds, req.min_price_change, req.balanced_cap, req.balanced_weight,
        )
        return {"ok": True}


    @app.post("/api/optimizer/run")
    async def optimizer_run(req: OptimizerRunRequest):
        from bot import STRATEGY_MAP
        from optimizer import PARAM_BOUNDS
        if req.strategy not in STRATEGY_MAP or req.strategy not in PARAM_BOUNDS:
            return JSONResponse(status_code=400, content={"error": "Unknown strategy."})
        if not req.symbol.strip():
            return JSONResponse(status_code=400, content={"error": "Select a symbol."})
        if req.custom_bounds:
            for name, val in req.custom_bounds.items():
                if not isinstance(val, (list, tuple)) or len(val) != 2:
                    return JSONResponse(status_code=400, content={"error": f"Bad bounds for '{name}': expected [min, max]."})
                try:
                    lo, hi = int(val[0]), int(val[1])
                except (TypeError, ValueError):
                    return JSONResponse(status_code=400, content={"error": f"Bad bounds for '{name}': values must be integers."})
                if lo >= hi:
                    return JSONResponse(status_code=400, content={"error": f"Parameter '{name}': min ({lo}) must be less than max ({hi})."})
        # Persist the selection so the tab reopens where the user left off.
        optimizer_manager.save_prefs(
            req.symbol, req.strategy, req.timeframe, req.candles, req.credit, req.leverage,
            req.cores, req.maxiter, req.objective, req.custom_bounds, req.min_price_change, req.balanced_cap, req.balanced_weight,
        )
        job = _start_optimizer_job(
            req.symbol, req.strategy, req.timeframe, req.candles, req.credit, req.leverage,
            req.cores, req.maxiter, req.objective, req.custom_bounds, req.min_price_change, req.balanced_cap, req.balanced_weight,
            execution=selected_execution(req.execution), random_seed=req.random_seed,
        )
        if job is None:
            return JSONResponse({"error": "Optimizer capacity is busy; try again later"}, status_code=429)
        return {"job_id": job.id}


    @app.get("/api/optimizer/run/{job_id}")
    async def optimizer_run_status(job_id: str):
        job = optimizer_manager.get_job(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"error": "Job not found"})
        return job.to_dict()


    @app.post("/api/optimizer/run/{job_id}/cancel")
    async def optimizer_run_cancel(job_id: str):
        job = optimizer_manager.get_job(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"error": "Job not found"})
        job.cancel()
        return {"ok": True}


    # ── Optimizer Tester ───────────────────────────────────────────────────────────
    @app.post("/api/optimizer/tester/run")
    async def optimizer_tester_run(req: OptimizerTesterRunRequest):
        from bot import STRATEGY_MAP
        from optimizer import PARAM_BOUNDS
        if req.strategy not in STRATEGY_MAP or req.strategy not in PARAM_BOUNDS:
            return JSONResponse(status_code=400, content={"error": "Unknown strategy."})
        if not req.symbol.strip():
            return JSONResponse(status_code=400, content={"error": "Select a symbol."})
        if req.custom_bounds:
            for name, val in req.custom_bounds.items():
                if not isinstance(val, (list, tuple)) or len(val) != 2:
                    return JSONResponse(status_code=400, content={"error": f"Bad bounds for '{name}': expected [min, max]."})
                try:
                    lo, hi = int(val[0]), int(val[1])
                except (TypeError, ValueError):
                    return JSONResponse(status_code=400, content={"error": f"Bad bounds for '{name}': values must be integers."})
                if lo >= hi:
                    return JSONResponse(status_code=400, content={"error": f"Parameter '{name}': min ({lo}) must be less than max ({hi})."})
        job = _start_optimizer_job(
            req.symbol, req.strategy, req.timeframe, req.candles, req.credit, req.leverage,
            req.cores, req.maxiter, req.objective, req.custom_bounds, req.min_price_change, req.balanced_cap, req.balanced_weight,
            train_pct=req.train_pct,
            execution=selected_execution(req.execution), random_seed=req.random_seed,
        )
        if job is None:
            return JSONResponse({"error": "Optimizer capacity is busy; try again later"}, status_code=429)
        return {"job_id": job.id}


    @app.get("/api/optimizer/tester/run/{job_id}")
    async def optimizer_tester_run_status(job_id: str):
        job = optimizer_manager.get_job(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"error": "Job not found"})
        return job.to_dict()


    @app.post("/api/optimizer/tester/run/{job_id}/cancel")
    async def optimizer_tester_run_cancel(job_id: str):
        job = optimizer_manager.get_job(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"error": "Job not found"})
        job.cancel()
        return {"ok": True}


    # ── Bulk bot actions (must be registered before {bot_id} routes) ───────────────
    def selected_execution(value=None):
        saved = bot_manager.store.get("prefs", "execution", {}) if value is None else value
        return asdict(execution_config(saved))


    @app.get("/api/settings/execution")
    async def get_execution_settings():
        return selected_execution()

    @app.get("/api/settings/execution/nobitex")
    async def get_nobitex_execution_settings(symbol: str):
        market = symbol.upper()
        if not re.fullmatch(r"[A-Z0-9]{3,30}(?:USDT|IRT)", market):
            return JSONResponse(status_code=400, content={"error": "Choose a valid Nobitex market"})
        try:
            client = _client()
        except ValueError:
            return JSONResponse(status_code=503, content={"error": "Nobitex credentials are unavailable"})

        values, sources, warnings = {}, {}, []
        try:
            profile = await asyncio.to_thread(client.profile)
            fee, source = profile_fee(profile, market)
            values.update(entry_fee_rate=fee, exit_fee_rate=fee)
            sources.update(entry_fee_rate=source, exit_fee_rate=source)
        except (KeyError, TypeError, ValueError, AttributeError):
            warnings.append("Account trading fee was absent or invalid in the Nobitex profile response")
        except Exception:
            warnings.append("Nobitex account profile could not be read")

        try:
            markets = await asyncio.to_thread(client.margin_markets)
            rate, source = market_renewal_rate(markets, market)
            values.update(holding_rate_per_day=rate,
                          holding_policy="entry_notional_daily" if rate > 0 else "none")
            sources.update(holding_rate_per_day=source,
                           holding_policy={"kind": "simulation_mapping", "documentation": MARGIN_DOC})
        except (KeyError, TypeError, ValueError, AttributeError):
            warnings.append("Daily renewal rate was absent or invalid for this Nobitex market")
        except Exception:
            warnings.append("Nobitex margin market settings could not be read")

        if not values:
            return JSONResponse(status_code=503, content={"error": "; ".join(warnings)})
        values["liquidation_ratio"] = 1.1
        sources["liquidation_ratio"] = {"kind": "documentation", "documentation": MARGIN_DOC,
                                        "detail": "Published minimum commitment ratio; simulator approximation"}
        return {"ok": True, "symbol": market, "values": values, "sources": sources,
                "warnings": warnings, "fetched_at": datetime.now(timezone.utc).isoformat()}


    @app.post("/api/settings/execution")
    async def save_execution_settings(value: dict):
        try:
            validated = asdict(execution_config(value))
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"error": "Invalid execution assumptions"})
        bot_manager.store.put("prefs", "execution", validated)
        return validated


    @app.get("/api/tester/jobs")
    async def tester_jobs():
        return tester_manager.list_jobs()


    @app.get("/api/optimizer/jobs")
    async def optimizer_jobs():
        return optimizer_manager.list_jobs()


    @app.post("/api/bots/{bot_id}/reconcile")
    async def reconcile_bot(bot_id: str):
        from exchange_execution import OrderExecutor
        bot = bot_manager.get_bot(bot_id)
        if bot is None:
            return JSONResponse(status_code=404, content={"error": "Bot not found"})
        if bot.status == BotStatus.RUNNING or bot.cfg.mode != "real":
            return JSONResponse(status_code=409, content={"error": "Stop the real bot before manual reconciliation"})
        if not bot._ensure_exchange_client():
            return JSONResponse(status_code=400, content={"error": "Exchange credentials unavailable"})
        await OrderExecutor(bot, bot._exchange_client).reconcile()
        bot.error = "Unresolved exchange order; verify its ID on the exchange" if bot.unresolved_intents() else None
        bot._persist()
        return bot.to_detail_dict()


    @app.post("/api/bots/{bot_id}/adopt-position")
    async def adopt_position(bot_id: str, request: dict):
        bot = bot_manager.get_bot(bot_id)
        if bot is None:
            return JSONResponse(status_code=404, content={"error": "Bot not found"})
        if bot.cfg.mode != "real" or bot.status == BotStatus.RUNNING or bot._exchange_position_id is not None or bot.unresolved_intents():
            return JSONResponse(status_code=409, content={"error": "Only a stopped real bot without exposure can adopt a position"})
        position_id = request.get("position_id")
        if not isinstance(position_id, int) or isinstance(position_id, bool) or position_id <= 0:
            return JSONResponse(status_code=400, content={"error": "A positive exchange position ID is required"})
        if not bot._ensure_exchange_client():
            return JSONResponse(status_code=400, content={"error": "Exchange credentials unavailable"})
        position = await asyncio.to_thread(bot._exchange_client.position, position_id)
        src, dst = bot._parse_symbol()
        if position.get("id") != position_id or position.get("status") != "Open" or position.get("srcCurrency") != src or position.get("dstCurrency") != dst:
            return JSONResponse(status_code=400, content={"error": "Position must be open and match the bot market"})
        bot_manager.store.claim_position(bot._account_key(), position_id, bot.id)
        bot._exchange_position_id = position_id
        bot._real_state = "long" if position.get("side") == "buy" else "short"
        bot._seed_position = {"direction": "Long" if position.get("side") == "buy" else "Short",
                              "entry_price": float(position["entryPrice"]), "entry_time": position["openedAt"]}
        bot._log_event("reconnected", f"Operator assigned exchange position {position_id}")
        bot._persist()
        return bot.to_detail_dict()


    @app.post("/api/bots/{bot_id}/resolve-order")
    async def resolve_order(bot_id: str, request: dict):
        from exchange_execution import OrderExecutor
        bot = bot_manager.get_bot(bot_id)
        if bot is None:
            return JSONResponse(status_code=404, content={"error": "Bot not found"})
        if bot.cfg.mode != "real" or bot.status == BotStatus.RUNNING:
            return JSONResponse(status_code=409, content={"error": "Stop the real bot before resolving an order"})
        client_order_id, order_id = request.get("client_order_id"), request.get("order_id")
        if not isinstance(client_order_id, str) or len(client_order_id) > 64:
            return JSONResponse(status_code=400, content={"error": "A valid local order intent ID is required"})
        if not bot._ensure_exchange_client():
            return JSONResponse(status_code=400, content={"error": "Exchange credentials unavailable"})
        try:
            await OrderExecutor(bot, bot._exchange_client).bind_order(client_order_id, order_id)
        except (TypeError, ValueError) as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        return bot.to_detail_dict()


    @app.post("/api/bots/actions/start-all")
    async def start_all_bots():
        # Simulation bots start in parallel (no budget constraint).
        sims = [b for b in bot_manager.get_all() if b.cfg.mode != "real" and b.cfg.strategy_name not in ("AgenticAI", "PriceAI")]
        await asyncio.gather(*[b.start() for b in sims])

        reals = [b for b in bot_manager.get_all() if b.cfg.mode == "real" and b.cfg.strategy_name not in ("AgenticAI", "PriceAI") and b.status != BotStatus.RUNNING]
        for b in reals:
            await b.start()
        return {"ok": True, "blocked": []}


    @app.post("/api/bots/actions/stop-all")
    async def stop_all_bots():
        await asyncio.gather(*[bot.stop() for bot in bot_manager.get_all()])
        return {"ok": True}


    # ── REST Endpoints ─────────────────────────────────────────────────────────────
    @app.get("/api/bots")
    async def list_bots():
        return bot_manager.get_all_states()


    @app.post("/api/bots", status_code=201)
    async def create_bot(req: CreateBotRequest):
        if req.mode not in ("simulation", "real"):
            return JSONResponse(status_code=400, content={"error": "mode must be 'simulation' or 'real'"})
        if req.strategy_name == "PriceAI" and req.mode != "simulation":
            return JSONResponse(status_code=400, content={"error": "PriceAI is simulation-only"})
        cfg = BotConfig(
            symbol=req.symbol.lower().replace("/", ""),
            strategy_name=req.strategy_name,
            timeframe=req.timeframe,
            credit=req.credit,
            leverage=req.leverage,
            mode=req.mode,
            name=req.name,
            custom_params=req.custom_params or None,
            execution=selected_execution(req.execution),
            ai_timeframes=req.ai_timeframes,
        )
        try:
            bot = bot_manager.create_bot(cfg)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        if exchange_client is not None:
            bot._exchange_client = exchange_client
        return bot.to_state_dict()


    @app.get("/api/bots/{bot_id}")
    async def get_bot(bot_id: str):
        bot = bot_manager.get_bot(bot_id)
        if bot is None:
            return JSONResponse(status_code=404, content={"error": "Bot not found"})
        return bot.to_detail_dict()


    @app.patch("/api/bots/{bot_id}")
    async def edit_bot(bot_id: str, req: PatchBotRequest):
        bot = bot_manager.get_bot(bot_id)
        if bot is None:
            return JSONResponse(status_code=404, content={"error": "Bot not found"})
        new_mode = req.mode if req.mode in ("simulation", "real") else bot.cfg.mode
        new_strategy = req.strategy_name or bot.cfg.strategy_name
        if new_strategy == "PriceAI" and new_mode != "simulation":
            return JSONResponse(status_code=400, content={"error": "PriceAI is simulation-only"})
        new_cfg = BotConfig(
            id=bot.id,
            symbol=(req.symbol or bot.cfg.symbol).lower().replace("/", ""),
            strategy_name=new_strategy,
            timeframe=req.timeframe or bot.cfg.timeframe,
            credit=req.credit if req.credit is not None else bot.cfg.credit,
            leverage=req.leverage if req.leverage is not None else bot.cfg.leverage,
            mode=new_mode,
            name=req.name if req.name is not None else bot.cfg.name,
            custom_params=bot.cfg.custom_params,  # preserve existing optimized inputs
            execution=bot.cfg.execution,
            ai_timeframes=req.ai_timeframes if req.ai_timeframes is not None else bot.cfg.ai_timeframes,
        )
        try:
            Bot(new_cfg)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        was_running = bot.status.value == "running"
        if was_running:
            await bot.stop()
        try:
            bot_manager.update_bot(bot, new_cfg)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

        if req.restart or was_running:
            await bot.start()

        return bot.to_state_dict()


    @app.post("/api/bots/{bot_id}/duplicate", status_code=201)
    async def duplicate_bot(bot_id: str):
        src = bot_manager.get_bot(bot_id)
        if src is None:
            return JSONResponse(status_code=404, content={"error": "Bot not found"})
        cfg = BotConfig(
            symbol=src.cfg.symbol,
            strategy_name=src.cfg.strategy_name,
            timeframe=src.cfg.timeframe,
            credit=src.cfg.credit,
            leverage=src.cfg.leverage,
            mode=src.cfg.mode,
            name=f"{src.cfg.name or src.id} (copy)",
            custom_params=src.cfg.custom_params,
            execution=src.cfg.execution,
            ai_timeframes=src.cfg.ai_timeframes,
        )
        bot = bot_manager.create_bot(cfg)
        return bot.to_state_dict()


    @app.get("/api/bots/{bot_id}/chart")
    async def bot_chart(bot_id: str):
        bot = bot_manager.get_bot(bot_id)
        if bot is None:
            return JSONResponse(status_code=404, content={"error": "Bot not found"})
        try:
            return bot.get_chart_data()
        except Exception as exc:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


    @app.get("/api/bots/{bot_id}/backtest")
    async def backtest_bot(bot_id: str):
        bot = bot_manager.get_bot(bot_id)
        if bot is None:
            return JSONResponse(status_code=404, content={"error": "Bot not found"})
        try:
            result = await bot.run_backtest()
            return {"ok": True, **result}
        except Exception as exc:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


    @app.post("/api/bots/{bot_id}/start")
    async def start_bot(bot_id: str):
        bot = bot_manager.get_bot(bot_id)
        if bot is None:
            return JSONResponse(status_code=404, content={"error": "Bot not found"})
        await bot.start()
        return bot.to_state_dict()


    @app.post("/api/bots/{bot_id}/stop")
    async def stop_bot(bot_id: str):
        bot = bot_manager.get_bot(bot_id)
        if bot is None:
            return JSONResponse(status_code=404, content={"error": "Bot not found"})
        await bot.stop()
        return bot.to_state_dict()


    @app.delete("/api/bots/{bot_id}", status_code=204)
    async def delete_bot(bot_id: str):
        bot = bot_manager.get_bot(bot_id)
        if bot is None:
            return JSONResponse(status_code=404, content={"error": "Bot not found"})
        await bot.stop()
        bot_manager.remove_bot(bot_id)

    app.state.bot_manager = bot_manager
    app.state.tester_manager = tester_manager
    app.state.optimizer_manager = optimizer_manager
    return app


load_environment()
app = create_app()
# Compatibility for scripts that imported the default registry/credential handler.
bot_manager = app.state.bot_manager
set_credentials = next(route.endpoint for route in app.routes if getattr(route, "name", None) == "set_credentials")
