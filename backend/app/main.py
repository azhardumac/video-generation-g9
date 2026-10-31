from contextlib import asynccontextmanager
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import sqlite3
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


def _project(project_id: str, *, allow_archived: bool = False) -> dict:
    project = db.get_project(project_id)
    if not project or (project.get("archived_at") and not allow_archived):
        raise HTTPException(404, "Project not found or archived")
    return project


def _project_id_for_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:40] or "project"
    return f"{slug}-{uuid4().hex}"


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
    return db.create_storyboard(project_id, title, [])


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
    values = []
    for shot in payload.shots:
        _preset_frames(shot.preset)
        shot_value = shot.model_dump()
        if shot.reference_image:
            try:
                shot_value["reference_image"] = str(safe_local_path(shot.reference_image, settings, must_exist=True))
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(400, str(exc)) from exc
        values.append(shot_value)
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
    if db.generation_settings_snapshot(job["payload"]) != db.generation_settings_snapshot(shot):
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
    if project_id is not None:
        _project(project_id)
    key = _generation_settings_key(project_id)
    specific = db.get_setting(key)
    if specific is not None:
        return specific
    value = (db.get_setting("generation") if project_id is None else None) or GenerationSettings().model_dump()
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
    payload = _generation_payload(request.settings)
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
