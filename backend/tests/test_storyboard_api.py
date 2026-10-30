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
    story = api.import_storyboard(api.ImportStoryboardRequest(project_id="scorpion-frog"))
    assert story["project_id"] == "scorpion-frog"
    assert story["shots"][0]["reference_image"].endswith("riverbank-master.png")
    assert story["shots"][0]["prompt"] == "Saved project prompt"
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
    assert render_job["payload"]["composition_id"] == "StoryboardAssembly"
    assert render_job["payload"]["expected_output"] == {"width": 448, "height": 768, "fps": 24, "frames": 186}
    assert Path(shot["reference_image"]).read_bytes() == reference_before
