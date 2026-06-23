import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bot import BotConfig
from bot_manager import bot_manager

# ── WebSocket client registry ──────────────────────────────────────────────────
_clients: list[WebSocket] = []


async def _broadcast_loop() -> None:
    """Push bot states to all connected dashboard clients every second."""
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
    # Send current state immediately on connect
    try:
        await websocket.send_text(json.dumps(bot_manager.get_all_states()))
    except Exception:
        pass
    try:
        while True:
            await websocket.receive_text()  # keep the connection alive
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
