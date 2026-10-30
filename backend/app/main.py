from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .adapters import verify_video
from .config import get_settings
from .db import Database
from .integrations import detect_integrations
from .paths import safe_local_path
from .presets import BUILTIN_PRESETS, GenerationSettings, Preset
from .projects import VIDEO_TYPES, scan_project
from .presets import SCORPION_FROG_MOONLIT_RIVERBANK_PROMPT
from .resources import ResourcePreflight, resource_snapshot
from .storyboards import ShotInput, StoryboardUpdate, build_composition_manifest
from .worker import LocalWorker


settings = get_settings()
db = Database(settings)
worker = LocalWorker(db, settings)
resource_preflight = ResourcePreflight(settings)


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


class ImportStoryboardRequest(BaseModel):
    project_id: str = "scorpion-frog"


def _preset_frames(name: str) -> tuple[int, int]:
    preset = next((item for item in BUILTIN_PRESETS if item.name == name), None)
    if preset:
        return preset.frames, preset.fps
    custom = db.get_setting("custom_presets") or []
    row = next((item for item in custom if item["name"] == name), None)
    if not row:
        raise HTTPException(400, f"Unknown generation preset: {name}")
    return row["frames"], row["fps"]


def _generation_payload(config: GenerationSettings) -> dict:
    frames, fps = _preset_frames(config.preset)
    try:
        output = safe_local_path(config.output_location or settings.output_root, settings)
        reference = safe_local_path(config.reference_image, settings, must_exist=True) if config.reference_image else None
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    root = settings.expanded(settings.output_root)
    metrics = resource_snapshot(str(root if root.exists() else settings.allowed_roots[0]))
    warnings = resource_preflight.assess(metrics)
    return config.model_dump() | {"frames": frames, "fps": fps, "output_location": str(output),
                                  "reference_image": str(reference) if reference else None,
                                  "preflight_warnings": warnings}


@app.get("/api/health")
def health():
    return {"status": "ok", "mode": "local", "worker": "single"}


@app.get("/api/integrations")
def integrations():
    return detect_integrations(settings)


@app.get("/api/projects")
def list_projects():
    return db.list_projects()


@app.post("/api/projects/import", status_code=200)
def import_story_project():
    try:
        root = safe_local_path(settings.story_project, settings, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    assets = scan_project(root)
    db.import_project("scorpion-frog", "Scorpion & Frog", str(root), assets)
    project = db.get_project("scorpion-frog")
    return project | {"asset_count": len(assets), "assets": assets}


@app.get("/api/projects/{project_id}/assets")
def project_assets(project_id: str):
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project has not been imported")
    root = safe_local_path(project["root"], settings, must_exist=True)
    return [asset | {"path": str(root / asset["relative_path"]), "name": Path(asset["relative_path"]).name}
            for asset in db.project_assets(project_id)]


def _asset_file(project_id: str, asset_id: str):
    project = db.get_project(project_id)
    asset = db.get_project_asset(project_id, asset_id)
    if not project or not asset:
        raise HTTPException(404, "Project asset not found")
    try:
        root = safe_local_path(project["root"], settings, must_exist=True)
        path = safe_local_path(str(root / asset["relative_path"]), settings, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, "Imported asset is no longer available") from exc
    if root not in path.parents or path.suffix.lower() not in (VIDEO_TYPES if asset["category"].endswith("video") else {".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".webp":"image/webp",".gif":"image/gif"}):
        raise HTTPException(400, "Unsupported project asset")
    return asset, path


@app.get("/api/projects/{project_id}/assets/{asset_id}/file")
def project_asset_file(project_id: str, asset_id: str):
    asset, path = _asset_file(project_id, asset_id)
    return FileResponse(path, media_type=asset["mime_type"], headers={"Content-Disposition": "inline"})


@app.get("/api/projects/{project_id}/assets/{asset_id}/verify")
async def verify_project_asset(project_id: str, asset_id: str):
    asset, path = _asset_file(project_id, asset_id)
    if asset["mime_type"] not in VIDEO_TYPES.values():
        raise HTTPException(400, "Only video assets can be verified")
    try:
        result = await verify_video(str(path), settings)
        return {"verified": True, "format": result.get("format", {}), "streams": result.get("streams", [])}
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        return {"verified": False, "error": str(exc)}


@app.get("/api/storyboards")
def list_storyboards():
    return db.list_storyboards()


@app.post("/api/storyboards/import", status_code=201)
def import_storyboard(request: ImportStoryboardRequest):
    if request.project_id != "scorpion-frog":
        raise HTTPException(400, "Only an already configured local project can be imported here")
    project = db.get_project(request.project_id)
    if project is None:
        try:
            root = safe_local_path(settings.story_project, settings, must_exist=True)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(400, str(exc)) from exc
        assets = scan_project(root)
        db.import_project(request.project_id, "Scorpion & Frog", str(root), assets)
        project = db.get_project(request.project_id)
    existing = next((item for item in db.list_storyboards() if item["project_id"] == request.project_id), None)
    if existing:
        return existing
    riverbank = next((item for item in db.project_assets(request.project_id)
                      if item["relative_path"].endswith("riverbank-master.png")), None)
    project_defaults = db.get_setting(_generation_settings_key(request.project_id)) or {}
    reference = project_defaults.get("reference_image")
    if reference:
        try:
            reference = str(safe_local_path(reference, settings, must_exist=True))
        except (ValueError, FileNotFoundError):
            reference = None
    if not reference and riverbank:
        reference = str(Path(project["root"]) / riverbank["relative_path"])
    preset = project_defaults.get("preset", "Short")
    try:
        _preset_frames(preset)
    except HTTPException:
        preset = "Short"
    shot = {"scene_title": "Moonlit riverbank", "title": "Riverbank opening",
            "prompt": project_defaults.get("prompt") or SCORPION_FROG_MOONLIT_RIVERBANK_PROMPT,
            "reference_image": reference, "preset": preset,
            "seed": project_defaults.get("seed"), "caption": ""}
    return db.create_storyboard(request.project_id, "Scorpion & Frog", [shot])


@app.get("/api/storyboards/{storyboard_id}")
def get_storyboard(storyboard_id: str):
    story = db.get_storyboard(storyboard_id)
    if not story:
        raise HTTPException(404, "Storyboard not found")
    return story


@app.get("/api/storyboards/{storyboard_id}/revisions")
def storyboard_revisions(storyboard_id: str):
    revisions = db.storyboard_revisions(storyboard_id)
    if revisions is None:
        raise HTTPException(404, "Storyboard not found")
    return revisions


@app.get("/api/storyboards/{storyboard_id}/scenes")
def storyboard_scenes(storyboard_id: str):
    story = db.get_storyboard(storyboard_id)
    if not story:
        raise HTTPException(404, "Storyboard not found")
    return [{**scene, "shots": [shot for shot in story["shots"] if shot["scene_id"] == scene["id"]]}
            for scene in story["scenes"]]


@app.put("/api/storyboards/{storyboard_id}")
def update_storyboard(storyboard_id: str, payload: StoryboardUpdate):
    story = db.get_storyboard(storyboard_id)
    if not story:
        raise HTTPException(404, "Storyboard not found")
    values = []
    for shot in payload.shots:
        _preset_frames(shot.preset)
        if shot.reference_image:
            try:
                safe_local_path(shot.reference_image, settings, must_exist=True)
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(400, str(exc)) from exc
        values.append(shot.model_dump())
    try:
        return db.update_storyboard(storyboard_id, payload.revision, payload.title, values)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/storyboards/{storyboard_id}/shots/{shot_id}")
def delete_storyboard_shot(storyboard_id: str, shot_id: str, revision: int = Query(ge=1)):
    try:
        return db.delete_storyboard_shot(storyboard_id, shot_id, revision)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.delete("/api/storyboards/{storyboard_id}", status_code=204)
def archive_storyboard(storyboard_id: str):
    try:
        db.archive_storyboard(storyboard_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/storyboards/{storyboard_id}/shots/{shot_id}/generate", status_code=201)
def queue_storyboard_shot(storyboard_id: str, shot_id: str):
    story = db.get_storyboard(storyboard_id)
    if not story:
        raise HTTPException(404, "Storyboard not found")
    shot = next((item for item in story["shots"] if item["id"] == shot_id), None)
    if not shot:
        raise HTTPException(404, "Shot not found")
    if not detect_integrations(settings)["ltx"]["ready"]:
        raise HTTPException(409, "LTX runner or cached Q8 model weights are unavailable; no weights will be downloaded")
    defaults = db.get_setting(_generation_settings_key(story["project_id"])) or db.get_setting("generation") or GenerationSettings().model_dump()
    config = GenerationSettings.model_validate(defaults | {"prompt": shot["prompt"], "preset": shot["preset"],
                         "seed": shot["seed"], "reference_image": shot["reference_image"]})
    payload = _generation_payload(config)
    try:
        return db.create_shot_job(storyboard_id, shot_id, payload)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/storyboards/{storyboard_id}/manifest")
def create_storyboard_manifest(storyboard_id: str):
    story = db.get_storyboard(storyboard_id)
    if not story:
        raise HTTPException(404, "Storyboard not found")
    try:
        output_root = safe_local_path(settings.output_root, settings)
        output_root.mkdir(parents=True, exist_ok=True)
        manifest = build_composition_manifest(story, output_root)
        directory = safe_local_path(str(output_root / "storyboards" / storyboard_id), settings)
        directory.mkdir(parents=True, exist_ok=True)
        manifest_path = directory / "composition-manifest.json"
        content = __import__("json").dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        temp_path = manifest_path.with_suffix(".json.tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(manifest_path)
        return db.store_storyboard_manifest(storyboard_id, manifest, str(manifest_path))
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/storyboards/{storyboard_id}/render", status_code=201)
def queue_storyboard_render(storyboard_id: str):
    story = db.get_storyboard(storyboard_id)
    if not story:
        raise HTTPException(404, "Storyboard not found")
    manifest = story.get("manifest")
    if not manifest:
        raise HTTPException(409, "Build and preview the storyboard manifest before rendering")
    if manifest.get("revision") != story["revision"]:
        raise HTTPException(409, "Storyboard changed; rebuild its composition preview before rendering")
    try:
        current = build_composition_manifest(story, settings.expanded(settings.output_root))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if current != manifest:
        raise HTTPException(409, "Verified shot outputs changed; rebuild the composition preview before rendering")
    integration = detect_integrations(settings)["remotion"]
    if not integration["ready"]:
        raise HTTPException(409, "The configured local Remotion CLI is unavailable")
    output_root = settings.expanded(settings.output_root)
    output_path = output_root / "storyboards" / storyboard_id / f"storyboard-r{story['revision']}.mp4"
    entrypoint = Path(__file__).resolve().parents[2] / "remotion" / "index.ts"
    payload = {"entrypoint": str(entrypoint), "composition_id": "StoryboardAssembly",
               "output_path": str(output_path), "public_dir": str(output_root),
               "props": {"manifest": manifest}, "width": manifest["width"], "height": manifest["height"],
               "fps": manifest["fps"], "expected_output": {"width": manifest["width"], "height": manifest["height"],
               "fps": manifest["fps"], "frames": manifest["duration_in_frames"]}}
    return db.create_job("render", payload)


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


def _generation_settings_key(project_id: str | None) -> str:
    if project_id is None:
        return "generation"
    if not project_id.replace("-", "").isalnum() or len(project_id) > 80:
        raise HTTPException(400, "Invalid project id")
    return f"generation:{project_id}"


@app.get("/api/settings")
def read_settings(project_id: str | None = None):
    key = _generation_settings_key(project_id)
    specific = db.get_setting(key)
    if specific is not None:
        return specific
    value = db.get_setting("generation") or GenerationSettings().model_dump()
    if project_id == "scorpion-frog":
        value["prompt"] = SCORPION_FROG_MOONLIT_RIVERBANK_PROMPT
    return value


@app.patch("/api/settings")
def update_settings(payload: GenerationSettings, project_id: str | None = None):
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
    db.put_setting(_generation_settings_key(project_id), value)
    return value


@app.post("/api/jobs", status_code=201)
def create_job(request: CreateJob):
    status = detect_integrations(settings)
    if not status["ltx"]["ready"]:
        raise HTTPException(409, "LTX runner or cached Q8 model weights are unavailable. The app will not download model files.")
    payload = _generation_payload(request.settings)
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
    updated = db.cancel_job(job_id)
    if updated is None:
        raise HTTPException(409, "Job state changed before cancellation could be applied")
    worker.cancel_job(job_id)
    db.event(job_id, "warning", "Cancellation requested")
    db.invalidate_storyboard_manifest(job_id)
    return updated


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] != "failed":
        raise HTTPException(409, "Only failed jobs can be retried")
    updated = db.update_job(job_id, status="queued", progress=0, error=None, result_path=None,
                            exit_code=None, elapsed_seconds=None, verification_metadata=None,
                            partial_output_path=None)
    db.event(job_id, "info", "Retry queued")
    db.invalidate_storyboard_manifest(job_id)
    return updated


@app.get("/api/resources")
def resources():
    root = str(settings.expanded(settings.output_root))
    snapshot = resource_snapshot(root if settings.expanded(settings.output_root).exists() else str(settings.allowed_roots[0]))
    snapshot["warnings"] = resource_preflight.assess(snapshot)
    return snapshot


@app.get("/api/benchmarks")
def benchmarks():
    return db.benchmarks()


@app.post("/api/verify-output")
async def verify_output(path: str):
    try:
        result = await verify_video(path, settings)
        return {"verified": True, "format": result.get("format", {}), "streams": result.get("streams", [])}
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/media/file")
def media_file(path: str):
    try:
        video = safe_local_path(path, settings, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    media_type = VIDEO_TYPES.get(video.suffix.lower())
    if media_type is None or not video.is_file():
        raise HTTPException(400, "Only local video files can be previewed")
    return FileResponse(video, media_type=media_type, headers={"Content-Disposition": "inline"})
