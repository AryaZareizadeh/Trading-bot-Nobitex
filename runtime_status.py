"""Read-only authenticated operational diagnostics for one account."""
from datetime import datetime, timezone
import json
from fastapi import APIRouter
from research_scheduler import queue_status
from trading_audit import sanitized
import asyncio


def _age(value):
    if value is None:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return round(max(0, (datetime.now(timezone.utc) - stamp).total_seconds()), 1)
    except (ValueError, TypeError):
        return None


def runtime_status(manager, exchange_state, jobs):
    bots = []
    for bot in manager.get_all():
        runners = bot.execution_bots()
        bots.append(dict(id=bot.id, symbol=bot.cfg.symbol, timeframe=bot.cfg.timeframe,
                         mode=bot.cfg.mode, status=bot.status.value,
                         error=sanitized(bot.error) if bot.error else None,
                         update_age_seconds=_age(bot.last_updated),
                         candle_age_seconds=_age(getattr(bot, "_last_processed_ts", None)),
                         unresolved_intents=sum(len(runner.unresolved_intents()) for runner in runners)))
    with manager.store.connection() as conn:
        rows = conn.execute("SELECT value FROM records WHERE namespace='audit_events' "
                            "ORDER BY json_extract(value,'$.time') DESC LIMIT 100").fetchall()
    audit = [{key: sanitized(value) for key, value in json.loads(row[0]).items()
              if key in {"bot_id", "time", "event", "detail"}} for row in rows]
    return dict(captured_at=datetime.now(timezone.utc).isoformat(), bots=bots,
                exchange={"age_seconds": _age(exchange_state.get("last_synced")),
                          "error": sanitized(exchange_state["error"]) if exchange_state.get("error") else None},
                queues=queue_status(), jobs=[{key: job.get(key) for key in
                                             ("id", "status", "stage", "progress")} for job in jobs], audit=audit)


def status_router(manager, exchange_state, trainer):
    router = APIRouter()

    @router.get("/healthz")
    async def liveness():
        return {"ok": True}

    @router.get("/api/runtime/status")
    async def operational_status():
        return await asyncio.to_thread(runtime_status, manager, exchange_state, trainer.list_jobs())
    return router
