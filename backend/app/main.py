import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from .adapters import verify_video
from .config import get_settings
from .db import Database, MAX_GEMINI_ATTEMPTS_PER_REVISION
from .integrations import detect_integrations
from .film_planner import UnsupportedStory, make_approved_fable_plan, validate_approved_fable
from .film_orchestrator import FilmOrchestrator, FilmRunConflict, FilmRunTransitionError, STAGES
from .story_planning import LocalStoryPlanner
from .gemini_planning import (GeminiPlanningError, GeminiStoryPlanner, STORY_PLAN_SCHEMA_VERSION,
                              _stored_model_is_text_eligible)
from .gemini_secrets import GeminiSecretStore, SecretStoreError
from .paths import safe_local_path
from .presets import BUILTIN_PRESETS, GenerationSettings, Preset
from .projects import IMAGE_TYPES, VIDEO_TYPES, scan_project
from .resources import ResourcePreflight, resource_snapshot
from .storyboards import ShotInput, StoryboardUpdate, build_composition_manifest
from .setup_service import inspect_setup, setup_manager
from .worker import LocalWorker


settings = get_settings()
db = Database(settings)
worker = LocalWorker(db, settings)
gemini_secret_store = worker.film_runs.secret_store
film_orchestrator = FilmOrchestrator(db)
resource_preflight = ResourcePreflight(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await worker.start()
    yield
    await worker.stop()


app = FastAPI(title="AI Video Studio Local API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
                   allow_credentials=False, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                   allow_headers=["Content-Type", "X-File-Name"])


class CreateJob(BaseModel):
    settings: GenerationSettings
    project_id: str | None = None
    kind: Literal["generation"] = "generation"


class RenderRequest(BaseModel):
    entrypoint: str
    composition_id: str = Field(min_length=1, max_length=160)
    output_path: str
    props: dict = Field(default_factory=dict)
    project_id: str | None = None


AspectRatio = Literal["9:16", "16:9", "1:1", "4:3", "3:2"]
ReferenceCategory = Literal["character", "scene", "style", "other"]


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    workspace_path: str = Field(min_length=1, max_length=2000)
    description: str = Field(default="", max_length=2000)
    aspect_ratio: AspectRatio
    frame_rate: int = Field(ge=1, le=120)
    production_type: str = Field(min_length=1, max_length=120)


class ProjectImportRequest(BaseModel):
    workspace_path: str = Field(min_length=1, max_length=2000)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    aspect_ratio: AspectRatio | None = None
    frame_rate: int | None = Field(default=None, ge=1, le=120)
    production_type: str | None = Field(default=None, min_length=1, max_length=120)


class ActiveProjectRequest(BaseModel):
    project_id: str | None


class ProjectReferenceCreate(BaseModel):
    path: str = Field(min_length=1, max_length=4000)
    name: str = Field(min_length=1, max_length=160)
    category: ReferenceCategory
    notes: str = Field(default="", max_length=2000)


class ProjectReferenceUpdate(BaseModel):
    path: str | None = Field(default=None, max_length=4000)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    category: ReferenceCategory | None = None
    notes: str | None = Field(default=None, max_length=2000)


class ContinuityScene(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    stage_layout: str = Field(default="", max_length=2000)
    framing: str = Field(default="", max_length=500)
    continuity_notes: str = Field(default="", max_length=3000)
    expected_character_count: int = Field(default=2, ge=0, le=20)
    character_identities: list[str] = Field(default_factory=list, max_length=20)
    river_width_scale: str = Field(default="", max_length=500)
    camera_orientation: str = Field(default="", max_length=500)
    anchor_reference_ids: list[str] = Field(default_factory=list, max_length=8)
    anchor_frame_indices: list[int] = Field(default_factory=list, max_length=8)


class SceneContinuityDocument(BaseModel):
    frog_reference_id: str = Field(min_length=1, max_length=160)
    scorpion_reference_id: str = Field(min_length=1, max_length=160)
    environment_reference_id: str | None = Field(default=None, max_length=160)
    historical_reference_approval_id: str | None = Field(default=None, max_length=160)
    scenes: list[ContinuityScene] = Field(default_factory=list, max_length=30)


class SceneContinuitySaveRequest(BaseModel):
    document: SceneContinuityDocument


class SceneContinuityApproveRequest(BaseModel):
    revision: int = Field(ge=1)


class CreativeReviewRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    continuity_revision: int = Field(ge=1)
    checklist: dict[str, bool]
    reason: str = Field(default="", max_length=2000)


class ShotGenerateRequest(BaseModel):
    continuity_revision: int | None = Field(default=None, ge=1)
    use_experimental_multi_anchor: bool = False


class CreateStoryboardRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    story_text: str = Field(default="", max_length=20000)


class BuildFilmPlanRequest(BaseModel):
    story_text: str = Field(min_length=80, max_length=20000)
    title: str = Field(default="The Frog and the Scorpion — River Crossing", min_length=1, max_length=160)
    frog_reference_id: str = Field(min_length=1, max_length=160)
    scorpion_reference_id: str = Field(min_length=1, max_length=160)
    target_format: AspectRatio = "9:16"
    preset: str = "Short"
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)


class FilmRunBudget(BaseModel):
    max_shots: int = Field(default=1, ge=1, le=40)
    max_retries_per_shot: int = Field(default=1, ge=0, le=3)
    max_generation_seconds: int = Field(default=3600, ge=60, le=86400)
    max_output_bytes: int = Field(default=8 * 1024**3, ge=256 * 1024**2, le=50 * 1024**3)


class CreateFilmRunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    reference_ids: list[str] = Field(default_factory=list, max_length=16)
    preset: str = "Short"
    budget: FilmRunBudget = Field(default_factory=FilmRunBudget)
    idempotency_key: str = Field(min_length=1, max_length=128)
    initial_confirmation: bool = False
    planner_provider: Literal["gemini", "local"] = "local"
    planner_model: str | None = Field(default=None, min_length=1, max_length=120)
    planner_response_mode: Literal["schema", "json_only"] = "schema"
    plan_only: bool = False
    remote_processing_consent: bool = False

    @model_validator(mode="after")
    def normalized_prompt(self):
        self.prompt = self.prompt.strip()
        if not self.prompt:
            raise ValueError("Story prompt cannot be blank")
        if len(set(self.reference_ids)) != len(self.reference_ids):
            raise ValueError("Reference selections must be unique")
        if self.planner_provider == "local" and self.remote_processing_consent:
            raise ValueError("Remote-processing consent applies only to Gemini planning")
        return self


class FilmRunRevisionRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class FilmRunAuthorizationRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    confirmed: bool = False
    budget_acknowledged: bool = False


class GeminiModelRequest(BaseModel):
    model: str = Field(min_length=1, max_length=120)


class GeminiGenerationEndpointProbeRequest(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    confirmed: bool = False


class GeminiReplanRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    planner_model: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=1, max_length=128)
    response_mode: Literal["schema", "json_only"] = "schema"
    confirm_billable_planning: bool = False


class ApproveFilmPlanRequest(BaseModel):
    revision: int = Field(ge=1)
    style_bible_revision: int = Field(ge=0)


class GenerateFilmRequest(ApproveFilmPlanRequest):
    pass


class RetryFilmRequest(BaseModel):
    shot_ids: list[str] = Field(min_length=1, max_length=40)


class ReconcileVerifiedShotsRequest(ApproveFilmPlanRequest):
    source_batch_id: str = Field(min_length=1, max_length=80)


class StyleBibleRequest(BaseModel):
    material: str = Field(default="", max_length=2000)
    silhouettes: str = Field(default="", max_length=2000)
    proportions: str = Field(default="", max_length=2000)
    composition: str = Field(default="", max_length=2000)
    negative_constraints: str = Field(default="", max_length=2000)


class ImportStoryboardRequest(BaseModel):
    project_id: str = "scorpion-frog"


class RevealOutputRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4000)


def _preset_frames(name: str) -> tuple[int, int]:
    preset = next((item for item in BUILTIN_PRESETS if item.name == name), None)
    if preset:
        return preset.frames, preset.fps
    custom = db.get_setting("custom_presets") or []
    row = next((item for item in custom if item["name"] == name), None)
    if not row:
        raise HTTPException(400, f"Unknown generation preset: {name}")
    return row["frames"], row["fps"]


def _generation_payload(config: GenerationSettings, project_id: str | None = None, fps_override: int | None = None) -> dict:
    frames, preset_fps = _preset_frames(config.preset)
    fps = fps_override or preset_fps
    project = _project(project_id) if project_id else None
    default_output = str(_project_output(project)) if project else settings.output_root
    try:
        output = safe_local_path(config.output_location or default_output, settings)
        reference = _validate_project_reference(project, config.reference_image) if project else (
            safe_local_path(config.reference_image, settings, must_exist=True) if config.reference_image else None)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    root = settings.expanded(settings.output_root)
    metrics = resource_snapshot(str(root if root.exists() else settings.allowed_roots[0]))
    warnings = resource_preflight.assess(metrics)
    return config.model_dump() | {"frames": frames, "fps": fps, "output_location": str(output),
                                  "reference_image": str(reference) if reference else None,
                                  "preflight_warnings": warnings}


def _project(project_id: str, *, allow_archived: bool = False) -> dict:
    project = db.get_project(project_id)
    if not project or (project.get("archived_at") and not allow_archived):
        raise HTTPException(404, "Project not found or archived")
    return project


def _project_id_for_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:40] or "project"
    return f"{slug}-{uuid4().hex}"


def _project_output(project: dict) -> Path:
    return settings.expanded(settings.output_root) / project["id"]


def _available_bytes_for_path(path: Path) -> int:
    candidate = path.expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return shutil.disk_usage(candidate).free


def _batch_with_resource_warnings(batch: dict) -> dict:
    root = settings.expanded(settings.output_root)
    try:
        metrics = resource_snapshot(str(root if root.exists() else settings.allowed_roots[0]))
        batch["resource_warnings"] = resource_preflight.assess(metrics)
    except Exception as exc:
        batch["resource_warnings"] = [f"Resource safeguards could not be read: {exc}"]
    return batch


def _validate_project_reference(project: dict, raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    try:
        path = safe_local_path(raw_path, settings, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    allowed = {Path(item["path"]).resolve() for item in db.project_references(project["id"])}
    root = settings.expanded(project["root"])
    for asset in db.project_assets(project["id"]):
        allowed.add((root / asset["relative_path"]).resolve())
    if path not in allowed:
        raise HTTPException(400, "Reference image must be linked to the selected project first")
    return path


def _style_context(style_bible: dict) -> str:
    labels = (("material", "Material and finish"), ("silhouettes", "Silhouettes"),
              ("proportions", "Character proportions"), ("composition", "Composition guidance"),
              ("negative_constraints", "Negative constraints"))
    return "\n".join(f"{label}: {style_bible[key].strip()}" for key, label in labels
                     if style_bible.get(key, "").strip())


def _default_dimensions(aspect_ratio: str | None) -> tuple[int, int]:
    return {"16:9": (768, 432), "1:1": (640, 640), "4:3": (640, 480), "3:2": (672, 448)}.get(aspect_ratio or "9:16", (448, 768))


def _reference_by_id(project_id: str, reference_id: str, character: str) -> dict:
    reference = next((item for item in db.project_references(project_id) if item["id"] == reference_id), None)
    if not reference:
        raise HTTPException(400, f"Select a linked {character} reference from this project")
    if reference["category"] != "character":
        raise HTTPException(400, f"The selected {character} reference must have the character category")
    path = _validate_project_reference(_project(project_id), reference["path"])
    return reference | {"path": str(path)}


def _reference_details(project_id: str, reference_id: str, expected_category: str | None = None) -> dict:
    item = db.get_project_reference(project_id, reference_id)
    if not item:
        raise HTTPException(400, "Reference must be linked to this project")
    if expected_category and item["category"] != expected_category:
        raise HTTPException(400, f"Select a {expected_category} reference")
    if not str(item.get("mime_type", "")).startswith("image/"):
        raise HTTPException(400, "Scene continuity references must be local image files; video clips cannot be used as image anchors")
    path = _validate_project_reference(_project(project_id), item["path"])
    if not path or not path.is_file():
        raise HTTPException(400, f"Reference file is unavailable: {item['name']}")
    return item | {"path": str(path)}


def _is_historical_visual_reference(item: dict) -> bool:
    path = Path(item["path"]).expanduser().resolve()
    try:
        output_root = settings.expanded(settings.output_root).resolve()
        if path == output_root or output_root in path.parents:
            return True
    except OSError:
        pass
    name = f"{item.get('name', '')} {path.name}".casefold()
    return any(token in name for token in ("riverbank-master", "benchmark", "storyboard", "generated", ".mp4", ".mov"))


def _validated_continuity_document(project_id: str, document: SceneContinuityDocument) -> dict:
    if document.frog_reference_id == document.scorpion_reference_id:
        raise HTTPException(400, "Frog and scorpion must use two distinct character references")
    frog = _reference_details(project_id, document.frog_reference_id, "character")
    scorpion = _reference_details(project_id, document.scorpion_reference_id, "character")
    if _is_historical_visual_reference(frog) or _is_historical_visual_reference(scorpion):
        raise HTTPException(400, "Original character anchors must be linked character references, not old outputs or historical riverbank media")
    env = None
    if document.environment_reference_id:
        env = _reference_details(project_id, document.environment_reference_id, "scene")
        if _is_historical_visual_reference(env) and document.historical_reference_approval_id != env["id"]:
            raise HTTPException(400, "This environment image looks historical or generated. Explicitly approve this exact reference before using it")
    scene_titles: set[str] = set()
    references = {frog["id"]: frog, scorpion["id"]: scorpion}
    if env:
        references[env["id"]] = env
    scenes = []
    for scene in document.scenes:
        normalized = scene.model_dump()
        title = normalized["title"].strip()
        if not title or title.casefold() in scene_titles:
            raise HTTPException(422, "Scene titles must be non-empty and unique")
        scene_titles.add(title.casefold())
        if len(set(normalized["anchor_reference_ids"])) != len(normalized["anchor_reference_ids"]):
            raise HTTPException(422, "Scene anchor references must not be repeated")
        if len(normalized["anchor_frame_indices"]) != len(normalized["anchor_reference_ids"]):
            raise HTTPException(422, "Each reference anchor needs exactly one frame index")
        if len(set(normalized["anchor_frame_indices"])) != len(normalized["anchor_frame_indices"]):
            raise HTTPException(422, "Each temporal image anchor must use a distinct frame index")
        if any(index < 0 or index > 192 for index in normalized["anchor_frame_indices"]):
            raise HTTPException(422, "Reference anchor frame indices must be between 0 and 192")
        for ref_id in normalized["anchor_reference_ids"]:
            if ref_id not in references:
                raise HTTPException(400, "A scene anchor must be one of the approved character references or environment reference")
        normalized["title"] = title
        scenes.append(normalized)
    return {"frog_reference_id": frog["id"], "scorpion_reference_id": scorpion["id"],
            "environment_reference_id": env["id"] if env else None,
            "historical_reference_approval_id": (env["id"] if env and _is_historical_visual_reference(env) and
                document.historical_reference_approval_id == env["id"] else None), "scenes": scenes}


@app.get("/api/projects/{project_id}/scene-continuity")
def get_scene_continuity(project_id: str):
    _project(project_id)
    state = db.get_scene_continuity(project_id)
    ltx = detect_integrations(settings)["ltx"]
    runner_available = bool(ltx.get("runner_available", ltx.get("ready", False)))
    cache = settings.expanded(settings.hf_cache)
    hub_cache = cache / "hub" if (cache / "hub").is_dir() else cache
    try:
        ic_lora_weights = any(path.is_dir() and "IC-LoRA" in path.name for path in hub_cache.glob("models--*"))
    except OSError:
        ic_lora_weights = False
    references = {item["id"]: item for item in db.project_references(project_id)}
    for field in ("frog_reference_id", "scorpion_reference_id", "environment_reference_id"):
        reference_id = (state.get("document") or {}).get(field)
        if reference_id:
            try:
                path = safe_local_path(references[reference_id]["path"], settings, must_exist=True) if references.get(reference_id) else None
                state.setdefault("reference_availability", {})[field] = bool(path and path.is_file())
            except (ValueError, FileNotFoundError):
                state.setdefault("reference_availability", {})[field] = False
    state["capabilities"] = {"independent_i2v": {"available": runner_available and bool(ltx.get("weights_cached", ltx.get("ready", False))), "status": "supported" if ltx.get("ready", False) else "not_ready", "description": "Existing single-image image-to-video generation; uses the configured local runner and offline Q8 cache."},
        "multi_image_anchors": {"available": runner_available, "enabled": runner_available, "status": "experimental" if runner_available else "runner_missing", "description": "Installed CLI accepts repeated temporal --image anchors. These guide frames over time; they do not lock character identity or placement."},
        "video_extension": {"available": runner_available, "enabled": False, "status": "experimental_blocked" if runner_available else "runner_missing", "description": "The installed CLI exposes extend, but this mode is experimental for this cached build, audio behavior is not safely compatible, and 16 GB source-encode memory has not been validated."},
        "ic_lora": {"available": runner_available, "enabled": False, "status": "experimental_blocked" if ic_lora_weights else "weights_missing", "description": "The installed CLI subcommand exists, but compatible IC-LoRA/control weights are not in the existing local cache."}}
    return state


@app.put("/api/projects/{project_id}/scene-continuity")
def save_scene_continuity(project_id: str, request: SceneContinuitySaveRequest):
    _project(project_id)
    document = _validated_continuity_document(project_id, request.document)
    return db.save_scene_continuity(project_id, document)


@app.post("/api/projects/{project_id}/scene-continuity/approve")
def approve_scene_continuity(project_id: str, request: SceneContinuityApproveRequest):
    _project(project_id)
    current = db.get_scene_continuity(project_id)
    if current["revision"] != request.revision or not current.get("document"):
        raise HTTPException(409, "Save the current continuity package before approving it")
    # Revalidate links and local file availability immediately before recording approval.
    _validated_continuity_document(project_id, SceneContinuityDocument.model_validate(current["document"]))
    try:
        return db.approve_scene_continuity(project_id, request.revision)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/storyboards/{storyboard_id}/shots/{shot_id}/creative-review")
def review_shot_creatively(storyboard_id: str, shot_id: str, request: CreativeReviewRequest):
    story = db.get_storyboard(storyboard_id, include_events=False)
    if not story:
        raise HTTPException(404, "Storyboard not found")
    shot = next((item for item in story["shots"] if item["id"] == shot_id), None)
    if not shot:
        raise HTTPException(404, "Shot not found")
    job = shot.get("job")
    try:
        current_output = safe_local_path(job["result_path"], settings, must_exist=True) if job and job.get("result_path") else None
    except (ValueError, FileNotFoundError):
        current_output = None
    verified = bool(job and job["status"] == "completed" and job.get("exit_code") == 0 and
        (job.get("verification_metadata") or {}).get("verified_output") and current_output and current_output.is_file() and shot.get("settings_match"))
    if not verified:
        raise HTTPException(409, "Creative review requires the current shot output to remain technically verified")
    continuity = db.get_scene_continuity(story["project_id"])
    if continuity.get("approved_revision") != request.continuity_revision or continuity.get("revision") != request.continuity_revision:
        raise HTTPException(409, "Approve the current project continuity package before reviewing this clip")
    required_checks = {"identities", "count", "environment", "framing", "artifacts"}
    if request.decision == "approved" and (set(request.checklist) != required_checks or not all(request.checklist.values())):
        raise HTTPException(422, "Approve only after confirming every creative review checklist item")
    if request.decision == "rejected" and not request.reason.strip():
        raise HTTPException(422, "A clear rejection reason is required")
    return db.add_creative_review(project_id=story["project_id"], storyboard_id=storyboard_id, shot_id=shot_id,
        job_id=job["id"], storyboard_revision=story["revision"], continuity_revision=request.continuity_revision,
        decision=request.decision, checklist=request.checklist, reason=request.reason.strip())


def _validate_image_path(raw: str) -> tuple[Path, str, int, str]:
    try:
        path = safe_local_path(raw, settings, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.is_file() or path.suffix.lower() not in IMAGE_TYPES:
        raise HTTPException(400, "Choose a readable PNG, JPEG, WebP, or GIF image")
    try:
        size = path.stat().st_size
        if size <= 0 or size > settings.project_upload_max_bytes:
            raise HTTPException(400, f"Reference image must be between 1 byte and {settings.project_upload_max_bytes} bytes")
        data = path.read_bytes()
    except OSError as exc:
        raise HTTPException(400, f"Reference image is not readable: {exc}") from exc
    mime, digest = _validate_image_bytes(path.name, data)
    return path, mime, size, digest


def _validate_image_bytes(filename: str, data: bytes) -> tuple[str, str]:
    extension = Path(filename).suffix.lower()
    mime = IMAGE_TYPES.get(extension)
    if not mime or not data or len(data) > settings.project_upload_max_bytes:
        raise HTTPException(400, "Reference image has an unsupported file type or size")
    signatures = {
        ".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": data.startswith(b"\xff\xd8\xff"),
        ".jpeg": data.startswith(b"\xff\xd8\xff"),
        ".webp": len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
        ".gif": data.startswith((b"GIF87a", b"GIF89a")),
    }
    if not signatures.get(extension, False):
        raise HTTPException(400, "Image extension does not match readable image content")
    return mime, sha256(data).hexdigest()


def _reference_storage_root(project: dict, category: str) -> Path:
    if project.get("workspace_kind") == "created":
        base = settings.expanded(project["root"]) / "references"
    else:
        base = settings.expanded(settings.database_path).parent / "project-uploads" / project["id"]
    try:
        resolved = safe_local_path(str(base / category), settings)
        if project.get("workspace_kind") == "created":
            project_root = settings.expanded(project["root"])
            if resolved != project_root and project_root not in resolved.parents:
                raise ValueError("Project reference storage resolved outside its workspace")
        return resolved
    except ValueError as exc:
        raise HTTPException(400, f"Configured project upload storage is outside ALLOWED_PATH_ROOTS: {exc}") from exc


@app.get("/api/health")
def health():
    return {"status": "ok", "mode": "local", "worker": "single"}


class SetupInstallRequest(BaseModel):
    download_model: bool = False
    confirm_model_download: bool = False


def _require_setup_loopback(request: Request) -> None:
    host = request.client.host if request.client else ""
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise HTTPException(403, "Setup installation is available only from a loopback client")


@app.get("/api/setup/status")
def setup_status(request: Request):
    _require_setup_loopback(request)
    result = inspect_setup(settings)
    running = setup_manager.snapshot()
    result.update(running)
    stage = str(running.get("installing") or "").casefold()
    stage_map = (("homebrew", "homebrew"), ("git", "git"), ("gh", "gh"), ("ffmpeg", "ffmpeg"),
                 ("ffprobe", "ffprobe"), ("python@3.11", "python_homebrew"), ("python", "python"), ("node", "node"), ("npm", "npm"),
                 ("uv", "uv"), ("ltx_source", "ltx_source"), ("ltx_dependencies", "ltx_runner"),
                 ("ltx_runner", "ltx_runner"), ("backend", "backend"), ("frontend", "frontend"),
                 ("remotion", "remotion"), ("model", "model"), ("env", "env"), ("projects", "projects"),
                 ("outputs", "outputs"))
    # Generic stage words follow the more specific phase names to avoid
    # flagging the wrong component (e.g. ltx_source contains 'ltx').
    component_id = next((target for token, target in stage_map if token in stage), None)
    if component_id:
        for item in result["components"]:
            if item["id"] == component_id:
                item["status"] = "downloading"
                if component_id == "model" and result.get("download_progress_percent") is not None:
                    item["detail"] = f"Downloading {result['download_progress_percent']:.0f}% · resumable cache files are retained."
                else:
                    item["detail"] = running.get("installing") or item["detail"]
                break
    result["ready"] = all(item["status"] == "installed" for item in result["components"])
    result["minimum_non_model_free_bytes"] = 12 * 1024**3
    result["disk_sufficient_for_bootstrap"] = result["disk_free_bytes"] >= result["minimum_non_model_free_bytes"]
    return result


@app.post("/api/setup/install", status_code=202)
def install_setup(request: Request, payload: SetupInstallRequest):
    _require_setup_loopback(request)
    if payload.download_model and not payload.confirm_model_download:
        raise HTTPException(422, "Model download requires explicit confirmation after reviewing the large-download notice")
    snapshot = inspect_setup(settings)
    if snapshot["disk_free_bytes"] < 12 * 1024**3:
        raise HTTPException(409, "At least 12 GiB free disk space is recommended for installing the app dependencies")
    app_root = Path(__file__).resolve().parents[2]
    try:
        state = setup_manager.start(app_root, settings, payload.download_model, payload.confirm_model_download)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, FileNotFoundError) as exc:
        raise HTTPException(500, f"Could not start local bootstrap: {exc}") from exc
    return {"install_state": state["install_state"], "message": "Setup started. Missing components are installed in order; existing local data and .env are preserved."}


@app.post("/api/setup/cancel")
def cancel_setup(request: Request):
    _require_setup_loopback(request)
    try:
        return setup_manager.cancel()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/integrations")
def integrations():
    return detect_integrations(settings)


@app.get("/api/projects")
def list_projects(include_archived: bool = False):
    return db.list_projects(include_archived=include_archived)


@app.get("/api/projects/active")
def active_project():
    project_id = db.get_setting("active_project_id")
    project = db.get_project(project_id) if project_id else None
    if project and project.get("archived_at"):
        project = None
    return {"project": project}


@app.put("/api/projects/active")
def set_active_project(request: ActiveProjectRequest):
    if request.project_id is not None:
        _project(request.project_id)
    db.put_setting("active_project_id", request.project_id)
    return active_project()


@app.post("/api/projects", status_code=201)
def create_project(request: ProjectCreateRequest):
    try:
        root = safe_local_path(request.workspace_path, settings)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not request.name.strip() or not request.production_type.strip():
        raise HTTPException(422, "Project name and production type cannot be blank")
    if root.exists():
        raise HTTPException(409, "Workspace path already exists. Choose a new directory; existing files will not be overwritten.")
    if not root.parent.is_dir():
        raise HTTPException(400, "Workspace parent directory must already exist")
    if any(project["root"] == str(root) for project in db.list_projects(include_archived=True)):
        raise HTTPException(409, "A project is already registered at this workspace path")
    project_id = _project_id_for_name(request.name)
    stage = Path(tempfile.mkdtemp(prefix=f".{root.name}.creating-", dir=root.parent))
    try:
        for directory in ("references/character", "references/scene", "references/style", "references/other", "storyboards", "exports"):
            (stage / directory).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        shutil.rmtree(stage, ignore_errors=True)
        raise HTTPException(500, f"Could not prepare the new project workspace: {exc}") from exc
    project = {"id": project_id, "name": request.name.strip(), "root": str(root),
               "description": request.description, "aspect_ratio": request.aspect_ratio,
               "frame_rate": request.frame_rate, "production_type": request.production_type.strip()}
    created_root = False
    try:
        if root.exists():
            raise FileExistsError(f"Workspace path appeared during creation: {root}")
        os.rename(stage, root)
        created_root = True
        db.create_project(project)
    except (OSError, sqlite3.Error) as exc:
        # The destination can only be removed here if this request created it.
        if created_root and root.exists() and db.get_project(project_id) is None:
            shutil.rmtree(root, ignore_errors=True)
        if stage.exists():
            shutil.rmtree(stage)
        raise HTTPException(409, f"Could not create the project workspace safely: {exc}") from exc
    return db.get_project(project_id)


@app.post("/api/projects/import", status_code=201)
def import_existing_project(request: ProjectImportRequest | None = None):
    raw_root = request.workspace_path if request else settings.story_project
    try:
        root = safe_local_path(raw_root, settings, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if not root.is_dir():
        raise HTTPException(400, "Choose an existing directory to import")
    existing = next((p for p in db.list_projects(include_archived=True) if Path(p["root"]).resolve() == root), None)
    if existing:
        if existing.get("archived_at"):
            raise HTTPException(409, "This workspace belongs to an archived project; restore it from Projects first")
        return existing | {"asset_count": len(db.project_assets(existing["id"])), "assets": db.project_assets(existing["id"])}
    assets = scan_project(root)
    name = (request.name if request and request.name else root.name).strip()
    project_id = _project_id_for_name(name)
    project = db.import_project(project_id, name, str(root), assets,
                                description=request.description if request else "")
    return project | {"asset_count": len(assets), "assets": db.project_assets(project_id)}


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, request: ProjectUpdateRequest):
    _project(project_id)
    values = request.model_dump(exclude_unset=True)
    if "name" in values and values["name"] is not None:
        values["name"] = values["name"].strip()
    if "production_type" in values and values["production_type"] is not None:
        values["production_type"] = values["production_type"].strip()
    try:
        return db.update_project(project_id, **values)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/projects/{project_id}/archive", status_code=204)
def archive_project(project_id: str):
    try:
        db.archive_project(project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/projects/{project_id}/restore")
def restore_project(project_id: str):
    try:
        return db.restore_project(project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/projects/{project_id}/style-bible")
def get_project_style_bible(project_id: str):
    project = _project(project_id)
    return {"revision": project["style_bible_revision"], "fields": project["style_bible"]}


@app.put("/api/projects/{project_id}/style-bible")
def update_project_style_bible(project_id: str, request: StyleBibleRequest):
    _project(project_id)
    values = {key: value.strip() for key, value in request.model_dump().items()}
    try:
        project = db.update_project_style_bible(project_id, values)
        return {"revision": project["style_bible_revision"], "fields": project["style_bible"]}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/projects/{project_id}/assets")
def project_assets(project_id: str):
    project = _project(project_id)
    root = safe_local_path(project["root"], settings, must_exist=True)
    return [asset | {"path": str(root / asset["relative_path"]), "name": Path(asset["relative_path"]).name}
            for asset in db.project_assets(project_id)]


@app.get("/api/projects/{project_id}/references")
def project_references(project_id: str):
    _project(project_id)
    references = []
    for item in db.project_references(project_id):
        try:
            path = safe_local_path(item["path"], settings, must_exist=True)
            item["available"] = path.is_file()
        except (ValueError, FileNotFoundError):
            item["available"] = False
        item["file_url"] = f"/api/projects/{project_id}/references/{item['id']}/file"
        references.append(item)
    return references


@app.post("/api/projects/{project_id}/references", status_code=201)
def add_existing_project_reference(project_id: str, request: ProjectReferenceCreate):
    _project(project_id)
    path, mime, size, digest = _validate_image_path(request.path)
    try:
        return db.add_project_reference({"id": str(uuid4()), "project_id": project_id, "name": request.name.strip(),
            "category": request.category, "path": str(path), "source_kind": "linked", "notes": request.notes,
            "mime_type": mime, "size_bytes": size, "content_hash": digest})
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "This image is already linked to this project") from exc


@app.put("/api/projects/{project_id}/references/upload", status_code=201)
async def upload_project_reference(project_id: str, request: Request,
                                   name: str = Query(min_length=1, max_length=160),
                                   category: ReferenceCategory = Query(), notes: str = Query(default="", max_length=2000),
                                   replace_id: str | None = Query(default=None)):
    project = _project(project_id)
    file_name = request.headers.get("x-file-name", "").replace("\\", "/").split("/")[-1]
    if not file_name or Path(file_name).suffix.lower() not in IMAGE_TYPES:
        raise HTTPException(400, "Upload a PNG, JPEG, WebP, or GIF file")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > settings.project_upload_max_bytes:
            raise HTTPException(413, f"Reference image exceeds {settings.project_upload_max_bytes} bytes")
        chunks.append(chunk)
    content = b"".join(chunks)
    mime, digest = _validate_image_bytes(file_name, content)
    sent_mime = request.headers.get("content-type", "").split(";")[0].strip()
    if sent_mime not in (mime, "application/octet-stream"):
        raise HTTPException(400, "Upload MIME type does not match image content")
    storage = _reference_storage_root(project, category)
    storage.mkdir(parents=True, exist_ok=True)
    stored_path = storage / f"{uuid4().hex}{Path(file_name).suffix.lower()}"
    try:
        with stored_path.open("xb") as handle:
            handle.write(content)
    except OSError as exc:
        raise HTTPException(500, f"Could not store uploaded reference safely: {exc}") from exc
    record = {"id": replace_id or str(uuid4()), "project_id": project_id, "name": name.strip(), "category": category,
              "path": str(stored_path), "source_kind": "uploaded", "notes": notes, "mime_type": mime,
              "size_bytes": total, "content_hash": digest}
    try:
        if replace_id:
            if not db.get_project_reference(project_id, replace_id):
                raise HTTPException(404, "Project reference not found")
            return db.update_project_reference(project_id, replace_id, **{k:v for k,v in record.items() if k not in ("id", "project_id")})
        return db.add_project_reference(record)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "This image content is already linked to the project") from exc
    finally:
        # The upload is project-owned; keep it even when the old link is replaced or removed.
        pass


@app.patch("/api/projects/{project_id}/references/{reference_id}")
def update_project_reference(project_id: str, reference_id: str, request: ProjectReferenceUpdate):
    _project(project_id)
    if not db.get_project_reference(project_id, reference_id):
        raise HTTPException(404, "Project reference not found")
    values = request.model_dump(exclude_unset=True)
    if "path" in values and values["path"] is not None:
        path, mime, size, digest = _validate_image_path(values.pop("path"))
        values.update(path=str(path), source_kind="linked", mime_type=mime, size_bytes=size, content_hash=digest)
    if "name" in values and values["name"] is not None:
        values["name"] = values["name"].strip()
    try:
        return db.update_project_reference(project_id, reference_id, **values)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "This image content is already linked to the project") from exc


@app.delete("/api/projects/{project_id}/references/{reference_id}", status_code=204)
def remove_project_reference(project_id: str, reference_id: str):
    _project(project_id)
    try:
        db.remove_project_reference(project_id, reference_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/projects/{project_id}/references/{reference_id}/file")
def project_reference_file(project_id: str, reference_id: str):
    _project(project_id)
    item = db.get_project_reference(project_id, reference_id)
    if not item:
        raise HTTPException(404, "Project reference not found")
    try:
        path = safe_local_path(item["path"], settings, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, "Reference file is no longer available") from exc
    if not path.is_file():
        raise HTTPException(404, "Reference file is no longer available")
    return FileResponse(path, media_type=item["mime_type"], headers={"Content-Disposition": "inline"})


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
def list_storyboards(project_id: str | None = None):
    selected = project_id or db.get_setting("active_project_id")
    if not selected:
        return []
    _project(selected)
    return db.list_storyboards(selected)


@app.get("/api/film-orchestration/capabilities")
def film_orchestration_capabilities():
    local_planner = LocalStoryPlanner(settings.story_planner_command, settings.story_planner_timeout_seconds).availability()
    gemini = _gemini_planner_config()
    return {
        "stages": list(STAGES),
        "general_story_planner": {"available": gemini["status"] == "connected" or local_planner["available"],
                                   "status": gemini["status"] if gemini["status"] == "connected" else local_planner["status"],
                                   "detail": "Gemini is connected and has an account-verified Flash model selected." if gemini["status"] == "connected" else local_planner["detail"]},
        "story_planners": {"recommended": "gemini", "gemini": {"available": gemini["status"] == "connected", "status": gemini["status"], "detail": gemini["detail"]},
                           "local": local_planner},
        "legacy_planner": {"available": True, "status": "limited_fixture",
                           "detail": "The existing deterministic planner is limited to the approved Scorpion & Frog fable."},
        "technical_verification": {"available": True, "method": "ffprobe"},
        "creative_quality_detection": {"available": False, "status": "manual_review_required",
                                        "detail": "Technical verification is separate from human creative review; no automated visual quality detector is claimed."},
        "generation_worker": {"available": True, "concurrency": 1},
        "remotion": {"available": Path(settings.expanded(settings.remotion_executable)).is_file(),
                     "explicit_confirmation_required": True},
    }


def _gemini_planner_config() -> dict:
    try:
        key, source = gemini_secret_store.get()
    except SecretStoreError as exc:
        return {"status": "storage_error", "source": None, "model": None, "models": [], "detail": str(exc)}
    saved = db.get_setting("story_planner_gemini") or {}
    cached_models = saved.get("models", []) if key else []
    models = [model for model in cached_models if isinstance(model, dict) and _stored_model_is_text_eligible(model)]
    if not key:
        status, detail = "missing_key", "Connect your own Gemini API key in Settings; cloud planning stays opt-in per film run."
    elif saved.get("status") == "connected" and models:
        status, detail = "connected", "The account list includes text-output candidates. Listing does not guarantee inference availability; use the explicit endpoint test before planning."
    elif cached_models and not models:
        status, detail = "no_text_models", "Previously listed models are not suitable text-output StoryPlan models (for example, TTS/audio, image, embedding, video, or live variants). Refresh the account model list after text-output access is available."
    elif saved.get("status") == "connection_failed":
        status, detail = "connection_failed", {
            "auth_denied": "Gemini rejected the key or account. Check API access and permissions, then test again.",
            "rate_limit": "Gemini connection testing reached an account rate or quota limit. Wait and review the account quota.",
            "service_unavailable": "Gemini was unavailable during the last connection test. Retry after the provider recovers.",
            "timeout": "The last Gemini connection test timed out. Its result is unknown; test again manually.",
            "model_or_endpoint_incompatible": "Gemini returned 404 during the last connection test. Confirm the API is enabled for this account, then retest and choose a Flash model supporting generateContent.",
        }.get(saved.get("error_code"), "The last Gemini connection test failed. Recheck the key and account model access, then test again.")
    elif saved.get("status") == "no_text_models":
        status, detail = "no_text_models", "The key was accepted, but the account returned no text-output Flash model suitable for story planning. TTS/audio, image, embedding, video, and live variants are excluded."
    else:
        status, detail = "configured_not_tested", "A backend-only Gemini key is configured. Test the connection to list account-available Flash models."
    eligible_ids = {item.get("id") for item in models}
    model = saved.get("model") if saved.get("model") in eligible_ids else (models[0]["id"] if models else None)
    if saved.get("model") and saved.get("model") not in eligible_ids and models:
        detail = (f"Previously selected model {saved['model']} is not eligible for text story output. "
                  f"FRAMEWORK selected the first account-listed text-output candidate, {model}; run the explicit endpoint test before planning.")
    return {"status": status, "source": source, "model": model, "models": models, "detail": detail}


@app.get("/api/story-planner/config")
def story_planner_config():
    config = _gemini_planner_config()
    return {"provider_default": "gemini", "gemini": config,
            "gemini_timeout_seconds": settings.gemini_timeout_seconds,
            "local": LocalStoryPlanner(settings.story_planner_command, settings.story_planner_timeout_seconds).availability(),
            "privacy": {"uploads_media": False, "remote_fields": ["story prompt", "project style text", "preset and generation budget", "opaque selected-reference IDs and categories"],
                        "omitted_fields": ["reference images", "generated media", "file paths", "filenames", "reference notes", "entire project database"]}}


def _store_gemini_key_value(api_key: str):
    try:
        gemini_secret_store.save(api_key)
    except (ValueError, SecretStoreError) as exc:
        raise HTTPException(400, str(exc)) from None
    saved = db.get_setting("story_planner_gemini") or {}
    db.put_setting("story_planner_gemini", {"status": "configured_not_tested", "models": [], "model": None,
                                               "last_connection_test_at": saved.get("last_connection_test_at")})
    return {"saved": True, "status": "configured_not_tested", "source": "local_file",
            "detail": "Key stored by the backend with owner-only file permissions. The key is never returned to the browser."}


@app.post("/api/story-planner/gemini/key")
async def save_gemini_key(request: Request):
    try:
        if int(request.headers.get("content-length", "0")) > 8192:
            raise HTTPException(413, "Gemini key request body exceeds the 8 KiB limit.")
    except ValueError:
        raise HTTPException(400, "Invalid request body size.") from None
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Provide a Gemini API key in a JSON request body.") from None
    api_key = payload.get("api_key") if isinstance(payload, dict) else None
    if not isinstance(api_key, str):
        raise HTTPException(400, "Provide a Gemini API key as a string.")
    return _store_gemini_key_value(api_key)


@app.delete("/api/story-planner/gemini/key")
def remove_gemini_key():
    try:
        gemini_secret_store.delete()
    except SecretStoreError as exc:
        raise HTTPException(409, str(exc)) from None
    db.put_setting("story_planner_gemini", {"status": "missing_key", "models": [], "model": None})
    return {"removed": True, "status": "missing_key"}


@app.post("/api/story-planner/gemini/test")
def test_gemini_connection():
    try:
        key, source = gemini_secret_store.get()
    except SecretStoreError as exc:
        raise HTTPException(409, str(exc)) from None
    if not key:
        raise HTTPException(409, "Gemini API key is missing. Connect it in Settings first.")
    saved = db.get_setting("story_planner_gemini") or {}
    try:
        last = float(saved.get("last_connection_test_at") or 0)
    except (TypeError, ValueError):
        last = 0
    if __import__("time").time() - last < 10:
        raise HTTPException(429, "Connection test was just run. Wait 10 seconds before testing again.")
    now = __import__("time").time()
    recent_tests = [float(value) for value in saved.get("connection_test_times", [])
                    if isinstance(value, (int, float)) and value >= now - 3600]
    if len(recent_tests) >= 10:
        raise HTTPException(429, "FRAMEWORK limits Gemini account connection tests to 10 per hour. Wait before testing again.")
    recent_tests.append(now)
    saved["connection_test_times"] = recent_tests
    saved["last_connection_test_at"] = now
    db.put_setting("story_planner_gemini", saved)
    try:
        models = GeminiStoryPlanner(key, None, settings.gemini_timeout_seconds).test_connection()
    except GeminiPlanningError as exc:
        db.put_setting("story_planner_gemini", {"status": "connection_failed", "models": [], "model": None,
                                                  "error_code": exc.code, "last_connection_test_at": now,
                                                  "connection_test_times": recent_tests})
        raise HTTPException(502 if exc.code == "service_unavailable" else 401 if exc.code == "auth_denied" else 400,
                            {"code": exc.code, "message": str(exc)}) from None
    if not models:
        db.put_setting("story_planner_gemini", {"status": "no_text_models", "models": [], "model": None,
                                                  "last_connection_test_at": now, "connection_test_times": recent_tests})
        raise HTTPException(409, "The account key was accepted, but no text-output Flash model suitable for story planning was listed. TTS/audio, image, embedding, video, and live variants are excluded.")
    ids = {item["id"] for item in models}
    selected = saved.get("model") if saved.get("model") in ids else models[0]["id"]
    db.put_setting("story_planner_gemini", {"status": "connected", "models": models, "model": selected,
                                              "source": source, "last_connection_test_at": now,
                                              "connection_test_times": recent_tests})
    return {"connected": True, "status": "connected", "models": models, "model": selected,
            "detail": "Key accepted. Models were listed from this account; only available Flash-class models are selectable."}


@app.post("/api/story-planner/gemini/test-generation")
def test_gemini_generation_endpoint(payload: GeminiGenerationEndpointProbeRequest):
    """Explicit, fixed-text API probe; does not create a film run or planner attempt."""
    if not payload.confirmed:
        raise HTTPException(400, "Confirm the potentially billable Gemini generation test before continuing.")
    try:
        key, _source = gemini_secret_store.get()
    except SecretStoreError as exc:
        raise HTTPException(409, str(exc)) from None
    if not key:
        raise HTTPException(409, "Gemini API key is missing. Connect it in Settings first.")
    config = _gemini_planner_config()
    if config["status"] != "connected":
        raise HTTPException(409, "Test the Gemini connection and list account models before probing generation.")
    if payload.model not in {item["id"] for item in config["models"]}:
        raise HTTPException(400, "Choose a model returned by the latest successful Gemini account test.")
    try:
        return GeminiStoryPlanner(key, payload.model, settings.gemini_timeout_seconds).test_generation_endpoint()
    except GeminiPlanningError as exc:
        http_status = exc.diagnostics.get("http_status")
        response_status = (502 if isinstance(http_status, int) and http_status >= 500 else
                           504 if exc.code == "timeout" else
                           422 if exc.code in {"non_text_response", "no_text_response", "token_exhaustion", "blocked_content", "empty_response"} else 400)
        raise HTTPException(response_status, {"code": exc.code, "message": str(exc),
                                               "diagnostics": exc.diagnostics}) from None


@app.patch("/api/story-planner/gemini/model")
def select_gemini_model(payload: GeminiModelRequest):
    config = _gemini_planner_config()
    if config["status"] != "connected":
        raise HTTPException(409, "Test the Gemini connection before selecting an account model.")
    if payload.model not in {item["id"] for item in config["models"]}:
        raise HTTPException(400, "Choose a Flash-class model returned by the last successful account connection test.")
    saved = db.get_setting("story_planner_gemini") or {}
    saved["model"] = payload.model
    db.put_setting("story_planner_gemini", saved)
    return {"model": payload.model, "status": "connected"}


@app.post("/api/projects/{project_id}/film-runs/preview")
def preview_film_run(project_id: str, request: CreateFilmRunRequest):
    """Read-only cost and capability preview; it writes no run and queues no jobs."""
    _project(project_id)
    for reference_id in request.reference_ids:
        detail = _reference_details(project_id, reference_id)
        if _is_historical_visual_reference(detail):
            raise HTTPException(400, "Historical outputs cannot be silently attached to a new film run")
    frames, fps = _preset_frames(request.preset)
    benchmark = next((row for row in db.benchmarks()
                      if row["frames"] == frames and row["fps"] == fps and row["elapsed_seconds"] is not None), None)
    samples = []
    for job in db.list_jobs(project_id=project_id, limit=1000):
        payload = job.get("payload", {})
        verified = (job.get("verification_metadata") or {}).get("verified_output") or {}
        path = Path(job["result_path"]) if job.get("result_path") else None
        if (job.get("kind") == "generation" and job.get("status") == "completed" and path and path.is_file()
                and payload.get("frames") == frames and payload.get("fps") == fps
                and verified.get("frames") == frames and verified.get("fps") == fps):
            try:
                samples.append(path.stat().st_size)
            except OSError:
                pass
    estimated_shots = request.budget.max_shots * (request.budget.max_retries_per_shot + 1)
    output_root = settings.expanded(settings.output_root)
    available_output_bytes = _available_bytes_for_path(output_root)
    return {
        "preview_only": True,
        "writes_database": False,
        "queues_jobs": False,
        "general_story_planner_available": LocalStoryPlanner(settings.story_planner_command, settings.story_planner_timeout_seconds).availability()["available"],
        "planner_message": LocalStoryPlanner(settings.story_planner_command, settings.story_planner_timeout_seconds).availability()["detail"],
        "planner_provider": request.planner_provider,
        "gemini_available": _gemini_planner_config()["status"] == "connected",
        "requested_max_shots": request.budget.max_shots,
        "preset": request.preset,
        "frames": frames,
        "fps": fps,
        "maximum_footage_seconds": round(request.budget.max_shots * frames / fps, 2),
        "generation_time_estimate_seconds": round(float(benchmark["elapsed_seconds"]) * estimated_shots, 2) if benchmark else None,
        "generation_time_sample": ("local benchmark history" if benchmark else None),
        "estimated_output_bytes": int(sorted(samples)[len(samples)//2] * estimated_shots) if samples else None,
        "estimated_output_within_cap": (int(sorted(samples)[len(samples)//2] * estimated_shots) <= request.budget.max_output_bytes) if samples else None,
        "generation_time_within_cap": (float(benchmark["elapsed_seconds"]) * estimated_shots <= request.budget.max_generation_seconds) if benchmark else None,
        "storage_sample_count": len(samples),
        "storage_hard_cap_bytes": request.budget.max_output_bytes,
        "available_output_bytes": available_output_bytes,
        "disk_sufficient_for_budget": available_output_bytes >= request.budget.max_output_bytes,
        "generation_time_hard_cap_seconds": request.budget.max_generation_seconds,
        "maximum_retries_per_shot": request.budget.max_retries_per_shot,
        "technical_verification": "ffprobe",
        "creative_quality_status": "manual review required; no automated visual detector is implemented",
    }


@app.get("/api/projects/{project_id}/film-runs")
def list_film_runs(project_id: str):
    _project(project_id)
    return [_film_run_status(run) for run in film_orchestrator.list(project_id)]


def _film_run_status(run: dict) -> dict:
    provider = run.get("request", {}).get("planner_provider", "local")
    if provider == "gemini":
        run["planner_status"] = _gemini_planner_config()["status"]
        request = db.film_run_planner_request(run["id"], include_result=False)
        run["planner_request"] = ({"status": request["status"], "provider": request["provider"], "model": request["model"],
                                   "revision": request.get("revision", 1), "schema_version": request.get("schema_version", "legacy-unversioned-schema"),
                                   "response_mode": request.get("response_mode", "schema"),
                                   "attempts": request["attempts"], "remaining_attempts": request.get("remaining_attempts", max(0, 3-request["attempts"])),
                                   "error_code": request.get("error_code"), "error_details": request.get("error_details")} if request else None)
        history = db.film_run_planner_attempts(run["id"], include_result=False)
        run["planner_attempts"] = [{"revision": item["revision"], "provider": item["provider"], "model": item["model"],
            "schema_version": item["schema_version"], "response_mode": item.get("response_mode", "schema"),
            "status": item["status"], "attempts": item["attempts"],
            "remaining_attempts": item["remaining_attempts"], "error_code": item.get("error_code"),
            "error_details": item.get("error_details"), "consent": item.get("consent")} for item in history]
        run["planner_schema_current"] = STORY_PLAN_SCHEMA_VERSION
        run["planner_attempt_limit"] = MAX_GEMINI_ATTEMPTS_PER_REVISION
    else:
        run["planner_status"] = LocalStoryPlanner(settings.story_planner_command, settings.story_planner_timeout_seconds).availability()["status"]
    batch_id = run.get("film_batch_id") or run.get("checkpoint", {}).get("generate_shots", {}).get("film_batch_id")
    batch = db.get_film_batch(batch_id) if batch_id else None
    run["generation_jobs_created"] = batch["total_shots"] if batch else 0
    run["generation_progress"] = ({"total": batch["total_shots"], "verified": batch["completed_shots"],
                                    "queued": batch["queued_shots"], "failed": batch["failed_shots"],
                                    "progress": batch["progress"],
                                    "shots": [{"title": shot["title"], "scene_title": shot["scene_title"],
                                               "status": shot["status"], "progress": shot.get("progress", 0),
                                               "events": (shot.get("job") or {}).get("events", [])[-8:]}
                                              for shot in batch["shots"]]}
                                   if batch else None)
    run["creative_review_status"] = "pending" if run["status"] == "needs_review" else None
    return run


@app.post("/api/projects/{project_id}/film-runs", status_code=201)
def create_film_run(project_id: str, request: CreateFilmRunRequest):
    project = _project(project_id)
    if request.planner_provider == "gemini" and not request.remote_processing_consent:
        raise HTTPException(400, "Gemini planning requires explicit opt-in to send the story prompt and disclosed metadata to Google.")
    if request.planner_provider == "gemini" and not request.initial_confirmation:
        raise HTTPException(400, "Gemini planning is authorized only as part of an explicitly confirmed film run.")
    gemini_config = _gemini_planner_config() if request.planner_provider == "gemini" else None
    if request.planner_provider == "gemini" and request.planner_model and request.planner_model not in {item["id"] for item in gemini_config["models"]}:
        raise HTTPException(400, "Choose a Gemini Flash model returned by the last successful account connection test.")
    preview = preview_film_run(project_id, request)
    free_bytes = preview["available_output_bytes"]
    if not request.plan_only and free_bytes < request.budget.max_output_bytes:
        raise HTTPException(409, f"The approved output storage cap ({request.budget.max_output_bytes} bytes) exceeds current free space ({free_bytes} bytes). Lower the cap or free disk space and re-preview.")
    if not request.plan_only and preview["estimated_output_within_cap"] is False:
        raise HTTPException(409, "Measured output-size samples exceed the approved storage cap. Increase the cap or reduce the maximum shot/retry count and re-preview.")
    if not request.plan_only and preview["generation_time_within_cap"] is False:
        raise HTTPException(409, "The measured time estimate exceeds the approved generation time cap. Increase the cap or reduce the maximum shot/retry count and re-preview.")
    references = []
    for reference_id in request.reference_ids:
        reference = db.get_project_reference(project_id, reference_id)
        if not reference:
            raise HTTPException(400, "Character and environment attachments must be linked to the selected project")
        detail = _reference_details(project_id, reference_id)
        if _is_historical_visual_reference(detail):
            raise HTTPException(400, "Historical outputs cannot be silently attached to a new film run")
        references.append({"id": reference_id, "category": reference["category"]})
    try:
        frames, fps = _preset_frames(request.preset)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    request_data = {
        "prompt": request.prompt,
        "reference_ids": [item["id"] for item in references],
        "reference_categories": [item["category"] for item in references],
        "planner_provider": request.planner_provider,
        "planner_model": (request.planner_model or (gemini_config.get("model") if gemini_config else None)),
        "planner_response_mode": request.planner_response_mode,
        "plan_only": request.plan_only,
        "remote_processing_consent": ({"confirmed": True, "provider": "Google Gemini API",
            "fields": ["story prompt", "project style text", "preset and generation budget", "opaque selected-reference IDs and categories"],
            "media_uploaded": False} if request.planner_provider == "gemini" and request.remote_processing_consent else {}),
        "preset": request.preset,
        "budget": request.budget.model_dump(),
        "generation_profile": {"frames": frames, "fps": fps, "width": 448, "height": 768},
        "authorization": ({"confirmed": True, "scope": ("Gemini story planning only; no LTX generation until separately authorized" if request.plan_only else "local LTX jobs within the declared shot/time/storage budget; Gemini planning explicitly opted in" if request.planner_provider == "gemini" else "local LTX jobs within the declared shot/time/storage budget")}
                          if request.initial_confirmation else {}),
    }
    try:
        run = film_orchestrator.create(project["id"], request.idempotency_key, request_data,
                                       authorized=request.initial_confirmation)
    except FilmRunConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        status = 409 if "Idempotency key" in str(exc) else 400
        raise HTTPException(status, str(exc)) from exc
    run = _film_run_status(run)
    local_state = LocalStoryPlanner(settings.story_planner_command, settings.story_planner_timeout_seconds).availability()
    gemini_state = _gemini_planner_config()
    run["planner_status"] = gemini_state["status"] if request.planner_provider == "gemini" else local_state["status"]
    if run.get("deduplicated"):
        run["message"] = "An identical active film request already exists. Reusing its durable plan and queue; no duplicate planner API request or LTX jobs were created."
    elif not request.initial_confirmation:
        run["message"] = "A dry request was saved. Explicit initial authorization is required before any future planning or generation stage."
    elif request.planner_provider == "gemini":
        run["message"] = (("Plan Only is authorized: one explicit story-planning action will be attempted; LTX generation still requires a separate confirmation. References and generated media remain local." if request.plan_only else "The authorized run will use Gemini only for structured story planning; references and generated media remain local.") if gemini_state["status"] == "connected"
                          else gemini_state["detail"] + " The run is saved and no shot jobs will be queued until planning succeeds.")
    else:
        run["message"] = ("The authorized request was saved and will be planned locally." if local_state["available"]
                          else local_state["detail"])
    return run


@app.get("/api/film-runs/{run_id}")
def get_film_run(run_id: str):
    run = film_orchestrator.get(run_id)
    _project(run["project_id"])
    return _film_run_status(run)


def _film_run_control(run_id: str, request: FilmRunRevisionRequest, action: str):
    try:
        if action == "pause":
            return film_orchestrator.pause(run_id, request.expected_revision)
        if action == "resume":
            return film_orchestrator.resume(run_id, request.expected_revision)
        cancelled = film_orchestrator.cancel(run_id, request.expected_revision)
        active_job_id = worker.active_job_id
        if active_job_id:
            budget = db.film_run_budget_for_job(active_job_id)
            if budget and budget["run_id"] == run_id:
                worker.cancel_job(active_job_id)
        return cancelled
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (RuntimeError, FilmRunTransitionError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/film-runs/{run_id}/authorize")
def authorize_film_run(run_id: str, request: FilmRunAuthorizationRequest):
    if not request.confirmed or not request.budget_acknowledged:
        raise HTTPException(422, "Explicit confirmation of the run scope and budget is required")
    try:
        run = film_orchestrator.get(run_id)
        if (run["status"] == "awaiting_authorization" and run["stage"] == "define_visual_bible" and
                run.get("request", {}).get("plan_only") and run.get("checkpoint", {}).get("understand_story")):
            saved = run["request"]
            preview_request = CreateFilmRunRequest(prompt=run["prompt"], reference_ids=run["reference_ids"],
                preset=run["preset"], budget=FilmRunBudget.model_validate(run["budget"]),
                idempotency_key="plan-only-generation-authorization", initial_confirmation=True,
                planner_provider=saved.get("planner_provider", "local"), planner_model=saved.get("planner_model"),
                planner_response_mode=saved.get("planner_response_mode", "schema"), plan_only=True,
                remote_processing_consent=bool(saved.get("remote_processing_consent", {}).get("confirmed")))
            preview = preview_film_run(run["project_id"], preview_request)
            if preview["available_output_bytes"] < run["budget"]["max_output_bytes"]:
                raise HTTPException(409, "The saved generation storage cap exceeds current free space. Lower the cap in a new run or free disk space before authorizing LTX jobs.")
            if preview["estimated_output_within_cap"] is False:
                raise HTTPException(409, "Verified output-size samples exceed this run's saved storage cap. Review the cap before authorizing LTX jobs.")
            if preview["generation_time_within_cap"] is False:
                raise HTTPException(409, "The measured generation estimate exceeds this run's saved time cap. Review the cap before authorizing LTX jobs.")
        return film_orchestrator.authorize(run_id, request.expected_revision,
                                           {"confirmed": True, "budget_acknowledged": True,
                                            "scope": "local LTX work within the persisted run budget"})
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (RuntimeError, FilmRunTransitionError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/film-runs/{run_id}/pause")
def pause_film_run(run_id: str, request: FilmRunRevisionRequest):
    return _film_run_control(run_id, request, "pause")


@app.post("/api/film-runs/{run_id}/resume")
def resume_film_run(run_id: str, request: FilmRunRevisionRequest):
    return _film_run_control(run_id, request, "resume")


@app.post("/api/film-runs/{run_id}/replan")
def replan_film_run(run_id: str, request: GeminiReplanRequest):
    if not request.confirm_billable_planning:
        raise HTTPException(422, "Confirm that this new Gemini structured-planning request may incur a charge.")
    run = film_orchestrator.get(run_id)
    config = _gemini_planner_config()
    if config["status"] != "connected":
        raise HTTPException(409, "Connect Gemini and test the account model list before switching planner models.")
    if request.planner_model not in {model["id"] for model in config["models"]}:
        raise HTTPException(400, "Choose a Gemini model returned by the latest successful account connection test.")
    scope = {"project_id": run["project_id"], "prompt": run["prompt"], "reference_ids": run["reference_ids"],
             "budget": run["budget"], "preset": run["preset"]}
    scope_hash = sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    consent = {"confirmed": True, "provider": "Google Gemini API", "model": request.planner_model,
        "scope": "Same saved prompt, selected project references, preset, and original authorized generation budget",
        "cost_disclosure_acknowledged": True, "media_uploaded": False, "response_mode": request.response_mode,
        "fields": ["story prompt", "project style text", "preset and generation budget", "opaque selected-reference IDs and categories"]}
    try:
        updated = film_orchestrator.replan_with_gemini(run_id, request.expected_revision, request.planner_model,
            request.idempotency_key, scope_hash, STORY_PLAN_SCHEMA_VERSION, request.response_mode, consent)
        return _film_run_status(updated)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except FilmRunConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (FilmRunTransitionError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/film-runs/{run_id}/cancel")
def cancel_film_run(run_id: str, request: FilmRunRevisionRequest):
    return _film_run_control(run_id, request, "cancel")


@app.get("/api/projects/{project_id}/storyboards")
def list_project_storyboards(project_id: str):
    _project(project_id)
    return db.list_storyboards(project_id)


@app.post("/api/projects/{project_id}/storyboards", status_code=201)
def create_project_storyboard(project_id: str, request: CreateStoryboardRequest):
    _project(project_id)
    title = request.title.strip()
    if not title:
        raise HTTPException(422, "Storyboard title cannot be blank")
    return db.create_storyboard(project_id, title, [], request.story_text.strip())


@app.post("/api/projects/{project_id}/film-plans", status_code=201)
def build_film_plan(project_id: str, request: BuildFilmPlanRequest):
    """Build a separate, persistent plan for the approved river-crossing fable."""
    project = _project(project_id)
    try:
        story_text = validate_approved_fable(request.story_text)
    except UnsupportedStory as exc:
        raise HTTPException(422, str(exc)) from exc
    if request.frog_reference_id == request.scorpion_reference_id:
        raise HTTPException(400, "Choose two distinct linked character references")
    frog = _reference_by_id(project_id, request.frog_reference_id, "frog")
    scorpion = _reference_by_id(project_id, request.scorpion_reference_id, "scorpion")
    try:
        frames, preset_fps = _preset_frames(request.preset)
    except HTTPException:
        raise
    defaults = db.get_setting(_generation_settings_key(project_id)) or GenerationSettings().model_dump()
    width, height = _default_dimensions(request.target_format)
    fps = project.get("frame_rate") or preset_fps
    style_bible = project.get("style_bible") or {}
    style_bible_notice = ""
    if re.search(r"do not use riverbank or water imagery", style_bible.get("negative_constraints", ""), re.IGNORECASE):
        previous_revision = project.get("style_bible_revision", 0)
        revised = dict(style_bible)
        constraints = re.sub(r"do not use riverbank or water imagery[,;.]?\s*", "", revised.get("negative_constraints", ""), flags=re.IGNORECASE).strip()
        constraints = re.sub(r"\s+", " ", constraints).strip(" ,;.")
        required_note = "River and water imagery is required by this approved river-crossing story. Do not reuse riverbank-master.png, prior videos, benchmark media, or old storyboard content."
        revised["negative_constraints"] = (constraints + " " if constraints else "") + required_note
        project = db.update_project_style_bible(project_id, revised)
        style_bible = project["style_bible"]
        style_bible_notice = (f"Updated the conflicting no-water rule for this river-crossing film as style-bible revision "
                              f"{project['style_bible_revision']}; the prior revision is retained in local style history.")
    style_context = _style_context(style_bible)
    output = defaults.get("output_location") or str(_project_output(project))
    try:
        output = str(safe_local_path(output, settings))
        shots, metadata = make_approved_fable_plan(
            frog=frog, scorpion=scorpion, style_context=style_context,
            target_format=request.target_format, preset=request.preset, frames=frames,
            width=width, height=height, fps=fps,
            seed=request.seed if request.seed is not None else defaults.get("seed"),
            output_location=output,
            generation_profile={key: defaults.get(key) for key in (
                "model", "distilled_two_stage", "low_ram")},
        )
        for shot in shots:
            reference = shot["reference_image"]
            config = GenerationSettings.model_validate(defaults | {
                "model": defaults.get("model", "dgrauet/ltx-2.3-mlx-q8"),
                "width": width, "height": height, "distilled_two_stage": defaults.get("distilled_two_stage", True),
                "low_ram": defaults.get("low_ram", True), "audio": False,
                "seed": shot["seed"], "prompt": shot["prompt"], "reference_image": reference,
                "output_location": output, "preset": request.preset,
            })
            payload = _generation_payload(config, project_id, fps_override=fps)
            payload.update(reference_conditioning="one-image", reference_id=shot["reference_ids"][0],
                           shot_prompt=shot["prompt"], style_context=style_context,
                           shot_settings={"width": width, "height": height, "fps": fps,
                               "output_location": output, "framing": shot["framing"],
                               "style_bible_revision": project.get("style_bible_revision", 0)})
            shot["generation_payload"] = payload
        metadata.update({"target_format": request.target_format, "preset": request.preset,
                        "frames_per_shot": frames, "fps": fps, "width": width, "height": height,
                        "expected_workload": f"{len(shots)} sequential LTX jobs · approximately {metadata['estimated_seconds']:.2f}s total source footage",
                        "ltx_ready_at_plan_time": detect_integrations(settings)["ltx"]["ready"],
                        "preflight_warnings": sorted({warning for shot in shots for warning in shot["generation_payload"].get("preflight_warnings", [])}),
                        "approved_reference_ids": [frog["id"], scorpion["id"]],
                        "style_bible_revision": project.get("style_bible_revision", 0),
                        "style_bible_snapshot": style_bible,
                        "style_bible_notice": style_bible_notice,
                        "experimental_drift_warning": request.preset == "Experimental",
                        "audio_available": False})
        matching_benchmarks = [item for item in db.benchmarks()
            if item["preset"] == request.preset and item["frames"] == frames and item["fps"] == fps
            and item.get("elapsed_seconds") is not None]
        if matching_benchmarks:
            average_seconds = sum(float(item["elapsed_seconds"]) for item in matching_benchmarks) / len(matching_benchmarks)
            metadata["benchmark_estimate"] = {"samples": len(matching_benchmarks),
                "average_seconds_per_shot": round(average_seconds, 2),
                "total_seconds": round(average_seconds * len(shots), 2),
                "source": "matching completed local job history"}
            metadata["expected_workload"] = (f"{len(shots)} sequential LTX jobs · approximately "
                f"{average_seconds * len(shots) / 60:.1f} min from {len(matching_benchmarks)} matching local benchmark(s)")
        else:
            metadata["benchmark_estimate"] = None
            metadata["expected_workload"] = f"{len(shots)} sequential LTX jobs · no matching local timing benchmark; run time unknown"
        return db.create_film_plan(project_id, request.title.strip(), story_text, shots,
                                   request.target_format, request.preset, metadata)
    except HTTPException:
        raise
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, f"Could not prepare generation-ready shots: {exc}") from exc


@app.post("/api/storyboards/{storyboard_id}/film-plan/approve")
def approve_film_plan(storyboard_id: str, request: ApproveFilmPlanRequest):
    try:
        return db.approve_film_plan(storyboard_id, request.revision, request.style_bible_revision)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


def _expected_shot_output(shot: dict) -> dict:
    payload = shot.get("generation_payload") or {}
    return {"width": int(payload.get("width") or shot.get("width") or 0),
            "height": int(payload.get("height") or shot.get("height") or 0),
            "fps": int(payload.get("fps") or shot.get("fps") or 0),
            "frames": int(payload.get("frames") or 0)}


def _job_matches_shot(job: dict, shot: dict, storyboard: dict, batch_id: str) -> bool:
    payload = job.get("payload") or {}
    expected_payload = shot.get("generation_payload") or {}
    return bool(
        job.get("kind") == "generation" and job.get("status") == "completed" and job.get("exit_code") == 0 and
        job.get("project_id") == storyboard["project_id"] and
        payload.get("shot_id") == shot["id"] and payload.get("storyboard_id") == storyboard["id"] and
        payload.get("film_batch_id") == batch_id and
        db.generation_settings_snapshot(payload) == db.generation_settings_snapshot(shot) and
        payload.get("reference_image") == expected_payload.get("reference_image") and
        payload.get("frames") == expected_payload.get("frames") and payload.get("audio") is False
    )


def _verified_source_batches(story: dict) -> list[dict]:
    if not story.get("film_plan", {}).get("approved"):
        return []
    shot_by_id = {shot["id"]: shot for shot in story["shots"]}
    candidates = []
    for batch in db.list_film_batches(story["id"]):
        if (batch["status"] != "completed" or batch["project_id"] != story["project_id"] or
                batch["storyboard_revision"] != story["revision"] or
                batch["style_bible_revision"] != story["style_bible_revision"] or len(batch["shots"]) != len(shot_by_id)):
            continue
        matches = {}
        reasons = []
        for item in batch["shots"]:
            shot = shot_by_id.get(item["shot_id"])
            job = item.get("job")
            if not shot or not job or not _job_matches_shot(job, shot, story, batch["id"]):
                reasons.append(item.get("title", item["shot_id"]) + ": job or settings do not match this approved shot")
                continue
            expected = _expected_shot_output(shot)
            recorded = (job.get("verification_metadata") or {}).get("verified_output")
            if not recorded or any(recorded.get(key) != value for key, value in expected.items()):
                reasons.append(shot["title"] + ": recorded ffprobe metadata does not match the shot")
                continue
            try:
                path = safe_local_path(job.get("result_path") or "", settings, must_exist=True)
                output_root = safe_local_path(settings.output_root, settings)
                if not path.is_file() or output_root not in path.parents:
                    raise ValueError("output is outside OUTPUT_ROOT or is not a file")
            except (ValueError, FileNotFoundError) as exc:
                reasons.append(shot["title"] + ": " + str(exc))
                continue
            matches[shot["id"]] = job["id"]
        if len(matches) == len(shot_by_id):
            candidates.append({"id": batch["id"], "created_at": batch["created_at"],
                               "verified_shots": len(matches), "total_shots": len(shot_by_id),
                               "job_ids": matches})
    return candidates


def _film_export_status(story: dict) -> dict:
    candidates = _verified_source_batches(story)
    shots = story.get("shots", [])
    current_verified = [shot["id"] for shot in shots if shot.get("status") == "verified" and shot.get("settings_match")]
    missing = [shot["title"] for shot in shots if shot["id"] not in current_verified]
    batches = db.list_film_batches(story["id"])
    paused = [{"id": batch["id"], "status": batch["status"],
               "queued_jobs": [item["job"]["id"] for item in batch["shots"] if item.get("job") and item["job"].get("status") == "queued"],
               "completed_jobs": [item["job"]["id"] for item in batch["shots"] if item.get("job") and item["job"].get("status") == "completed"]}
              for batch in batches if batch["status"] == "paused"]
    manifest = story.get("manifest")
    manifest_ready = bool(manifest and manifest.get("revision") == story["revision"])
    all_verified = bool(shots) and len(current_verified) == len(shots)
    reconciled = bool(shots) and all(shot.get("reconciled_job_id") == (shot.get("job") or {}).get("id") for shot in shots)
    latest_render = None
    for job in db.list_jobs(limit=500, project_id=story["project_id"]):
        job_manifest = ((job.get("payload") or {}).get("props") or {}).get("manifest") or {}
        if job.get("kind") == "render" and job_manifest.get("storyboard_id") == story["id"]:
            latest_render = {"id": job["id"], "status": job["status"], "progress": job["progress"],
                             "result_path": job.get("result_path"), "elapsed_seconds": job.get("elapsed_seconds"),
                             "error": job.get("error"), "verification_metadata": job.get("verification_metadata"),
                             "current": bool(manifest_ready and job_manifest == manifest),
                             "manifest_revision": job_manifest.get("revision")}
            break
    return {"storyboard_id": story["id"], "revision": story["revision"],
            "style_bible_revision": story["style_bible_revision"], "approved": bool(story.get("film_plan", {}).get("approved")),
            "total_shots": len(shots), "verified_shots": len(current_verified), "missing_shots": missing,
            "all_verified": all_verified, "reconciled": reconciled, "manifest_ready": manifest_ready,
            "manifest_path": story.get("manifest_path"), "manifest": manifest,
            "remotion_ready": detect_integrations(settings)["remotion"]["ready"],
            "latest_render": latest_render,
            "source_batches": [{key: value for key, value in candidate.items() if key != "job_ids"} for candidate in candidates],
            "recommended_source_batch_id": candidates[0]["id"] if len(candidates) == 1 else None,
            "ambiguous_source_batches": len(candidates) > 1, "paused_batches": paused}


@app.get("/api/storyboards/{storyboard_id}/film-plan/export-status")
def film_plan_export_status(storyboard_id: str):
    story = db.get_storyboard(storyboard_id)
    if not story or not story.get("film_plan"):
        raise HTTPException(404, "Film plan not found")
    return _film_export_status(story)


@app.post("/api/storyboards/{storyboard_id}/film-plan/reconcile")
async def reconcile_verified_shots(storyboard_id: str, request: ReconcileVerifiedShotsRequest):
    story = db.get_storyboard(storyboard_id, include_events=False)
    if not story or not story.get("film_plan"):
        raise HTTPException(404, "Film plan not found")
    if (not story["film_plan"]["approved"] or story["revision"] != request.revision or
            story["style_bible_revision"] != request.style_bible_revision):
        raise HTTPException(409, "The current storyboard and style bible need approval before reconciling outputs")
    source = next((item for item in _verified_source_batches(story) if item["id"] == request.source_batch_id), None)
    if not source:
        raise HTTPException(409, "The selected batch is not a complete, current, project-scoped set of verified shots")
    shots_by_id = {shot["id"]: shot for shot in story["shots"]}
    for shot_id, job_id in source["job_ids"].items():
        shot, job = shots_by_id[shot_id], db.get_job(job_id)
        if not job or not _job_matches_shot(job, shot, story, source["id"]):
            raise HTTPException(409, f"Shot '{shot['title']}' changed or its recorded job is no longer eligible; nothing was reconciled")
        expected = _expected_shot_output(shot)
        recorded = (job.get("verification_metadata") or {}).get("verified_output")
        try:
            path = safe_local_path(job.get("result_path") or "", settings, must_exist=True)
            output_root = safe_local_path(settings.output_root, settings)
            if not path.is_file() or output_root not in path.parents:
                raise ValueError("The output is not a regular file under OUTPUT_ROOT")
            actual = await verify_video(str(path), settings, expected=expected)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(409, f"Shot '{shot['title']}' failed fresh ffprobe verification: {exc}; no associations were changed") from exc
        if actual.get("verified_output") != recorded:
            raise HTTPException(409, f"Shot '{shot['title']}' current ffprobe metadata differs from its recorded result; no associations were changed")
    duplicate_job_ids = []
    for batch in db.list_film_batches(storyboard_id):
        if (batch["status"] != "paused" or batch["id"] == source["id"] or
                batch["storyboard_revision"] != request.revision or batch["style_bible_revision"] != request.style_bible_revision):
            continue
        for item in batch["shots"]:
            job = item.get("job")
            if job and job["status"] == "queued" and item["shot_id"] in shots_by_id and _job_matches_queued_duplicate(job, item["shot_id"], story, batch["id"]):
                duplicate_job_ids.append(job["id"])
    paused_before = [batch for batch in db.list_film_batches(storyboard_id) if batch["status"] == "paused"]
    canonical_ids = set(source["job_ids"].values())
    preserved_completed = sum(1 for batch in paused_before for item in batch["shots"]
                              if item.get("job") and item["job"]["status"] == "completed" and item["job"]["id"] not in canonical_ids)
    try:
        result = db.reconcile_film_shots(storyboard_id, request.revision, request.style_bible_revision,
                                         source["id"], source["job_ids"], duplicate_job_ids)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"storyboard": result["storyboard"], "cancelled_job_ids": result["cancelled_job_ids"],
            "reconciled_shots": len(source["job_ids"]), "preserved_completed_outputs": preserved_completed}


def _job_matches_queued_duplicate(job: dict, shot_id: str, story: dict, batch_id: str) -> bool:
    shot = next((item for item in story["shots"] if item["id"] == shot_id), None)
    payload = job.get("payload") or {}
    return bool(shot and job.get("status") == "queued" and job.get("kind") == "generation" and
                job.get("project_id") == story["project_id"] and payload.get("shot_id") == shot_id and
                payload.get("storyboard_id") == story["id"] and payload.get("film_batch_id") == batch_id and
                db.generation_settings_snapshot(payload) == db.generation_settings_snapshot(shot))


@app.post("/api/storyboards/{storyboard_id}/film-plan/generate", status_code=201)
def generate_film(storyboard_id: str, request: GenerateFilmRequest):
    story = db.get_storyboard(storyboard_id)
    if not story or not story.get("film_plan"):
        raise HTTPException(404, "Film plan not found")
    if not story["film_plan"]["approved"] or story["revision"] != request.revision or story["style_bible_revision"] != request.style_bible_revision:
        raise HTTPException(409, "Approve and review this exact saved plan and style-bible revision before generation")
    export_status = _film_export_status(story)
    if export_status["all_verified"] and export_status["reconciled"]:
        return {"status": "ready_for_export", "queued_shots": 0, "total_shots": len(story["shots"]),
                "export_status": export_status,
                "message": "All required shots are reconciled to current verified outputs; no jobs were queued. Continue to Export / Remotion."}
    if export_status["source_batches"]:
        return {"status": "reconciliation_required", "queued_shots": 0, "total_shots": len(story["shots"]),
                "export_status": export_status,
                "message": "A complete verified source batch already exists. Reconcile its clips and assemble them from Export / Remotion; no new generation jobs were queued."}
    if export_status["all_verified"]:
        return {"status": "ready_for_export", "queued_shots": 0, "total_shots": len(story["shots"]),
                "export_status": export_status,
                "message": "All required shots already have current verified outputs; no new jobs were queued. Continue to Export / Remotion."}
    if any(batch["queued_jobs"] for batch in export_status["paused_batches"]):
        raise HTTPException(409, "A paused film batch still contains queued work. Reconcile verified clips or cancel the duplicate queue before generating missing shots.")
    if not detect_integrations(settings)["ltx"]["ready"]:
        raise HTTPException(409, "LTX runner or cached Q8 model weights are unavailable; no weights will be downloaded")
    project = _project(story["project_id"])
    for shot in story["shots"]:
        payload = shot.get("generation_payload") or {}
        if payload.get("audio"):
            raise HTTPException(409, f"Shot '{shot['title']}' requests audio, but no audio capability is configured")
        if payload.get("prompt") != shot["prompt"]:
            raise HTTPException(409, f"Shot '{shot['title']}' saved prompt differs from its generation payload; save the shot plan again")
        reference_ids = shot.get("reference_ids") or []
        if len(reference_ids) > 1:
            raise HTTPException(409, f"Shot '{shot['title']}' selects multiple images, but the installed LTX path supports one reference image per shot")
        reference = _validate_project_reference(project, payload.get("reference_image")) if payload.get("reference_image") else None
        if len(reference_ids) == 1:
            matches = [item for item in db.project_references(project["id"]) if item["id"] == reference_ids[0]
                       and reference and Path(item["path"]).resolve() == reference.resolve()]
            if not matches:
                raise HTTPException(409, f"Shot '{shot['title']}' reference is no longer linked to this project")
        elif reference:
            raise HTTPException(409, f"Shot '{shot['title']}' has an unassociated image path")
        try:
            safe_local_path(payload.get("output_location", ""), settings)
        except ValueError as exc:
            raise HTTPException(400, f"Shot '{shot['title']}' output path is invalid: {exc}") from exc
    try:
        batch = db.create_film_batch(storyboard_id, request.revision, request.style_bible_revision)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    metrics_root = settings.expanded(settings.output_root)
    current_metrics = resource_snapshot(str(metrics_root if metrics_root.exists() else settings.allowed_roots[0]))
    batch["resource_warnings"] = resource_preflight.assess(current_metrics)
    if batch["status"] == "completed":
        try:
            create_storyboard_manifest(storyboard_id)
        except HTTPException as exc:
            batch["composition_warning"] = str(exc.detail)
    return batch


@app.get("/api/storyboards/{storyboard_id}/film-plan/batches")
def film_plan_batches(storyboard_id: str):
    story = db.get_storyboard(storyboard_id)
    if not story or not story.get("film_plan"):
        raise HTTPException(404, "Film plan not found")
    return [_batch_with_resource_warnings(batch) for batch in db.list_film_batches(storyboard_id)]


@app.get("/api/film-batches/{batch_id}")
def get_film_batch(batch_id: str):
    batch = db.get_film_batch(batch_id)
    if not batch:
        raise HTTPException(404, "Film batch not found")
    return _batch_with_resource_warnings(batch)


@app.post("/api/film-batches/{batch_id}/pause")
def pause_film_batch(batch_id: str):
    try:
        return _batch_with_resource_warnings(db.set_film_batch_status(batch_id, {"running"}, "paused"))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/film-batches/{batch_id}/resume")
def resume_film_batch(batch_id: str):
    try:
        return _batch_with_resource_warnings(db.set_film_batch_status(batch_id, {"paused"}, "running"))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/film-batches/{batch_id}/cancel")
async def cancel_film_batch(batch_id: str):
    try:
        batch, job_ids = db.cancel_film_batch(batch_id)
        for job_id in job_ids:
            worker.cancel_job(job_id)
        return _batch_with_resource_warnings(batch)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/film-batches/{batch_id}/retry")
def retry_film_batch(batch_id: str, request: RetryFilmRequest):
    batch = db.get_film_batch(batch_id)
    if not batch:
        raise HTTPException(404, "Film batch not found")
    if not detect_integrations(settings)["ltx"]["ready"]:
        raise HTTPException(409, "LTX runner or cached Q8 model weights are unavailable; no weights will be downloaded")
    story = db.get_storyboard(batch["storyboard_id"])
    if not story:
        raise HTTPException(404, "Film storyboard not found")
    project = _project(story["project_id"])
    for shot_id in request.shot_ids:
        shot = next((item for item in story["shots"] if item["id"] == shot_id), None)
        if not shot:
            raise HTTPException(400, "Selected retry shot is not part of this storyboard")
        payload = shot.get("generation_payload") or {}
        if shot.get("reference_ids") != [payload.get("reference_id")]:
            raise HTTPException(409, f"Shot '{shot['title']}' reference selection changed; review the shot and approve the plan again")
        try:
            _validate_project_reference(project, payload.get("reference_image"))
            safe_local_path(payload.get("output_location", ""), settings)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(409, f"Shot '{shot['title']}' inputs need attention: {exc}") from exc
    try:
        return _batch_with_resource_warnings(db.retry_film_batch_shots(batch_id, request.shot_ids))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


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
    existing = next((item for item in db.list_storyboards(request.project_id)), None)
    if existing:
        return existing
    raise HTTPException(409, "No storyboard exists yet. Create a blank storyboard and add shots explicitly.")


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
    project = _project(story["project_id"])
    values = []
    for shot in payload.shots:
        _, preset_fps = _preset_frames(shot.preset)
        shot_value = shot.model_dump()
        reference = _validate_project_reference(project, shot.reference_image)
        shot_value["reference_image"] = str(reference) if reference else None
        default_width, default_height = _default_dimensions(project.get("aspect_ratio"))
        shot_value["width"] = shot.width or default_width
        shot_value["height"] = shot.height or default_height
        shot_value["fps"] = shot.fps or project.get("frame_rate") or preset_fps
        shot_value["output_location"] = str(safe_local_path(shot.output_location or str(_project_output(project)), settings))
        linked_references = db.project_references(project["id"])
        supplied_reference_ids = set(shot_value.get("reference_ids") or [])
        if supplied_reference_ids:
            selected = [item for item in linked_references if item["id"] in supplied_reference_ids]
            if len(selected) != len(supplied_reference_ids) or (reference and str(reference) not in {str(Path(item["path"]).resolve()) for item in selected}):
                raise HTTPException(400, "Shot references must be linked to this project and match its selected image")
        elif reference:
            shot_value["reference_ids"] = [item["id"] for item in linked_references if Path(item["path"]).resolve() == reference.resolve()]
        generation_payload = shot_value.get("generation_payload") or {}
        if generation_payload:
            generation_payload = generation_payload | {"prompt": shot_value["prompt"], "shot_prompt": shot_value["prompt"],
                                                        "reference_image": str(reference) if reference else None,
                                                        "reference_id": (shot_value.get("reference_ids") or [None])[0],
                                                        "seed": shot_value.get("seed"), "preset": shot_value["preset"],
                                                        "width": shot_value["width"], "height": shot_value["height"],
                                                        "fps": shot_value["fps"], "output_location": shot_value["output_location"]}
            shot_value["generation_payload"] = generation_payload
        if shot_value["prompt_reviewed"] and shot_value["reviewed_style_revision"] != story.get("style_bible_revision", 0):
            raise HTTPException(409, "The style bible changed after this prompt was reviewed; review it again before saving")
        values.append(shot_value)
    try:
        return db.update_storyboard(storyboard_id, payload.revision, payload.title, values, payload.story_text.strip())
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
def queue_storyboard_shot(storyboard_id: str, shot_id: str, request: ShotGenerateRequest | None = None):
    request = request or ShotGenerateRequest()
    story = db.get_storyboard(storyboard_id)
    if not story:
        raise HTTPException(404, "Storyboard not found")
    shot = next((item for item in story["shots"] if item["id"] == shot_id), None)
    if not shot:
        raise HTTPException(404, "Shot not found")
    if shot.get("status") == "verified":
        raise HTTPException(409, "This shot already has a current verified clip. Edit and review its settings before creating a replacement.")
    if not shot.get("prompt_reviewed") or shot.get("reviewed_style_revision", 0) != story.get("style_bible_revision", 0):
        raise HTTPException(409, "Review and confirm the exact shot prompt and style context before queueing")
    if not detect_integrations(settings)["ltx"]["ready"]:
        raise HTTPException(409, "LTX runner or cached Q8 model weights are unavailable; no weights will be downloaded")
    project = _project(story["project_id"])
    defaults = db.get_setting(_generation_settings_key(story["project_id"])) or GenerationSettings().model_dump()
    style_context = _style_context(story.get("style_bible") or {})
    effective_prompt = (style_context + "\n\n" if style_context else "") + shot["prompt"]
    width = shot.get("width") or defaults.get("width", 448)
    height = shot.get("height") or defaults.get("height", 768)
    fps = shot.get("fps") or project.get("frame_rate") or _preset_frames(shot["preset"])[1]
    output_location = shot.get("output_location") or str(_project_output(project))
    reference = _validate_project_reference(project, shot.get("reference_image"))
    config = GenerationSettings.model_validate(defaults | {"prompt": effective_prompt, "preset": shot["preset"],
                         "width": width, "height": height, "output_location": output_location,
                         "seed": shot["seed"], "reference_image": str(reference) if reference else None})
    payload = _generation_payload(config, project["id"], fps_override=fps)
    shot_settings = {"style_context": style_context, "style_bible_revision": story.get("style_bible_revision", 0),
                     "width": width, "height": height, "fps": fps, "output_location": payload["output_location"],
                     "framing": shot.get("framing", ""), "aspect_ratio": project.get("aspect_ratio")}
    payload.update(shot_prompt=shot["prompt"], style_context=style_context, shot_settings=shot_settings)
    if request.use_experimental_multi_anchor:
        continuity = db.get_scene_continuity(story["project_id"])
        if request.continuity_revision is None or continuity.get("approved_revision") != request.continuity_revision or continuity.get("revision") != request.continuity_revision:
            raise HTTPException(409, "Approve the current scene continuity package before using experimental multi-image anchors")
        document = continuity.get("document") or {}
        scene = next((item for item in document.get("scenes", []) if item["title"].casefold() == (shot.get("scene_title") or "").casefold()), None)
        if not scene or not scene.get("anchor_reference_ids"):
            raise HTTPException(409, "This shot's scene has no explicitly approved image anchors")
        anchors = []
        frame_indices = scene["anchor_frame_indices"]
        for reference_id, frame_index in zip(scene["anchor_reference_ids"], frame_indices):
            if frame_index >= payload["frames"]:
                raise HTTPException(422, "A temporal anchor frame index must be inside the selected shot duration")
            item = _reference_details(story["project_id"], reference_id)
            if _is_historical_visual_reference(item) and document.get("historical_reference_approval_id") != item["id"]:
                raise HTTPException(400, "Historical anchor requires explicit approval")
            anchors.append({"path": item["path"], "frame_index": frame_index, "strength": 1.0, "reference_id": reference_id})
        payload["reference_anchors"] = anchors
        payload["continuity_revision"] = request.continuity_revision
        payload["conditioning_mode"] = "experimental-temporal-multi-image"
    try:
        return db.create_shot_job(storyboard_id, shot_id, payload)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/storyboards/{storyboard_id}/shots/{shot_id}/link-existing/{job_id}")
async def link_existing_storyboard_job(storyboard_id: str, shot_id: str, job_id: str):
    story = db.get_storyboard(storyboard_id)
    if not story:
        raise HTTPException(404, "Storyboard not found")
    shot = next((item for item in story["shots"] if item["id"] == shot_id), None)
    if not shot:
        raise HTTPException(404, "Shot not found")
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Generation job not found")
    if job["kind"] != "generation" or job["status"] != "completed":
        raise HTTPException(409, "Only completed generation jobs can be attached")
    job_snapshot, current_snapshot = db.generation_settings_snapshot(job["payload"]), db.generation_settings_snapshot(shot)
    if any(key in current_snapshot and current_snapshot[key] != value for key, value in job_snapshot.items()):
        raise HTTPException(409, "Existing output settings do not match this shot; copy the job settings into the shot and save first")
    recorded = (job.get("verification_metadata") or {}).get("verified_output")
    if not recorded or not job.get("result_path"):
        raise HTTPException(409, "This job has no ffprobe-verified output")
    payload = job["payload"]
    expected = {"width": payload.get("width"), "height": payload.get("height"),
                "fps": payload.get("fps"), "frames": payload.get("frames")}
    if any(value is None for value in expected.values()):
        raise HTTPException(409, "The job does not contain expected output dimensions and frame count")
    try:
        output = safe_local_path(job["result_path"], settings, must_exist=True)
        output_root = settings.expanded(settings.output_root)
        if not output.is_file() or output_root not in output.parents:
            raise ValueError("Existing output must be a file inside OUTPUT_ROOT")
        actual = await verify_video(str(output), settings, expected=expected)
        if actual.get("verified_output") != recorded:
            raise ValueError("Current ffprobe metadata differs from the job's recorded verification")
        return db.link_existing_shot_job(storyboard_id, shot_id, job_id)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


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
    prior_renders = [job for job in db.list_jobs(limit=500, project_id=story["project_id"])
                     if job.get("kind") == "render" and
                     (((job.get("payload") or {}).get("props") or {}).get("manifest") or {}).get("storyboard_id") == storyboard_id and
                     (((job.get("payload") or {}).get("props") or {}).get("manifest") or {}) == manifest]
    if any(job["status"] in ("queued", "running") for job in prior_renders):
        raise HTTPException(409, "A Remotion render for this composition is already queued or running")
    if any(job["status"] == "completed" and job.get("result_path") and Path(job["result_path"]).is_file() and
           (job.get("verification_metadata") or {}).get("verified_output") for job in prior_renders):
        raise HTTPException(409, "This composition already has a completed verified render")
    output_root = settings.expanded(settings.output_root)
    output_path = output_root / "storyboards" / storyboard_id / f"storyboard-r{story['revision']}.mp4"
    entrypoint = Path(__file__).resolve().parents[2] / "remotion" / "index.ts"
    payload = {"entrypoint": str(entrypoint), "composition_id": "StoryboardAssembly",
               "job_label": f"Remotion storyboard render · {story['title']}",
               "output_path": str(output_path), "public_dir": str(output_root),
               "props": {"manifest": manifest}, "width": manifest["width"], "height": manifest["height"],
               "fps": manifest["fps"], "expected_output": {"width": manifest["width"], "height": manifest["height"],
               "fps": manifest["fps"], "frames": manifest["duration_in_frames"]}}
    return db.create_job("render", payload, project_id=story["project_id"])


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
    project = _project(project_id) if project_id is not None else None
    key = _generation_settings_key(project_id)
    specific = db.get_setting(key)
    if specific is not None:
        value = specific
    else:
        value = (db.get_setting("generation") if project_id is None else None) or GenerationSettings().model_dump()
    if project and not value.get("output_location"):
        value = value | {"output_location": str(_project_output(project))}
    return value


@app.patch("/api/settings")
def update_settings(payload: GenerationSettings, project_id: str | None = None):
    if project_id is not None:
        _project(project_id)
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
    project_id = request.project_id or db.get_setting("active_project_id")
    if not project_id:
        raise HTTPException(409, "Select or create a project before queueing a generation")
    _project(project_id)
    status = detect_integrations(settings)
    if not status["ltx"]["ready"]:
        raise HTTPException(409, "LTX runner or cached Q8 model weights are unavailable. The app will not download model files.")
    payload = _generation_payload(request.settings, project_id)
    return db.create_job(request.kind, payload, project_id=project_id)


@app.post("/api/renders", status_code=201)
def create_render(request: RenderRequest):
    project_id = request.project_id or db.get_setting("active_project_id")
    if not project_id:
        raise HTTPException(409, "Select or create a project before queueing a render")
    _project(project_id)
    try:
        entrypoint = safe_local_path(request.entrypoint, settings, must_exist=True)
        output = safe_local_path(request.output_path, settings)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return db.create_job("render", request.model_dump() | {"entrypoint": str(entrypoint), "output_path": str(output)}, project_id=project_id)


@app.get("/api/jobs")
def list_jobs(project_id: str | None = None):
    selected = project_id or db.get_setting("active_project_id")
    if not selected:
        return []
    _project(selected)
    return db.list_jobs(project_id=selected)


def _scoped_job(job_id: str, project_id: str | None = None) -> dict:
    selected = project_id or db.get_setting("active_project_id")
    if not selected:
        raise HTTPException(404, "No active project")
    _project(selected)
    job = db.get_job(job_id)
    if not job or job.get("project_id") != selected:
        raise HTTPException(404, "Job not found in the selected project")
    return job


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, project_id: str | None = None):
    job = _scoped_job(job_id, project_id)
    return job | {"events": db.events(job_id)}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, project_id: str | None = None):
    job = _scoped_job(job_id, project_id)
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
def retry_job(job_id: str, project_id: str | None = None):
    job = _scoped_job(job_id, project_id)
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


@app.post("/api/outputs/reveal")
async def reveal_output(request: RevealOutputRequest):
    if sys.platform != "darwin":
        raise HTTPException(501, "Reveal in Finder is supported on macOS only")
    try:
        path = safe_local_path(request.path, settings, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.is_file() or path.suffix.lower() not in VIDEO_TYPES:
        raise HTTPException(400, "Choose an existing local video output")
    try:
        process = await asyncio.create_subprocess_exec(
            "open", "-R", str(path), stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
    except OSError as exc:
        raise HTTPException(500, f"Could not reveal this output in Finder: {exc}") from exc
    if process.returncode:
        detail = stderr.decode(errors="replace").strip() if stderr else "Finder returned an error"
        raise HTTPException(500, f"Could not reveal this output in Finder: {detail}")
    return {"revealed": True, "path": str(path)}
