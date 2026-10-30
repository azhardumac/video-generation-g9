from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .adapters import verify_video
from .config import get_settings
from .db import Database
from .paths import safe_local_path
from .presets import BUILTIN_PRESETS, GenerationSettings, Preset
from .resources import resource_snapshot
from .worker import LocalWorker


settings = get_settings()
db = Database(settings)
worker = LocalWorker(db, settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await worker.start()
    yield
    await worker.stop()


app = FastAPI(title="AI Video Studio Local API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
                   allow_credentials=False, allow_methods=["GET", "POST", "PATCH"], allow_headers=["Content-Type"])


class CreateJob(BaseModel):
    settings: GenerationSettings
    kind: Literal["generation"] = "generation"


class RenderRequest(BaseModel):
    entrypoint: str
    composition_id: str = Field(min_length=1, max_length=160)
    output_path: str
    props: dict = Field(default_factory=dict)


@app.get("/api/health")
def health():
    return {"status": "ok", "mode": "local", "worker": "single"}


@app.get("/api/presets")
def presets():
    custom = db.get_setting("custom_presets") or []
    return [p.model_dump() | {"duration": p.duration} for p in BUILTIN_PRESETS] + custom


@app.post("/api/presets", status_code=201)
def add_preset(preset: Preset):
    custom = db.get_setting("custom_presets") or []
    if any(p["name"].casefold() == preset.name.casefold() for p in custom) or any(p.name.casefold() == preset.name.casefold() for p in BUILTIN_PRESETS):
        raise HTTPException(409, "Preset name already exists")
    custom.append(preset.model_dump() | {"duration": preset.duration})
    db.put_setting("custom_presets", custom)
    return custom[-1]


@app.get("/api/settings")
def read_settings():
    return db.get_setting("generation") or GenerationSettings().model_dump()


@app.patch("/api/settings")
def update_settings(payload: GenerationSettings):
    value = payload.model_dump()
    if value.get("reference_image"):
        try:
            safe_local_path(value["reference_image"], settings, must_exist=True)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(400, str(exc)) from exc
    if value.get("output_location"):
        try:
            safe_local_path(value["output_location"], settings)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    db.put_setting("generation", value)
    return value


@app.post("/api/jobs", status_code=201)
def create_job(request: CreateJob):
    config = request.settings
    preset = next((p for p in BUILTIN_PRESETS if p.name == config.preset), None)
    if preset is None:
        custom = db.get_setting("custom_presets") or []
        preset_row = next((p for p in custom if p["name"] == config.preset), None)
        if not preset_row:
            raise HTTPException(400, "Select a known generation preset")
        frames, fps = preset_row["frames"], preset_row["fps"]
    else:
        frames, fps = preset.frames, preset.fps
    try:
        output = safe_local_path(config.output_location or settings.output_root, settings)
        reference = safe_local_path(config.reference_image, settings, must_exist=True) if config.reference_image else None
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    payload = config.model_dump() | {"frames": frames, "fps": fps, "output_location": str(output), "reference_image": str(reference) if reference else None}
    return db.create_job(request.kind, payload)


@app.post("/api/renders", status_code=201)
def create_render(request: RenderRequest):
    try:
        entrypoint = safe_local_path(request.entrypoint, settings, must_exist=True)
        output = safe_local_path(request.output_path, settings)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return db.create_job("render", request.model_dump() | {"entrypoint": str(entrypoint), "output_path": str(output)})


@app.get("/api/jobs")
def list_jobs():
    return db.list_jobs()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job | {"events": db.events(job_id)}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] not in ("queued", "running"):
        raise HTTPException(409, "Only queued or running jobs can be cancelled")
    updated = db.update_job(job_id, status="cancelled")
    worker.cancel_job(job_id)
    db.event(job_id, "warning", "Cancellation requested")
    return updated


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] != "failed":
        raise HTTPException(409, "Only failed jobs can be retried")
    updated = db.update_job(job_id, status="queued", progress=0, error=None, result_path=None)
    db.event(job_id, "info", "Retry queued")
    return updated


@app.get("/api/resources")
def resources():
    root = str(settings.expanded(settings.output_root))
    return resource_snapshot(root if settings.expanded(settings.output_root).exists() else str(settings.allowed_roots[0]))


@app.get("/api/benchmarks")
def benchmarks():
    return db.benchmarks()


@app.post("/api/verify-output")
async def verify_output(path: str):
    try:
        return await verify_video(path, settings)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
