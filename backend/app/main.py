import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
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
from pydantic import BaseModel, Field

from .adapters import verify_video
from .config import get_settings
from .db import Database
from .integrations import detect_integrations
from .film_planner import UnsupportedStory, make_approved_fable_plan, validate_approved_fable
from .paths import safe_local_path
from .presets import BUILTIN_PRESETS, GenerationSettings, Preset
from .projects import IMAGE_TYPES, VIDEO_TYPES, scan_project
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
    return {"storyboard_id": story["id"], "revision": story["revision"],
            "style_bible_revision": story["style_bible_revision"], "approved": bool(story.get("film_plan", {}).get("approved")),
            "total_shots": len(shots), "verified_shots": len(current_verified), "missing_shots": missing,
            "all_verified": all_verified, "reconciled": reconciled, "manifest_ready": manifest_ready,
            "manifest_path": story.get("manifest_path"), "manifest": manifest,
            "remotion_ready": detect_integrations(settings)["remotion"]["ready"],
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
        if len(shot.get("reference_ids") or []) != 1:
            raise HTTPException(409, f"Shot '{shot['title']}' must have exactly one linked character reference for LTX conditioning")
        reference = _validate_project_reference(project, payload.get("reference_image"))
        if reference is None:
            raise HTTPException(409, f"Shot '{shot['title']}' is missing its selected character image")
        matches = [item for item in db.project_references(project["id"]) if item["id"] == shot["reference_ids"][0]
                   and Path(item["path"]).resolve() == reference.resolve()]
        if not matches:
            raise HTTPException(409, f"Shot '{shot['title']}' reference is no longer linked to this project")
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
def queue_storyboard_shot(storyboard_id: str, shot_id: str):
    story = db.get_storyboard(storyboard_id)
    if not story:
        raise HTTPException(404, "Storyboard not found")
    shot = next((item for item in story["shots"] if item["id"] == shot_id), None)
    if not shot:
        raise HTTPException(404, "Shot not found")
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
