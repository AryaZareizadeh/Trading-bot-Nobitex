import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bot import BotConfig, BotStatus
from bot_manager import bot_manager
from backtester import tester_manager
from optimizer import optimizer_manager

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
            from nobitex_client import NobitexClient
            testnet = os.getenv("NOBITEX_TESTNET", "false").lower() == "true"
            client = NobitexClient(testnet=testnet)
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
        await asyncio.sleep(30)


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
    bot_manager.load()
    broadcast_task = asyncio.create_task(_broadcast_loop())
    exchange_task  = asyncio.create_task(_exchange_sync_loop())
    yield
    broadcast_task.cancel()
    exchange_task.cancel()


# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Trading Bot Dashboard", lifespan=lifespan)

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


class PatchBotRequest(BaseModel):
    name: Optional[str] = None
    symbol: Optional[str] = None
    strategy_name: Optional[str] = None
    timeframe: Optional[str] = None
    credit: Optional[float] = None
    leverage: Optional[int] = None
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


class OptimizerPrefsRequest(BaseModel):
    symbol: str = ""
    strategy: str = ""
    timeframe: str = "240"
    candles: int = 1000
    credit: float = 100.0
    leverage: int = Field(default=5, ge=1, le=100)
    cores: int = Field(default=0, ge=0)   # 0 = Auto
    maxiter: int = Field(default=30, ge=5, le=500)
    objective: str = "profit"             # profit | winrate | balanced | large_move
    custom_bounds: Optional[dict] = None  # param_name -> [low, high]
    min_price_change: float = Field(default=5.0, gt=0, le=100)


class OptimizerRunRequest(BaseModel):
    symbol: str
    strategy: str
    timeframe: str
    candles: int = Field(default=1000, ge=50)
    credit: float = Field(default=100.0, gt=0)
    leverage: int = Field(default=5, ge=1, le=100)
    cores: int = Field(default=0, ge=0)   # 0 = Auto
    maxiter: int = Field(default=30, ge=5, le=500)
    objective: str = "profit"             # profit | winrate | balanced | large_move
    custom_bounds: Optional[dict] = None  # param_name -> [low, high]
    min_price_change: float = Field(default=5.0, gt=0, le=100)


class OptimizerTesterRunRequest(OptimizerRunRequest):
    train_pct: float = Field(default=70.0, ge=50.0, le=90.0)  # % of candles for training


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
        from nobitex_client import NobitexClient
        testnet = os.getenv("NOBITEX_TESTNET", "false").lower() == "true"
        client = NobitexClient(testnet=testnet)
        raw = await asyncio.to_thread(client.margin_markets)
        return {"ok": True, "markets": _parse(raw), "source": "live_auth"}
    except ValueError:
        pass
    except Exception:
        pass

    # Hardcoded fallback — known Nobitex margin symbols as of mid-2025
    fallback = [
        {"symbol": "BTCUSDT",  "srcCurrency": "btc",  "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
        {"symbol": "ETHUSDT",  "srcCurrency": "eth",  "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
        {"symbol": "LTCUSDT",  "srcCurrency": "ltc",  "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
        {"symbol": "BCHUSDT",  "srcCurrency": "bch",  "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
        {"symbol": "XRPUSDT",  "srcCurrency": "xrp",  "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
        {"symbol": "ZECUSDT",  "srcCurrency": "zec",  "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
        {"symbol": "DOGEUSDT", "srcCurrency": "doge", "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
        {"symbol": "SOLUSDT",  "srcCurrency": "sol",  "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
        {"symbol": "LINKUSDT", "srcCurrency": "link", "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
        {"symbol": "UNIUSDT",  "srcCurrency": "uni",  "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
        {"symbol": "BNBUSDT",  "srcCurrency": "bnb",  "dstCurrency": "usdt", "maxLeverage": 5, "buyEnabled": False, "sellEnabled": True,  "feeRate": "0.001"},
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
        from nobitex_client import NobitexClient
        testnet = os.getenv("NOBITEX_TESTNET", "false").lower() == "true"
        client = NobitexClient(testnet=testnet)
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
        from nobitex_client import NobitexClient
        testnet = os.getenv("NOBITEX_TESTNET", "false").lower() == "true"
        client = NobitexClient(testnet=testnet)
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


# ── Settings ───────────────────────────────────────────────────────────────────
@app.get("/api/settings/credentials")
async def get_credentials():
    has_token = bool(os.getenv("NOBITEX_TOKEN"))
    has_api_key = bool(os.getenv("NOBITEX_API_PUBLIC_KEY") and os.getenv("NOBITEX_API_PRIVATE_KEY"))
    testnet = os.getenv("NOBITEX_TESTNET", "false").lower() == "true"
    if has_api_key:
        auth_type = "api_key"
    elif has_token:
        auth_type = "token"
    else:
        auth_type = None
    return {"has_credentials": bool(auth_type), "auth_type": auth_type, "testnet": testnet}


@app.post("/api/settings/credentials")
async def set_credentials(req: CredentialsRequest):
    env_path = Path(__file__).parent / ".env"

    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()

    # Clear old credentials
    for key in ("NOBITEX_TOKEN", "NOBITEX_API_PUBLIC_KEY", "NOBITEX_API_PRIVATE_KEY"):
        existing.pop(key, None)
        os.environ.pop(key, None)

    if req.testnet:
        existing["NOBITEX_TESTNET"] = "true"
        os.environ["NOBITEX_TESTNET"] = "true"
    else:
        existing.pop("NOBITEX_TESTNET", None)
        os.environ.pop("NOBITEX_TESTNET", None)

    if req.token:
        existing["NOBITEX_TOKEN"] = req.token
        os.environ["NOBITEX_TOKEN"] = req.token
    elif req.public_key and req.private_key:
        existing["NOBITEX_API_PUBLIC_KEY"] = req.public_key
        existing["NOBITEX_API_PRIVATE_KEY"] = req.private_key
        os.environ["NOBITEX_API_PUBLIC_KEY"] = req.public_key
        os.environ["NOBITEX_API_PRIVATE_KEY"] = req.private_key
    else:
        return JSONResponse(status_code=400, content={"error": "Provide token or public_key+private_key"})

    env_path.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n", encoding="utf-8")

    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except ImportError:
        pass

    return {"ok": True}


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
    job = tester_manager.start_job(combos, req.credit, req.leverage)
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
@app.get("/api/optimizer/config")
async def optimizer_config():
    """Saved preferences plus available strategies (with param search ranges)."""
    return optimizer_manager.get_config()


@app.post("/api/optimizer/prefs")
async def optimizer_save_prefs(req: OptimizerPrefsRequest):
    optimizer_manager.save_prefs(
        req.symbol, req.strategy, req.timeframe, req.candles, req.credit, req.leverage,
        req.cores, req.maxiter, req.objective, req.custom_bounds, req.min_price_change,
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
        req.cores, req.maxiter, req.objective, req.custom_bounds, req.min_price_change,
    )
    job = optimizer_manager.start_job(
        req.symbol, req.strategy, req.timeframe, req.candles, req.credit, req.leverage,
        req.cores, req.maxiter, req.objective, req.custom_bounds, req.min_price_change,
    )
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
    job = optimizer_manager.start_job(
        req.symbol, req.strategy, req.timeframe, req.candles, req.credit, req.leverage,
        req.cores, req.maxiter, req.objective, req.custom_bounds, req.min_price_change,
        train_pct=req.train_pct,
    )
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
@app.post("/api/bots/actions/start-all")
async def start_all_bots():
    # Simulation bots start in parallel (no budget constraint).
    sims = [b for b in bot_manager.get_all() if b.cfg.mode != "real"]
    await asyncio.gather(*[b.start() for b in sims])

    reals = [b for b in bot_manager.get_all() if b.cfg.mode == "real" and b.status != BotStatus.RUNNING]
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
    cfg = BotConfig(
        symbol=req.symbol.lower().replace("/", ""),
        strategy_name=req.strategy_name,
        timeframe=req.timeframe,
        credit=req.credit,
        leverage=req.leverage,
        mode=req.mode,
        name=req.name,
        custom_params=req.custom_params or None,
    )
    bot = bot_manager.create_bot(cfg)
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
    was_running = bot.status.value == "running"
    if was_running:
        await bot.stop()

    new_mode = req.mode if req.mode in ("simulation", "real") else bot.cfg.mode
    new_cfg = BotConfig(
        id=bot.id,
        symbol=(req.symbol or bot.cfg.symbol).lower().replace("/", ""),
        strategy_name=req.strategy_name or bot.cfg.strategy_name,
        timeframe=req.timeframe or bot.cfg.timeframe,
        credit=req.credit if req.credit is not None else bot.cfg.credit,
        leverage=req.leverage if req.leverage is not None else bot.cfg.leverage,
        mode=new_mode,
        name=req.name if req.name is not None else bot.cfg.name,
        custom_params=bot.cfg.custom_params,  # preserve existing optimized inputs
    )
    bot.reconfigure(new_cfg)
    bot_manager._save()

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
    )
    bot = bot_manager.create_bot(cfg)
    return bot.to_state_dict()


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
