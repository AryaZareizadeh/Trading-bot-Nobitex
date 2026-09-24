"""Disposable browser QA server. All API data is synthetic and held in memory.

Run: python -m tests.ui_preview
Sign in with the public test password: correct horse battery staple
This module never loads .env, imports server, or submits exchange requests.
"""
import asyncio
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, HTTPException
from fastapi.responses import FileResponse
import uvicorn

from auth import AuthState, hash_password, install_auth

app = FastAPI()
install_auth(app, AuthState(password_hash=hash_password("correct horse battery staple"),
                            allowed_hosts={"127.0.0.1", "localhost"}))
execution = dict(entry_fee_rate=0, exit_fee_rate=0, slippage_bps=0,
                 holding_rate_per_day=0, holding_policy="none", margin_fraction=1,
                 position_mode="net", fill_timing="next_open", liquidation_ratio=1.1)


demo_bot = dict(id="demo", name="Recovery demo", mode="real", status="stopped", symbol="BTCUSDT",
                strategy="AdvRSI", timeframe="60", leverage=2, credit=100, balance=100,
                total_pnl=0, trade_count=0, candle_count=0, win_rate=0, max_drawdown=0,
                balance_history=[], open_positions=[], pending_order=None, logs=[],
                error="Stopped after restart; reconcile recorded state", current_price=None,
                last_update=None)
tester_job = dict(id="saved-tester", created_at=1789516800, status="done", total=1, completed=1,
                  execution=dict(execution), results=[dict(symbol="BTCUSDT", strategy="AdvRSI", strategy_label="AdvRSI",
                  timeframe="60", total_pnl=3.5, trade_count=2, win_rate=50, profitable=True,
                  candle_count=100, sum_profits=5, sum_losses=-1.5, max_drawdown=1.5)])
optimizer_job = dict(id="saved-optimizer", created_at=1789516800, status="interrupted",
                     error="Interrupted by server restart", execution=dict(execution),
                     symbol="BTCUSDT", strategy="AdvRSI", timeframe="60", train_pct=None,
                     iteration=1, maxiter=10, history=[], best_pnl=None, params=[], best_summary=None)


@app.get("/api/{kind}/config")
async def config(kind: str):
    return dict(available_strategies=[], available_timeframes=[], favorites=[], strategies=[], timeframes=[])


@app.get("/api/{kind}/jobs")
async def jobs(kind: str):
    return [tester_job] if kind == "tester" else [optimizer_job]


@app.get("/api/{kind}/run/{job_id}")
async def job(kind: str, job_id: str):
    return tester_job if kind == "tester" else optimizer_job


@app.post("/api/bots/demo/reconcile")
async def reconcile():
    demo_bot["error"] = None
    return demo_bot


@app.post("/api/bots/demo/adopt-position")
async def adopt(request: Request):
    if (await request.json()).get("position_id") != 123:
        raise HTTPException(400, "Synthetic preview position must be 123")
    demo_bot["error"] = None
    return demo_bot


@app.get("/")
async def index():
    return FileResponse(Path(__file__).resolve().parents[1] / "static" / "index.html")


@app.get("/api/settings/execution")
async def get_execution():
    return execution


@app.post("/api/settings/execution")
async def save_execution(request: Request):
    execution.update(await request.json())
    return execution


@app.get("/api/wallet")
async def wallet():
    return {"ok": False, "error": "no_credentials"}


@app.get("/api/settings/credentials")
async def credentials():
    return {"has_credentials": False, "testnet": False}


@app.get("/api/margin-markets")
async def markets():
    return {"ok": True, "markets": []}


@app.websocket("/ws")
async def socket(ws: WebSocket):
    await ws.accept()
    await ws.send_json({"type": "state", "bots": [demo_bot], "exchange_positions": [], "exchange_orders": []})
    while True:
        await asyncio.sleep(1)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8098)
