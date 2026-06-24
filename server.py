import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bot import BotConfig
from bot_manager import bot_manager
from backtester import tester_manager

# ── WebSocket client registry ──────────────────────────────────────────────────
_clients: list[WebSocket] = []


async def _broadcast_loop() -> None:
    while True:
        if _clients:
            payload = json.dumps(bot_manager.get_all_states())
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
    task = asyncio.create_task(_broadcast_loop())
    yield
    task.cancel()


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
        await websocket.send_text(json.dumps(bot_manager.get_all_states()))
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


# ── Margin Markets ─────────────────────────────────────────────────────────────
@app.get("/api/margin-markets")
async def get_margin_markets():
    import requests as _req

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
            if info.get("buyEnabled") or info.get("sellEnabled")
        ]
        result.sort(key=lambda x: x["symbol"])
        return result

    # Try unauthenticated — /margin/markets/list is a public endpoint
    try:
        session = _req.Session()
        session.trust_env = False
        session.headers["User-Agent"] = "TraderBot/NobitexBot"
        resp = await asyncio.to_thread(
            lambda: session.get(
                "https://apiv2.nobitex.ir/margin/markets/list", timeout=10
            )
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") in ("ok", "success") and data.get("markets"):
            return {"ok": True, "markets": _parse(data["markets"]), "source": "live"}
    except Exception:
        pass

    # Try authenticated if credentials are available
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
        {"symbol": "BTCIRT",   "srcCurrency": "btc",  "dstCurrency": "rls",  "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
        {"symbol": "ETHIRT",   "srcCurrency": "eth",  "dstCurrency": "rls",  "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
        {"symbol": "LTCIRT",   "srcCurrency": "ltc",  "dstCurrency": "rls",  "maxLeverage": 5, "buyEnabled": True,  "sellEnabled": True,  "feeRate": "0.001"},
    ]
    return {"ok": True, "markets": fallback, "source": "fallback"}


# ── Wallet ─────────────────────────────────────────────────────────────────────
@app.get("/api/wallet")
async def get_wallet():
    try:
        from nobitex_client import NobitexClient
        testnet = os.getenv("NOBITEX_TESTNET", "false").lower() == "true"
        client = NobitexClient(testnet=testnet)
        spot = await asyncio.to_thread(client.wallets, "spot")
        margin = await asyncio.to_thread(client.wallets, "margin")
        return {"ok": True, "spot": spot, "margin": margin}
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


# ── Bulk bot actions (must be registered before {bot_id} routes) ───────────────
@app.post("/api/bots/actions/start-all")
async def start_all_bots():
    await asyncio.gather(*[bot.start() for bot in bot_manager.get_all()])
    return {"ok": True}


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
