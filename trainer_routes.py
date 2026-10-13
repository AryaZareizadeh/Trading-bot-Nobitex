"""Private model registry and training endpoints, mounted per account."""
import asyncio
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from research_scheduler import PREDICTION, ResearchBusy


def trainer_router(manager, request_model):
    router = APIRouter(prefix="/api/model-trainer")

    @router.get("/config")
    async def model_trainer_config():
        return await asyncio.to_thread(manager.get_config)

    @router.get("/jobs")
    async def model_trainer_jobs():
        return {"jobs": manager.list_jobs()}

    @router.post("/run", status_code=202)
    async def model_trainer_run(req: request_model):
        try:
            return {"job_id": manager.start_job(req.model_dump(exclude_unset=True))}
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

    @router.get("/run/{job_id}")
    async def model_trainer_run_status(job_id: str):
        job = manager.get_job(job_id)
        return job if job is not None else JSONResponse(status_code=404, content={"error": "Training run not found"})

    @router.post("/run/{job_id}/cancel")
    async def model_trainer_run_cancel(job_id: str):
        job = manager.cancel(job_id)
        return job if job is not None else JSONResponse(status_code=404, content={"error": "Training run not found"})

    @router.get("/models")
    async def trained_models():
        return {"models": manager.list_models()}

    @router.post("/models/{model_id}/predict")
    async def trained_model_predict(model_id: str):
        if manager.get_model(model_id) is None:
            return JSONResponse(status_code=404, content={"error": "Saved model not found"})
        try:
            return await PREDICTION.run(str(manager.store.path.resolve()), manager.predict, model_id)
        except ResearchBusy as exc:
            return JSONResponse(status_code=429, content={"error": str(exc)})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        except Exception:
            return JSONResponse(status_code=503, content={"error": "Prediction unavailable"})
    return router
