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
        shots=[first | {"scene_title": "The Riverbank", "title": "Opening wide", "prompt_reviewed": True},
                  {"scene_title": "The River", "title": "The crossing", "prompt": "A frog rows the scorpion across the moonlit river.",
                   "reference_image": first["reference_image"], "preset": "Short", "seed": 7, "caption": "Across the water",
                   "prompt_reviewed": True, "reviewed_style_revision": story.get("style_bible_revision", 0)}],
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


def test_fresh_scorpion_frog_production_uses_only_explicit_character_selections():
    from uuid import uuid4
    source = _ROOT / f"approved-originals-{uuid4().hex[:8]}"
    source.mkdir()
    frog = source / "frog-character-original.png"
    scorpion = source / "scorpion-character-original.png"
    riverbank = source / "riverbank-master.png"
    prior_video = source / "benchmark-output.mp4"
    for index, image in enumerate((frog, scorpion, riverbank)):
        image.write_bytes(b"\x89PNG\r\n\x1a\noriginal image fixture " + bytes([index]))
    prior_video.write_bytes(b"legacy video fixture")

    project_root = _ROOT / f"claymation-production-{uuid4().hex[:8]}"
    project = api.create_project(api.ProjectCreateRequest(
        name="Scorpion & Frog · Handmade Claymation",
        workspace_path=str(project_root), description="Handmade cinematic claymation",
        aspect_ratio="9:16", frame_rate=24,
        production_type="handmade cinematic claymation",
    ))
    assert api.project_references(project["id"]) == []
    assert api.db.project_assets(project["id"]) == []
    assert api.db.list_jobs(project_id=project["id"]) == []
    assert api.list_project_storyboards(project["id"]) == []

    for name, path in (("Original frog", frog), ("Original scorpion", scorpion)):
        api.add_existing_project_reference(project["id"], api.ProjectReferenceCreate(
            path=str(path), name=name, category="character", notes="Approved original",
        ))
    assert {Path(reference["path"]).resolve() for reference in api.project_references(project["id"])} == {frog.resolve(), scorpion.resolve()}
    assert api.project_references(project["id"])[0]["category"] == "character"

    storyboard = api.create_project_storyboard(project["id"], api.CreateStoryboardRequest(
        title="Claymation production storyboard",
    ))
    assert storyboard["shots"] == []
    assert storyboard["story_text"] == ""
    assert storyboard["manifest"] is None
    assert api.db.list_jobs(project_id=project["id"]) == []
    assert riverbank.read_bytes().startswith(b"\x89PNG")
    assert prior_video.read_bytes() == b"legacy video fixture"


def test_story_to_film_builds_persistent_approved_fable_plan_without_queueing_or_reusing_media():
    from uuid import uuid4
    source = _ROOT / f"film-plan-inputs-{uuid4().hex[:8]}"
    source.mkdir()
    frog = source / "frog-original.png"
    scorpion = source / "scorpion-original.png"
    unused_riverbank = source / "riverbank-master.png"
    old_video = source / "benchmark.mp4"
    for index, image in enumerate((frog, scorpion, unused_riverbank)):
        image.write_bytes(b"\x89PNG\r\n\x1a\nreference fixture " + bytes([index]))
    old_video.write_bytes(b"historical output fixture")
    project = api.create_project(api.ProjectCreateRequest(
        name=f"Story-to-Film {uuid4().hex[:6]}", workspace_path=str(_ROOT / f"film-workspace-{uuid4().hex[:8]}"),
        description="Handmade cinematic claymation", aspect_ratio="9:16", frame_rate=24,
        production_type="handmade cinematic claymation",
    ))
    api.update_project_style_bible(project["id"], api.StyleBibleRequest(
        material="Handmade cinematic claymation with tactile sculpted clay and miniature sets.",
        silhouettes="Preserve the original frog and scorpion silhouettes.",
        proportions="Consistent expressive clay characters.", composition="Cinematic close and wide framing.",
        negative_constraints="Do not use riverbank or water imagery, riverbank-master.png, any prior video, or benchmark media.",
    ))
    frog_ref = api.add_existing_project_reference(project["id"], api.ProjectReferenceCreate(
        path=str(frog), name="Original frog character", category="character"))
    scorpion_ref = api.add_existing_project_reference(project["id"], api.ProjectReferenceCreate(
        path=str(scorpion), name="Original scorpion character", category="character"))
    woodland = api.db.create_storyboard(project["id"], "Existing woodland draft", [{
        "scene_title": "Woodland path", "title": "Existing draft shot", "prompt": "Preserved woodland path draft.",
    }], "A different woodland path story.")
    api.db.put_setting(f"generation:{project['id']}", {"model": "dgrauet/ltx-2.3-mlx-q8", "seed": 81,
                                                        "width": 448, "height": 768, "distilled_two_stage": True,
                                                        "low_ram": True, "audio": False, "preset": "Standard"})
    isolated_project = api.create_project(api.ProjectCreateRequest(
        name=f"Reference isolation {uuid4().hex[:6]}", workspace_path=str(_ROOT / f"isolated-{uuid4().hex[:8]}"),
        aspect_ratio="9:16", frame_rate=24, production_type="handmade cinematic claymation",
    ))
    with pytest.raises(HTTPException) as cross_project_reference:
        api.build_film_plan(isolated_project["id"], api.BuildFilmPlanRequest(
            story_text="A frog searches the river for a crossing. A scorpion asks the frog for a ride. The frog fears being stung. The scorpion says both would die if he stung the frog, so the frog agrees. They cross the river. Halfway across the scorpion stings the frog. The frog asks why; the scorpion says it is his nature. Both face a tragic end.",
            frog_reference_id=frog_ref["id"], scorpion_reference_id=scorpion_ref["id"],
        ))
    assert cross_project_reference.value.status_code == 400
    assert api.list_project_storyboards(isolated_project["id"]) == []
    source_story = ("A frog searches for a crossing at a river. A scorpion requests a ride. The frog fears being stung. "
                    "The scorpion reasons that both would die if he stung the frog, so the frog agrees. They cross the river. "
                    "Halfway across the scorpion stings the frog. The frog asks why. The scorpion answers that it is in his nature. "
                    "Both face a tragic end.")
    result = api.build_film_plan(project["id"], api.BuildFilmPlanRequest(
        story_text=source_story, frog_reference_id=frog_ref["id"], scorpion_reference_id=scorpion_ref["id"],
        target_format="9:16", preset="Short", seed=81,
    ))
    assert result["id"] != woodland["id"]
    assert result["story_text"] == source_story
    assert result["revision"] == 1
    assert [scene["title"] for scene in result["scenes"]] == [item["title"] for item in result["film_plan"]["metadata"]["scenes"]]
    assert len(result["scenes"]) == 6
    assert len(result["shots"]) == 12
    assert result["film_plan"]["metadata"]["shot_count"] == 12
    assert result["film_plan"]["metadata"]["estimated_seconds"] == 24.5
    assert "no planning model is configured" in result["film_plan"]["metadata"]["planner_disclosure"].lower()
    assert result["film_plan"]["metadata"]["approved_reference_ids"] == [frog_ref["id"], scorpion_ref["id"]]
    assert result["shots"][0]["characters"] == ["frog"]
    assert result["shots"][7]["characters"] == ["frog", "scorpion"]
    assert result["shots"][-1]["characters"] == ["frog", "scorpion"]
    assert "style-bible revision 2" in result["film_plan"]["metadata"]["style_bible_notice"]
    assert "Do not use riverbank or water imagery" not in result["style_bible"]["negative_constraints"]
    assert "riverbank-master.png" in result["style_bible"]["negative_constraints"]
    with api.db.connect() as local_db:
        prior_style = local_db.execute("SELECT style_bible_json FROM project_style_bible_revisions WHERE project_id=? AND revision=1", (project["id"],)).fetchone()
    assert "Do not use riverbank or water imagery" in prior_style["style_bible_json"]
    for shot in result["shots"]:
        assert len(shot["reference_ids"]) == 1
        assert shot["reference_image"] in {str(frog.resolve()), str(scorpion.resolve())}
        payload = shot["generation_payload"]
        assert payload["reference_id"] == shot["reference_ids"][0]
        assert payload["reference_conditioning"] == "one-image"
        assert (payload["width"], payload["height"], payload["fps"], payload["frames"]) == (448, 768, 24, 49)
        assert payload["seed"] == 81 and payload["audio"] is False
        assert "Do not reuse prior video, riverbank-master.png, benchmark media" in payload["prompt"]
        assert "Project style bible:" in payload["prompt"]
    assert not result["film_plan"]["approved"]
    assert api.db.list_jobs(project_id=project["id"]) == []
    assert api.db.get_storyboard(woodland["id"])["shots"][0]["prompt"] == "Preserved woodland path draft."
    assert woodland["story_text"] == "A different woodland path story."
    assert frog.read_bytes().startswith(b"\x89PNG") and scorpion.read_bytes().startswith(b"\x89PNG")
    assert unused_riverbank.read_bytes().startswith(b"\x89PNG") and old_video.read_bytes() == b"historical output fixture"

    approved = api.approve_film_plan(result["id"], api.ApproveFilmPlanRequest(
        revision=result["revision"], style_bible_revision=result["style_bible_revision"]))
    assert approved["film_plan"]["approved"]
    assert approved["film_plan"]["approved_revision"] == approved["revision"]
    assert all(shot["prompt_reviewed"] for shot in approved["shots"])
    changed_shots = [approved["shots"][0] | {"prompt": approved["shots"][0]["prompt"] + " Revised framing."}, *approved["shots"][1:]]
    changed = api.update_storyboard(approved["id"], api.StoryboardUpdate(
        revision=approved["revision"], title=approved["title"], story_text=approved["story_text"], shots=changed_shots))
    assert changed["revision"] == approved["revision"] + 1
    assert not changed["film_plan"]["approved"]
    with pytest.raises(HTTPException) as unsupported:
        api.build_film_plan(project["id"], api.BuildFilmPlanRequest(
            story_text="A woodland path and a fallen twig change the animals' plans forever, as two travelers decide to take a different route through the quiet forest.",
            frog_reference_id=frog_ref["id"], scorpion_reference_id=scorpion_ref["id"],
        ))
    assert unsupported.value.status_code == 422
    assert Path(api.db.get_project_reference(project["id"], frog_ref["id"])["path"]).resolve() == frog.resolve()


def test_approved_film_batch_is_sequential_pausable_cancellable_retryable_and_assembles_only_verified_outputs(monkeypatch):
    import asyncio
    from uuid import uuid4
    root = _ROOT / f"batch-inputs-{uuid4().hex[:8]}"
    root.mkdir()
    frog, scorpion = root / "frog.png", root / "scorpion.png"
    frog.write_bytes(b"\x89PNG\r\n\x1a\nfrog fixture")
    scorpion.write_bytes(b"\x89PNG\r\n\x1a\nscorpion fixture")
    project = api.create_project(api.ProjectCreateRequest(
        name=f"Film batch {uuid4().hex[:6]}", workspace_path=str(_ROOT / f"batch-project-{uuid4().hex[:8]}"),
        aspect_ratio="9:16", frame_rate=24, production_type="handmade cinematic claymation",
    ))
    frog_ref = api.add_existing_project_reference(project["id"], api.ProjectReferenceCreate(path=str(frog), name="Original frog", category="character"))
    scorpion_ref = api.add_existing_project_reference(project["id"], api.ProjectReferenceCreate(path=str(scorpion), name="Original scorpion", category="character"))
    source_story = ("A frog searches the river for a crossing. A scorpion requests a ride. The frog fears being stung. "
                    "The scorpion reasons they would both die if he stung the frog, so the frog agrees. They cross the river together. "
                    "Halfway across, the scorpion stings the frog. The frog asks why. The scorpion says it is in his nature. Both face a tragic end.")
    plan = api.build_film_plan(project["id"], api.BuildFilmPlanRequest(
        story_text=source_story, frog_reference_id=frog_ref["id"], scorpion_reference_id=scorpion_ref["id"], preset="Short"))
    api.approve_film_plan(plan["id"], api.ApproveFilmPlanRequest(revision=plan["revision"], style_bible_revision=plan["style_bible_revision"]))
    monkeypatch.setattr(api, "detect_integrations", lambda _: {"ltx": {"ready": True}, "remotion": {"ready": True}})
    batch = api.generate_film(plan["id"], api.GenerateFilmRequest(revision=plan["revision"], style_bible_revision=plan["style_bible_revision"]))
    assert batch["status"] == "running" and batch["queued_shots"] == 12 and batch["progress"] == 0
    initial_job_ids = [item["job"]["id"] for item in batch["shots"]]
    assert len(set(initial_job_ids)) == 12
    assert all(api.db.get_job(job_id)["payload"]["film_batch_id"] == batch["id"] for job_id in initial_job_ids)

    paused = api.pause_film_batch(batch["id"])
    assert paused["status"] == "paused"
    next_job = api.db.next_queued_job()
    assert not next_job or next_job["id"] not in set(initial_job_ids)
    resumed = api.resume_film_batch(batch["id"])
    assert resumed["status"] == "running"

    # Complete the first shot with a test-only file and ffprobe metadata; no adapter runs.
    first_job = api.db.get_job(initial_job_ids[0])
    assert api.db.claim_job(first_job["id"])
    output_dir = api.settings.expanded(api.settings.output_root) / project["id"]
    output_dir.mkdir(parents=True, exist_ok=True)
    first_output = output_dir / f"{first_job['id']}.test-fixture.mp4"
    first_output.write_bytes(b"test-only verified fixture")
    assert api.db.complete_job(first_job["id"], progress=100, result_path=str(first_output), exit_code=0,
                               verification_metadata={"verified_output": {"width": 448, "height": 768, "fps": 24, "frames": 49}})

    cancelled = asyncio.run(api.cancel_film_batch(batch["id"]))
    assert cancelled["status"] == "cancelled"
    assert api.db.get_job(first_job["id"])["status"] == "completed"
    assert sum(api.db.get_job(job_id)["status"] == "cancelled" for job_id in initial_job_ids) == 11
    cancelled_shot_ids = [item["shot_id"] for item in cancelled["shots"] if item["status"] == "cancelled"]
    retried = api.retry_film_batch(batch["id"], api.RetryFilmRequest(shot_ids=cancelled_shot_ids))
    assert retried["status"] == "running" and retried["queued_shots"] == 11
    assert next(item for item in retried["shots"] if item["shot_id"] == batch["shots"][0]["shot_id"])["status"] == "completed"
    retry_job_ids = [item["job"]["id"] for item in retried["shots"] if item["status"] == "queued"]
    assert len(retry_job_ids) == 11 and not (set(retry_job_ids) & set(initial_job_ids))

    # Mark remaining queue items as verified test fixtures, then exercise the real manifest builder.
    final_job = None
    for job_id in retry_job_ids:
        job = api.db.get_job(job_id)
        assert api.db.claim_job(job_id)
        output = output_dir / f"{job_id}.test-fixture.mp4"
        output.write_bytes(b"test-only verified fixture")
        assert api.db.complete_job(job_id, progress=100, result_path=str(output), exit_code=0,
                                   verification_metadata={"verified_output": {"width": 448, "height": 768, "fps": 24, "frames": 49}})
        final_job = api.db.get_job(job_id)
    api.worker._assemble_completed_film(final_job)
    complete = api.get_film_batch(batch["id"])
    assert complete["status"] == "completed" and complete["completed_shots"] == 12 and complete["progress"] == 100
    storyboard = api.get_storyboard(plan["id"])
    assert storyboard["manifest"] is not None
    assert storyboard["manifest"]["duration_in_frames"] == 548
    assert len(storyboard["manifest"]["shots"]) == 12
    assert len(api.db.list_jobs(project_id=project["id"])) == 23
    assert api.db.list_jobs(project_id=project["id"])[0]["kind"] == "generation"
    assert first_output.read_bytes() == b"test-only verified fixture"
    repeated = api.generate_film(plan["id"], api.GenerateFilmRequest(
        revision=plan["revision"], style_bible_revision=plan["style_bible_revision"]))
    assert repeated["status"] == "completed" and repeated["queued_shots"] == 0
    assert len(api.db.list_jobs(project_id=project["id"])) == 23
    assert all(item["status"] == "completed" and item["job"] for item in repeated["shots"])


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


def test_reviewed_shot_queues_exact_project_scoped_input_without_running_it(monkeypatch):
    from uuid import uuid4
    from app.adapters import build_ltx_command
    root = _ROOT / f"reviewed-shot-{uuid4().hex[:8]}"
    project = api.create_project(api.ProjectCreateRequest(
        name="Claymation test", workspace_path=str(root), aspect_ratio="9:16",
        frame_rate=24, production_type="handmade cinematic claymation",
    ))
    image = root / "approved-frog.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\noriginal frog character reference")
    api.add_existing_project_reference(project["id"], api.ProjectReferenceCreate(
        path=str(image), name="Original frog", category="character",
    ))
    style = api.update_project_style_bible(project["id"], api.StyleBibleRequest(
        material="matte handmade clay", silhouettes="rounded frog silhouette",
        proportions="keep character proportions stable", composition="medium close-up",
        negative_constraints="no glossy plastic",
    ))
    story = api.create_project_storyboard(project["id"], api.CreateStoryboardRequest(
        title="Opening", story_text="A story written by the user.",
    ))
    edited = api.update_storyboard(story["id"], api.StoryboardUpdate(
        revision=story["revision"], title=story["title"], story_text=story["story_text"], shots=[{
            "scene_title":"River crossing", "title":"Frog close-up", "prompt":"The frog studies a handmade clay map.",
            "reference_image":str(image), "preset":"Short", "seed":314,
            "width":448, "height":768, "fps":24, "output_location":None,
            "framing":"medium close-up", "prompt_reviewed":True, "reviewed_style_revision":style["revision"],
        }],
    ))
    monkeypatch.setattr(api, "detect_integrations", lambda _: {"ltx":{"ready":True}})
    shot=edited["shots"][0]
    queued=api.queue_storyboard_shot(story["id"],shot["id"])
    assert queued["status"]=="queued"
    assert queued["project_id"]==project["id"]
    payload=queued["payload"]
    assert payload["shot_prompt"]=="The frog studies a handmade clay map."
    assert payload["style_context"].startswith("Material and finish: matte handmade clay")
    assert payload["prompt"].endswith(payload["shot_prompt"])
    assert payload["width"]==448 and payload["height"]==768 and payload["fps"]==24 and payload["frames"]==49
    assert payload["output_location"]==str(api._project_output(project).resolve())
    assert payload["reference_image"]==str(image.resolve())
    assert payload["shot_settings"]["framing"]=="medium close-up"
    command=build_ltx_command("local-ltx",payload,str(root/"exports"/"fixture.partial.mp4"),payload["reference_image"])
    assert command[command.index("--image"):command.index("--image")+4]==["--image",str(image.resolve()),"0","1.0"]
    assert api.db.get_job(queued["id"])["status"]=="queued"  # Command construction only; no subprocess starts.

    api.update_project_style_bible(project["id"],api.StyleBibleRequest(
        material="new material guidance",silhouettes="rounded frog silhouette",
        proportions="keep character proportions stable",composition="medium close-up",
        negative_constraints="no glossy plastic",
    ))
    stale=api.get_storyboard(story["id"])["shots"][0]
    assert stale["status"]=="stale"
    with pytest.raises(HTTPException) as review_error:
        api.queue_storyboard_shot(story["id"],shot["id"])
    assert review_error.value.status_code==409


def test_reveal_output_uses_argument_vector_without_shell(monkeypatch):
    import asyncio
    output=api.settings.expanded(api.settings.output_root)/"clip with spaces.mp4"
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_bytes(b"preview fixture")
    calls=[]
    class Process:
        returncode=0
        async def communicate(self):return b"",b""
    async def fake_exec(*args,**kwargs):
        calls.append((args,kwargs));return Process()
    monkeypatch.setattr(api.sys,"platform","darwin")
    monkeypatch.setattr(api.asyncio,"create_subprocess_exec",fake_exec)
    result=asyncio.run(api.reveal_output(api.RevealOutputRequest(path=str(output))))
    assert result["revealed"] is True
    assert calls==[(("open","-R",str(output.resolve())),{"stdout":asyncio.subprocess.DEVNULL,"stderr":asyncio.subprocess.PIPE})]


def test_reveal_output_rejects_non_macos_and_non_video_paths(monkeypatch):
    import asyncio
    monkeypatch.setattr(api.sys,"platform","linux")
    with pytest.raises(HTTPException) as unsupported:
        asyncio.run(api.reveal_output(api.RevealOutputRequest(path=str(_PROJECT))))
    assert unsupported.value.status_code==501
    monkeypatch.setattr(api.sys,"platform","darwin")
    with pytest.raises(HTTPException) as nonvideo:
        asyncio.run(api.reveal_output(api.RevealOutputRequest(path=str(_PROJECT))))
    assert nonvideo.value.status_code==400
