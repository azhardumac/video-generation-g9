import asyncio
import json
import socket
from pathlib import Path

import pytest

from app.adapters import AdapterError, RemotionAdapter, select_remotion_render_port
from app.config import Settings
from app.db import Database
from app.storyboards import build_composition_manifest


def _db(tmp_path: Path) -> Database:
    settings = Settings(_env_file=None, database_path=str(tmp_path / "studio.sqlite3"),
                        allowed_path_roots=str(tmp_path), output_root=str(tmp_path / "outputs"))
    return Database(settings)


def _shot(title="Riverbank opening", reference=None):
    return {"scene_title": "Moonlit riverbank", "title": title, "prompt": "Moonlit riverbank with the frog and scorpion.",
            "reference_image": reference, "preset": "Short", "seed": 42, "caption": "A quiet bargain"}


def test_additive_migration_preserves_existing_jobs_project_assets_and_revisions(tmp_path):
    db = _db(tmp_path)
    old_job = db.create_job("generation", {"prompt": "existing history"})
    db.import_project("scorpion-frog", "Scorpion & Frog", str(tmp_path), [
        {"id": "asset-1", "category": "scene_reference", "relative_path": "river.png",
         "size_bytes": 12, "modified_at": 1.0, "mime_type": "image/png"},
    ])
    story = db.create_storyboard("scorpion-frog", "Scorpion & Frog", [_shot()])

    restarted = _db(tmp_path)
    assert restarted.get_job(old_job["id"])["payload"]["prompt"] == "existing history"
    assert restarted.get_project_asset("scorpion-frog", "asset-1")["relative_path"] == "river.png"
    loaded = restarted.get_storyboard(story["id"])
    saved = restarted.update_storyboard(story["id"], loaded["revision"], loaded["title"], [
        loaded["shots"][0] | {"title": "Revised riverbank", "prompt": "New shot prompt"},
        _shot("Frog close-up"),
    ])
    assert saved["revision"] == 2
    assert [shot["title"] for shot in saved["shots"]] == ["Revised riverbank", "Frog close-up"]
    assert len(restarted.storyboard_revisions(story["id"])) == 2
    assert restarted.storyboard_revisions(story["id"])[0]["snapshot"]["shots"][0]["title"] == "Riverbank opening"


def test_shot_jobs_are_linked_to_single_worker_and_history_survives_edit_delete(tmp_path):
    db = _db(tmp_path)
    story = db.create_storyboard("scorpion-frog", "Scorpion & Frog", [_shot()])
    shot = story["shots"][0]
    job = db.create_shot_job(story["id"], shot["id"], {"prompt": shot["prompt"], "preset": "Short"})
    with pytest.raises(RuntimeError, match="already has a queued or running"):
        db.create_shot_job(story["id"], shot["id"], {"prompt": shot["prompt"], "preset": "Short"})

    edited = db.update_storyboard(story["id"], story["revision"], story["title"], [
        shot | {"title": "Edited while queued"},
    ])
    assert edited["shots"][0]["job"]["id"] == job["id"]
    with pytest.raises(RuntimeError, match="Cancel active shot jobs"):
        db.delete_storyboard_shot(story["id"], shot["id"], edited["revision"])

    assert db.cancel_job(job["id"])["status"] == "cancelled"
    archived = db.delete_storyboard_shot(story["id"], shot["id"], edited["revision"])
    assert archived["shots"] == []
    assert db.get_job(job["id"])["status"] == "cancelled"
    assert len(db.storyboard_revisions(story["id"])) == 3
    db.archive_storyboard(story["id"])
    assert db.get_storyboard(story["id"]) is None
    assert len(db.storyboard_revisions(story["id"])) == 3
    assert db.get_job(job["id"])["id"] == job["id"]


def test_queued_job_finishing_after_settings_edit_is_historical_and_manifest_rejects_it(tmp_path):
    db = _db(tmp_path)
    story = db.create_storyboard("scorpion-frog", "Scorpion & Frog", [_shot(reference=str(tmp_path / "river.png"))])
    shot = story["shots"][0]
    payload = {"prompt": shot["prompt"], "reference_image": shot["reference_image"], "seed": shot["seed"],
               "preset": shot["preset"], "width": 448, "height": 768, "fps": 24, "frames": 49}
    job = db.create_shot_job(story["id"], shot["id"], payload)
    assert db.claim_job(job["id"])
    changed = db.update_storyboard(story["id"], story["revision"], story["title"], [shot | {"prompt": "Edited while LTX was running"}])
    clip = tmp_path / "outputs" / "old-clip.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"preserved historical clip")
    assert db.complete_job(job["id"], progress=100, result_path=str(clip), exit_code=0,
                           verification_metadata={"verified_output": {"width": 448, "height": 768, "fps": 24, "frames": 49}})
    loaded = db.get_storyboard(story["id"])
    assert loaded["shots"][0]["job"]["status"] == "completed"
    assert loaded["shots"][0]["job"]["settings_match"] is False
    assert loaded["shots"][0]["job"]["result_path"] == str(clip)
    assert clip.read_bytes() == b"preserved historical clip"
    with pytest.raises(ValueError, match="output is stale"):
        build_composition_manifest(loaded, tmp_path / "outputs")


def test_manifest_single_verified_clip_has_no_transition_and_keeps_job_snapshot(tmp_path):
    root = tmp_path / "outputs"
    root.mkdir()
    clip = root / "one-shot.mp4"
    clip.write_bytes(b"verified fixture")
    settings_snapshot = {"prompt": "A river at night", "reference_image": None, "seed": 7, "preset": "Short"}
    story = {"id": "one-shot", "revision": 1, "title": "One Shot", "shots": [{
        "id": "shot-1", "position": 0, "title": "Opening", "prompt": "A river at night",
        "job": {"id": "job-1", "status": "completed", "result_path": str(clip),
                "settings_match": True, "settings_snapshot": settings_snapshot,
                "verification_metadata": {"verified_output": {"width": 448, "height": 768, "fps": 24, "frames": 49}}},
    }]}
    manifest = build_composition_manifest(story, root)
    assert manifest["duration_in_frames"] == 49
    assert manifest["shots"][0]["start_frame"] == 0
    assert manifest["shots"][0]["transition_in_frames"] == 0
    assert manifest["shots"][0]["transition_out_frames"] == 0
    assert manifest["shots"][0]["settings_snapshot"] == settings_snapshot


def test_manifest_accepts_configured_landscape_aspect_and_frame_rate(tmp_path):
    clip=tmp_path/"landscape.mp4"
    clip.write_bytes(b"test-only fixture")
    story={"id":"landscape","revision":1,"title":"Landscape production","shots":[{
        "id":"shot-1","position":0,"title":"Wide","prompt":"User prompt",
        "job":{"id":"job-1","status":"completed","result_path":str(clip),"settings_match":True,
               "verification_metadata":{"verified_output":{"width":768,"height":432,"fps":30,"frames":49}}},
    }]}
    manifest=build_composition_manifest(story,tmp_path)
    assert (manifest["width"],manifest["height"],manifest["fps"])==(768,432,30)
    assert manifest["duration_in_frames"]==49


def test_storyboard_revision_conflicts_are_rejected_without_overwriting_edits(tmp_path):
    db = _db(tmp_path)
    story = db.create_storyboard("scorpion-frog", "Scorpion & Frog", [_shot()])
    db.update_storyboard(story["id"], 1, "New title", [story["shots"][0]])
    with pytest.raises(RuntimeError, match="changed since it was loaded"):
        db.update_storyboard(story["id"], 1, "Stale title", [story["shots"][0]])
    assert db.get_storyboard(story["id"])["title"] == "New title"


def _ready_story(tmp_path: Path):
    root = tmp_path / "outputs"
    root.mkdir(exist_ok=True)
    shots = []
    for i in range(2):
        clip = root / f"shot-{i+1}.mp4"
        clip.write_bytes(b"verified fixture")
        shots.append({
            "id": f"shot-{i+1}", "position": i, "title": f"Shot {i+1}",
            "prompt": "A river at night", "caption": f"Caption {i+1}",
            "job": {"status": "completed", "result_path": str(clip),
                    "verification_metadata": {"verified_output": {"width": 448, "height": 768, "fps": 24, "frames": 49}}},
        })
    return root, {"id": "story-1", "revision": 4, "title": "Scorpion & Frog", "shots": shots}


def test_manifest_is_deterministic_ordered_portrait_and_crossfaded(tmp_path):
    root, story = _ready_story(tmp_path)
    first = build_composition_manifest(story, root)
    second = build_composition_manifest(story, root)
    assert first == second
    assert first["width"] == 448 and first["height"] == 768 and first["fps"] == 24
    assert first["duration_in_frames"] == 90
    assert [(shot["src"], shot["start_frame"], shot["transition_in_frames"], shot["transition_out_frames"])
            for shot in first["shots"]] == [("shot-1.mp4", 0, 0, 8), ("shot-2.mp4", 41, 8, 0)]


def test_manifest_fails_for_missing_or_unverified_clips(tmp_path):
    root, story = _ready_story(tmp_path)
    story["shots"][1]["job"]["status"] = "queued"
    with pytest.raises(ValueError, match="Verified output is missing for: Shot 2"):
        build_composition_manifest(story, root)
    story["shots"][1]["job"]["status"] = "completed"
    story["shots"][1]["job"]["verification_metadata"]["verified_output"]["width"] = 512
    with pytest.raises(ValueError, match="share the same dimensions and frame rate"):
        build_composition_manifest(story, root)


def test_remotion_adapter_passes_public_media_and_verifies_expected_output(tmp_path, monkeypatch):
    async def run():
        studio = tmp_path / "remotion"
        studio.mkdir()
        runner = studio / "remotion"
        runner.write_text("fixture")
        runner.chmod(0o755)
        output_root = tmp_path / "outputs"
        output_root.mkdir()
        settings = Settings(_env_file=None, remotion_studio=str(studio), remotion_executable=str(runner),
                            output_root=str(output_root), allowed_path_roots=str(tmp_path))
        captured = {}

        class Stream:
            lines = [b"render started\n", b""]
            async def readline(self):
                return self.lines.pop(0)

        class Process:
            stdout = Stream()
            returncode = 0
            async def wait(self): return 0

        async def fake_create(*args, **kwargs):
            captured["args"] = list(args)
            captured["env"] = kwargs["env"]
            partial = Path(args[args.index("--output") + 1]) if "--output" in args else Path(args[4])
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"rendered")
            return Process()

        async def fake_verify(path, _settings, expected=None):
            captured["expected"] = expected
            return {"verified_output": expected}

        from app import adapters
        monkeypatch.setattr(adapters.asyncio, "create_subprocess_exec", fake_create)
        monkeypatch.setattr(adapters, "verify_video", fake_verify)
        entrypoint = tmp_path / "storyboard-entry.ts"
        entrypoint.write_text("fixture")
        output = output_root / "story.mp4"
        expected = {"width": 448, "height": 768, "fps": 24, "frames": 90}
        result = await RemotionAdapter(settings).render({
            "entrypoint": str(entrypoint), "composition_id": "StoryboardAssembly", "output_path": str(output),
            "public_dir": str(output_root), "props": {"manifest": {"duration_in_frames": 90}},
            "width": 448, "height": 768, "fps": 24, "expected_output": expected,
        }, lambda *_: asyncio.sleep(0))
        argv = captured["args"]
        assert argv[argv.index("--port") + 1].isdigit()
        assert 1 <= int(argv[argv.index("--port") + 1]) <= 65535
        assert argv[argv.index("--public-dir") + 1] == str(output_root)
        assert argv[argv.index("--width") + 1] == "448"
        assert json.loads(argv[argv.index("--props") + 1])["manifest"]["duration_in_frames"] == 90
        assert captured["expected"] == expected
        assert result["output_path"] == str(output)
        assert output.read_bytes() == b"rendered"

    asyncio.run(run())


def test_remotion_render_port_can_be_configured_or_dynamically_allocated():
    port = select_remotion_render_port(0)
    assert 1 <= port <= 65535
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as free_socket:
        free_socket.bind(("127.0.0.1", 0))
        configured_candidate = free_socket.getsockname()[1]
    configured = select_remotion_render_port(configured_candidate)
    assert configured == configured_candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        blocked_port = occupied.getsockname()[1]
        with pytest.raises(AdapterError, match=f"Configured Remotion render port {blocked_port} is unavailable"):
            select_remotion_render_port(blocked_port)


def test_remotion_adapter_rejects_configured_port_collision_before_starting_process(tmp_path, monkeypatch):
    async def run():
        studio = tmp_path / "remotion"
        studio.mkdir()
        runner = studio / "remotion"
        runner.write_text("fixture")
        runner.chmod(0o755)
        output_root = tmp_path / "outputs"
        output_root.mkdir()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            port = occupied.getsockname()[1]
            settings = Settings(_env_file=None, remotion_studio=str(studio), remotion_executable=str(runner),
                                remotion_render_port=port, output_root=str(output_root),
                                allowed_path_roots=str(tmp_path))
            entrypoint = tmp_path / "storyboard-entry.ts"
            entrypoint.write_text("fixture")

            async def unexpected_start(*_args, **_kwargs):
                pytest.fail("Remotion subprocess must not start if the configured port is occupied")

            from app import adapters
            monkeypatch.setattr(adapters.asyncio, "create_subprocess_exec", unexpected_start)
            with pytest.raises(AdapterError, match=f"Configured Remotion render port {port} is unavailable"):
                await RemotionAdapter(settings).render({
                    "entrypoint": str(entrypoint), "composition_id": "StoryboardAssembly",
                    "output_path": str(output_root / "story.mp4"),
                }, lambda *_: asyncio.sleep(0))

    asyncio.run(run())


def test_remotion_startup_failure_has_actionable_port_context(tmp_path, monkeypatch):
    async def run():
        studio = tmp_path / "remotion"
        studio.mkdir()
        runner = studio / "remotion"
        runner.write_text("fixture")
        runner.chmod(0o755)
        output_root = tmp_path / "outputs"
        output_root.mkdir()
        settings = Settings(_env_file=None, remotion_studio=str(studio), remotion_executable=str(runner),
                            output_root=str(output_root), allowed_path_roots=str(tmp_path))
        entrypoint = tmp_path / "storyboard-entry.ts"
        entrypoint.write_text("fixture")

        async def fail_start(*_args, **_kwargs):
            raise OSError("simulated executable startup failure")

        from app import adapters
        monkeypatch.setattr(adapters.asyncio, "create_subprocess_exec", fail_start)
        with pytest.raises(AdapterError, match=r"Could not start Remotion render on port \d+: simulated executable startup failure"):
            await RemotionAdapter(settings).render({
                "entrypoint": str(entrypoint), "composition_id": "StoryboardAssembly",
                "output_path": str(output_root / "story.mp4"),
            }, lambda *_: asyncio.sleep(0))

    asyncio.run(run())


def test_remotion_nonzero_exit_includes_underlying_cli_output(tmp_path, monkeypatch):
    async def run():
        studio = tmp_path / "remotion"
        studio.mkdir()
        runner = studio / "remotion"
        runner.write_text("fixture")
        runner.chmod(0o755)
        output_root = tmp_path / "outputs"
        output_root.mkdir()
        settings = Settings(_env_file=None, remotion_studio=str(studio), remotion_executable=str(runner),
                            output_root=str(output_root), allowed_path_roots=str(tmp_path))
        entrypoint = tmp_path / "storyboard-entry.ts"
        entrypoint.write_text("fixture")

        class Stream:
            lines = [b"Tried to go to http://localhost:3000/index.html\n", b"window.getStaticCompositions is undefined\n", b""]
            async def readline(self): return self.lines.pop(0)

        class Process:
            stdout = Stream()
            returncode = 1
            async def wait(self): return 1

        async def fail_render(*_args, **_kwargs): return Process()
        from app import adapters
        monkeypatch.setattr(adapters.asyncio, "create_subprocess_exec", fail_render)
        with pytest.raises(AdapterError) as error:
            await RemotionAdapter(settings).render({
                "entrypoint": str(entrypoint), "composition_id": "StoryboardAssembly",
                "output_path": str(output_root / "story.mp4"),
            }, lambda *_: asyncio.sleep(0))
        assert error.value.exit_code == 1
        assert "window.getStaticCompositions is undefined" in str(error.value)
        assert "Final Remotion output" in str(error.value)
        assert "renderer port" in str(error.value)

    asyncio.run(run())
