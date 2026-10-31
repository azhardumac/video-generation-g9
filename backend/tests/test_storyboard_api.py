import atexit
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
import pytest

from app.config import get_settings


# Import the application against an isolated throwaway database and project. These tests
# call route functions directly; the lifespan worker is never started.
_ROOT = Path(tempfile.mkdtemp(prefix="ai-video-studio-storyboard-api-"))
atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)
_PROJECT = _ROOT / "scorpion-frog"
(_PROJECT / "references" / "scenes").mkdir(parents=True)
(_PROJECT / "references" / "scenes" / "riverbank-master.png").write_bytes(b"local reference fixture")
_ENV = {
    "DATABASE_PATH": str(_ROOT / "studio.sqlite3"),
    "ALLOWED_PATH_ROOTS": str(_ROOT),
    "STORY_PROJECT": str(_PROJECT),
    "OUTPUT_ROOT": str(_ROOT / "outputs"),
    "LTX_ROOT": str(_ROOT / "ltx"),
    "LTX_EXECUTABLE": str(_ROOT / "ltx" / "ltx-2-mlx"),
    "HF_CACHE": str(_ROOT / "hf-cache"),
    "REMOTION_STUDIO": str(_ROOT / "remotion"),
    "REMOTION_EXECUTABLE": str(_ROOT / "remotion" / "node_modules" / ".bin" / "remotion"),
}
_environment = patch.dict(os.environ, _ENV)
_environment.start()
get_settings.cache_clear()
from app import main as api  # noqa: E402
_environment.stop()
get_settings.cache_clear()
def test_import_edit_queue_and_safe_delete_api_workflow(monkeypatch):
    reference_path = _PROJECT / "references" / "scenes" / "riverbank-master.png"
    project_settings = {"prompt": "Saved project prompt", "reference_image": str(reference_path),
                       "preset": "Extended", "seed": 7}
    api.db.put_setting("generation:scorpion-frog", project_settings)
    with pytest.raises(HTTPException, match="Create a blank storyboard"):
        api.import_storyboard(api.ImportStoryboardRequest(project_id="scorpion-frog"))
    assert api.db.list_storyboards("scorpion-frog") == []
    project = api.db.get_project("scorpion-frog")
    assert project is not None
    story = api.db.create_storyboard("scorpion-frog", "Scorpion & Frog", [{
        "scene_title": "Opening", "title": "First shot", "prompt": "A project-authored prompt",
        "reference_image": str(reference_path), "preset": "Extended", "seed": 7,
    }])
    assert story["project_id"] == "scorpion-frog"
    assert story["shots"][0]["reference_image"].endswith("riverbank-master.png")
    assert story["shots"][0]["prompt"] == "A project-authored prompt"
    assert story["shots"][0]["preset"] == "Extended"
    assert story["shots"][0]["seed"] == 7
    assert api.db.get_setting("generation:scorpion-frog") == project_settings
    reference_before = Path(story["shots"][0]["reference_image"]).read_bytes()
    assert reference_before == b"local reference fixture"

    first = story["shots"][0]
    edited = api.update_storyboard(story["id"], api.StoryboardUpdate(
        revision=story["revision"], title="Scorpion & Frog — River Crossing",
        shots=[first | {"scene_title": "The Riverbank", "title": "Opening wide"},
                  {"scene_title": "The River", "title": "The crossing", "prompt": "A frog rows the scorpion across the moonlit river.",
                   "reference_image": first["reference_image"], "preset": "Short", "seed": 7, "caption": "Across the water"}],
    ))
    assert edited["revision"] == 2
    assert [scene["title"] for scene in edited["scenes"]] == ["The Riverbank", "The River"]
    assert len(api.storyboard_revisions(story["id"])) == 2
    assert len(api.storyboard_scenes(story["id"])) == 2

    monkeypatch.setattr(api, "detect_integrations", lambda _: {"ltx": {"ready": True}, "remotion": {"ready": True}})
    shot = edited["shots"][0]
    queued_job = api.queue_storyboard_shot(story["id"], shot["id"])
    reloaded = api.get_storyboard(story["id"])
    assert reloaded["shots"][0]["job"]["id"] == queued_job["id"]
    assert reloaded["shots"][0]["status"] == "queued"
    with pytest.raises(HTTPException) as manifest_error:
        api.create_storyboard_manifest(story["id"])
    assert manifest_error.value.status_code == 409
    with pytest.raises(HTTPException) as delete_error:
        api.delete_storyboard_shot(story["id"], shot["id"], revision=2)
    assert delete_error.value.status_code == 409
    assert Path(shot["reference_image"]).read_bytes() == reference_before

    api.db.cancel_job(queued_job["id"])
    api.db.invalidate_storyboard_manifest(queued_job["id"])
    output_root = api.settings.expanded(api.settings.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    for item in edited["shots"]:
        job = api.queue_storyboard_shot(story["id"], item["id"])
        assert api.db.claim_job(job["id"])
        clip = output_root / f"{job['id']}.mp4"
        clip.write_bytes(b"test-only verified video fixture")
        frames = job["payload"]["frames"]
        assert api.db.complete_job(job["id"], progress=100, result_path=str(clip), exit_code=0,
                                   verification_metadata={"verified_output": {"width": 448, "height": 768, "fps": 24, "frames": frames}})

    assembled = api.create_storyboard_manifest(story["id"])
    assert assembled["manifest"]["duration_in_frames"] == 186
    assert assembled["manifest"]["shots"][1]["start_frame"] == 137
    assert Path(assembled["manifest_path"]).is_file()
    render_job = api.queue_storyboard_render(story["id"])
    assert render_job["kind"] == "render"
    assert render_job["payload"]["job_label"] == "Remotion storyboard render · Scorpion & Frog — River Crossing"
    assert render_job["payload"]["composition_id"] == "StoryboardAssembly"
    assert render_job["payload"]["expected_output"] == {"width": 448, "height": 768, "fps": 24, "frames": 186}
    assert Path(shot["reference_image"]).read_bytes() == reference_before


def test_link_existing_clip_requires_matching_settings_and_ffprobe_reverification(monkeypatch):
    reference = str(_PROJECT / "references" / "scenes" / "riverbank-master.png")
    story = api.db.create_storyboard("scorpion-frog", "Safe clip reuse", [{
        "scene_title": "Riverbank", "title": "Opening", "prompt": "Original generation prompt",
        "reference_image": reference, "preset": "Short", "seed": 7,
    }])
    shot = story["shots"][0]
    payload = {"prompt": "Original generation prompt", "reference_image": reference,
               "preset": "Short", "seed": 7, "width": 448, "height": 768, "fps": 24, "frames": 49}
    job = api.db.create_job("generation", payload, project_id="scorpion-frog")
    assert api.db.claim_job(job["id"])
    output = api.settings.expanded(api.settings.output_root) / "existing-safe-fixture.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"previously verified clip fixture")
    verified = {"width": 448, "height": 768, "fps": 24, "frames": 49}
    assert api.db.complete_job(job["id"], progress=100, result_path=str(output), exit_code=0,
                               verification_metadata={"verified_output": verified})
    calls = []
    async def fake_verify(path, _settings, expected=None):
        calls.append((path, expected))
        return {"verified_output": verified}
    monkeypatch.setattr(api, "verify_video", fake_verify)
    payload["prompt"] = "Edited settings must not match"
    mismatch = api.db.update_storyboard(story["id"], story["revision"], story["title"], [shot | {"prompt": payload["prompt"]}])
    with pytest.raises(HTTPException) as stale_error:
        import asyncio
        asyncio.run(api.link_existing_storyboard_job(story["id"], shot["id"], job["id"]))
    assert stale_error.value.status_code == 409
    assert "settings do not match" in stale_error.value.detail
    updated = api.db.update_storyboard(story["id"], mismatch["revision"], story["title"], [mismatch["shots"][0] | {"prompt": "Original generation prompt"}])
    linked = asyncio.run(api.link_existing_storyboard_job(story["id"], shot["id"], job["id"]))
    assert calls == [(str(output), {"width": 448, "height": 768, "fps": 24, "frames": 49})]
    assert linked["shots"][0]["job"]["settings_match"] is True
    assert api.db.get_job(job["id"])["result_path"] == str(output)
    assert output.read_bytes() == b"previously verified clip fixture"
    manifest = api.create_storyboard_manifest(story["id"])
    assert manifest["manifest"]["duration_in_frames"] == 49
    assert manifest["manifest"]["shots"][0]["transition_out_frames"] == 0


def test_fresh_project_switch_and_blank_storyboard_are_isolated():
    from uuid import uuid4
    project_root = _ROOT / f"fresh-production-{uuid4().hex[:8]}"
    project = api.create_project(api.ProjectCreateRequest(
        name="Scorpion & Frog Claymation", workspace_path=str(project_root),
        description="Handmade cinematic claymation", aspect_ratio="9:16",
        frame_rate=24, production_type="handmade cinematic claymation",
    ))
    assert project["workspace_kind"] == "created"
    assert (project_root / "references" / "character").is_dir()
    assert list((project_root / "references" / "character").iterdir()) == []
    assert api.set_active_project(api.ActiveProjectRequest(project_id=project["id"]))["project"]["id"] == project["id"]

    story = api.create_project_storyboard(project["id"], api.CreateStoryboardRequest(title="First Storyboard"))
    assert story["shots"] == []
    assert story["manifest"] is None
    assert api.list_project_storyboards(project["id"])[0]["id"] == story["id"]
    edited = api.update_storyboard(story["id"], api.StoryboardUpdate(
        revision=story["revision"], title=story["title"], shots=[
            {"scene_title":"Scene one","title":"First shot","prompt":"User reviewed prompt one","preset":"Short"},
            {"scene_title":"Scene two","title":"Second shot","prompt":"User reviewed prompt two","preset":"Standard"},
        ],
    ))
    assert [shot["title"] for shot in api.get_storyboard(story["id"])["shots"]] == ["First shot", "Second shot"]
    assert edited["revision"] == 2
    api.archive_storyboard(story["id"])
    with pytest.raises(HTTPException) as archived:
        api.get_storyboard(story["id"])
    assert archived.value.status_code == 404
    assert len(api.db.storyboard_revisions(story["id"])) == 2
    with pytest.raises(HTTPException) as bad_scope:
        api.list_storyboards("not-a-real-project")
    assert bad_scope.value.status_code == 404
    assert api.db.project_references(project["id"]) == []
    assert api.db.project_assets(project["id"]) == []
    assert api.read_settings(project["id"])["prompt"] == ""
    api.update_settings(api.GenerationSettings(prompt="Project-authored claymation prompt"), project["id"])
    second = api.create_project(api.ProjectCreateRequest(
        name="Settings isolation", workspace_path=str(_ROOT / f"settings-{uuid4().hex[:8]}"),
        aspect_ratio="16:9", frame_rate=24, production_type="test production",
    ))
    assert api.read_settings(second["id"])["prompt"] == ""
    assert api.read_settings(project["id"])["prompt"] == "Project-authored claymation prompt"
    assert api.set_active_project(api.ActiveProjectRequest(project_id=None))["project"] is None


def test_project_reference_validation_scoping_and_unlink_preserve_source():
    from uuid import uuid4
    project_root = _ROOT / f"refs-production-{uuid4().hex[:8]}"
    project = api.create_project(api.ProjectCreateRequest(
        name="Reference test", workspace_path=str(project_root),
        aspect_ratio="9:16", frame_rate=24, production_type="test production",
    ))
    image_path = _ROOT / f"approved-character-{uuid4().hex[:8]}.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test image bytes")
    payload = {"path": str(image_path), "name": "Frog original", "category": "character", "notes": "Approved source"}
    reference = api.add_existing_project_reference(project["id"], api.ProjectReferenceCreate(**payload))
    assert reference["source_kind"] == "linked"
    with pytest.raises(HTTPException) as duplicate:
        api.add_existing_project_reference(project["id"], api.ProjectReferenceCreate(**payload))
    assert duplicate.value.status_code == 409
    assert Path(api.project_reference_file(project["id"], reference["id"]).path).read_bytes() == image_path.read_bytes()

    second_root = _ROOT / f"other-{uuid4().hex[:8]}"
    second = api.create_project(api.ProjectCreateRequest(
        name="Other project", workspace_path=str(second_root),
        aspect_ratio="16:9", frame_rate=24, production_type="test production",
    ))
    assert api.project_references(second["id"]) == []
    assert api.remove_project_reference(project["id"], reference["id"]) is None
    assert image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert api.project_references(project["id"]) == []

    with pytest.raises(HTTPException) as invalid:
        api.add_existing_project_reference(project["id"], api.ProjectReferenceCreate(**(payload | {"path": str(_PROJECT / "references/scenes/riverbank-master.png")})))
    assert invalid.value.status_code == 400


def test_project_reference_upload_replace_keeps_previous_file():
    import asyncio
    from uuid import uuid4
    from starlette.requests import Request
    project_root = _ROOT / f"upload-production-{uuid4().hex[:8]}"
    project = api.create_project(api.ProjectCreateRequest(
        name="Upload test", workspace_path=str(project_root), aspect_ratio="9:16",
        frame_rate=24, production_type="test production",
    ))
    async def upload(content:bytes, filename:str, replace_id:str|None=None):
        async def receive():
            return {"type":"http.request","body":content,"more_body":False}
        request=Request({"type":"http","method":"PUT","headers":[
            (b"x-file-name",filename.encode()),(b"content-type",b"image/png")
        ]},receive=receive)
        return await api.upload_project_reference(project["id"],request,name="Frog approved",category="character",notes="selected by user",replace_id=replace_id)
    first_bytes=b"\x89PNG\r\n\x1a\nfirst image bytes"
    first=asyncio.run(upload(first_bytes,"frog.png"))
    first_path=Path(first["path"])
    assert first["source_kind"]=="uploaded" and first_path.read_bytes()==first_bytes
    replacement_bytes=b"\x89PNG\r\n\x1a\nreplacement image bytes"
    replacement=asyncio.run(upload(replacement_bytes,"frog-revised.png",first["id"]))
    assert replacement["id"]==first["id"]
    assert Path(replacement["path"]).read_bytes()==replacement_bytes
    assert first_path.read_bytes()==first_bytes


def test_existing_project_import_is_read_only_and_idempotent():
    imported = api.import_existing_project(api.ProjectImportRequest(workspace_path=str(_PROJECT),name="Imported Story Project"))
    source = _PROJECT / "references" / "scenes" / "riverbank-master.png"
    source_before = source.read_bytes()
    asset_ids = [asset["id"] for asset in api.db.project_assets(imported["id"])]
    imported_again = api.import_existing_project(api.ProjectImportRequest(workspace_path=str(_PROJECT),name="Different Name"))
    assert imported_again["id"] == imported["id"]
    assert [asset["id"] for asset in api.db.project_assets(imported["id"])] == asset_ids
    assert source.read_bytes() == source_before
