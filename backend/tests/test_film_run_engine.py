import asyncio
import pytest
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.film_orchestrator import FilmOrchestrator
from app.film_run_engine import FilmRunEngine
from app.story_planning import StoryPlan
from app.worker import LocalWorker


def make_db(tmp_path):
    settings = Settings(_env_file=None, database_path=str(tmp_path / "studio.sqlite3"),
                        allowed_path_roots=str(tmp_path), output_root=str(tmp_path / "outputs"),
                        story_planner_command="")
    db = Database(settings)
    project = db.create_project({"id": "lighthouse-project", "name": "Lighthouse", "root": str(tmp_path),
                                 "aspect_ratio": "9:16", "frame_rate": 24, "production_type": "short film"})
    return db, project, settings


def plan_data(second_shot=False):
    shots = [{"id": "beam-finds-boat", "sequence": 0, "title": "The beam finds the boat",
              "beat": "Mira turns the beam toward the lost boat.",
              "prompt": "The lighthouse beam sweeps across fog and reveals a small boat near the harbor mouth.",
              "framing": "Wide establishing shot", "character_layout": "Mira is in the foreground; Noah remains on the boat in the background.",
              "characters_present": ["keeper", "sailor"], "expected_character_count": 2,
              "reference_ids": [], "caption": "", "transition_after": "cut"}]
    if second_shot:
        shots.append({"id": "boat-reaches-pier", "sequence": 1, "title": "The boat reaches the pier",
              "beat": "The lost boat reaches the sheltered pier.",
              "prompt": "The small boat moves from the foggy harbor mouth toward the sheltered stone pier.",
              "framing": "Medium-wide tracking shot", "character_layout": "Mira watches from the hill; Noah stays aboard the same boat.",
              "characters_present": ["keeper", "sailor"], "expected_character_count": 2,
              "reference_ids": [], "caption": "", "transition_after": "fade"})
    return {"title": "The Lighthouse Keeper", "logline": "A keeper guides a lost boat home before the storm reaches the harbor.",
        "visual_style": "Handmade miniature sets with tactile painted surfaces and soft dusk lighting.",
        "audio_requested": False,
        "video_continuation_requested": False,
        "ic_lora_requested": False,
        "characters": [
            {"id": "keeper", "name": "Mira", "identity": "An elderly lighthouse keeper with a silver braid and yellow oilskin coat.",
             "distinguishing_traits": ["silver braid", "yellow coat"], "reference_ids": []},
            {"id": "sailor", "name": "Noah", "identity": "A young sailor with a dark wool cap and a red scarf.",
             "distinguishing_traits": ["dark cap", "red scarf"], "reference_ids": []}],
        "locations": [{"id": "harbor", "name": "Foggy harbor", "description": "A small stone harbor below the lighthouse, wrapped in coastal fog.",
            "scale": "Human-sized dock and one distant lighthouse", "continuity_requirements": ["Lighthouse remains on the eastern headland"]}],
        "scenes": [{"id": "storm-arrives", "sequence": 0, "title": "The storm arrives",
            "story_beat": "Mira notices a boat lost as the storm reaches the harbor.", "location_id": "harbor",
            "characters_present": ["keeper", "sailor"], "visual_description": "A boat appears through fog beneath the lighthouse beam.",
            "framing": "Wide view from the lighthouse hill", "character_layout": "Keeper on the hill; sailor aboard one boat.",
            "expected_character_count": 2, "continuity_requirements": ["One boat only", "Beam points to harbor mouth"],
            "shots": shots}]}


class FakePlanner:
    def __init__(self, plan):
        self.value = StoryPlan.model_validate(plan)
        self.requests = []

    def plan(self, request):
        self.requests.append(request)
        return self.value


def create_run(db, project, max_shots=4, max_seconds=3600, max_output_bytes=8 * 1024**3, quality=None, sound=None):
    request = {"prompt": "A lighthouse keeper guides a lost boat home before a storm.",
        "reference_ids": [], "reference_categories": [], "preset": "Short",
        "budget": {"max_shots": max_shots, "max_retries_per_shot": 1,
                   "max_generation_seconds": max_seconds, "max_output_bytes": max_output_bytes},
        "generation_profile": {"frames": 49, "fps": 24, "width": 448, "height": 768,
                               **({"quality": quality} if quality else {}),
                               **({"audio": sound} if sound is not None else {})},
        "authorization": {"confirmed": True, "scope": "bounded local run"}}
    return FilmOrchestrator(db).create(project["id"], "test-key", request, authorized=True)


def test_prompt_to_validated_storyboard_to_single_worker_queue_is_project_scoped(tmp_path):
    db, project, settings = make_db(tmp_path)
    planner = FakePlanner(plan_data())
    engine = FilmRunEngine(db, settings, planner=planner, ltx_ready=lambda: True)
    run = create_run(db, project)

    assert engine.process_next()["stage"] == "define_visual_bible"
    assert engine.process_next()["stage"] == "plan_scenes"
    persisted = engine.process_next()
    assert persisted["stage"] == "anchor_visuals"
    storyboard = db.get_storyboard(persisted["storyboard_id"], include_events=False)
    assert storyboard["project_id"] == project["id"]
    assert storyboard["film_plan"]["approved"] is True
    assert storyboard["title"] == "The Lighthouse Keeper"
    assert storyboard["shots"][0]["framing"] == "Wide establishing shot"
    assert engine.process_next()["stage"] == "generate_shots"

    queued_run = engine.process_next()
    assert queued_run["stage"] == "inspect_clips"
    assert queued_run["film_batch_id"]
    jobs = db.list_jobs(project_id=project["id"])
    assert len(jobs) == 1
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["payload"]["reference_image"] is None
    assert jobs[0]["payload"]["frames"] == 49
    assert jobs[0]["payload"]["audio"] is False
    assert jobs[0]["payload"]["job_label"].startswith("LTX shot")

    repeated_batch = db.create_film_batch(storyboard["id"], storyboard["revision"],
                                          storyboard["style_bible_revision"], film_run_id=run["id"])
    assert repeated_batch["id"] == queued_run["film_batch_id"]
    assert len(db.list_jobs(project_id=project["id"])) == 1
    assert planner.requests[0]["project_id"] == project["id"]
    assert db.list_jobs(project_id="another-project") == []


def test_missing_local_planner_stops_run_with_no_fabricated_plan_or_job(tmp_path):
    db, project, settings = make_db(tmp_path)
    run = create_run(db, project)
    result = FilmRunEngine(db, settings).process_next()
    assert result["id"] == run["id"]
    assert result["status"] == "needs_attention"
    assert "No local general story-planning model" in result["error"]
    assert result["storyboard_id"] is None
    assert db.list_storyboards(project["id"]) == []
    assert db.list_jobs(project_id=project["id"]) == []


def test_shot_budget_is_checked_before_any_storyboard_or_job_is_created(tmp_path):
    db, project, settings = make_db(tmp_path)
    planner = FakePlanner(plan_data(second_shot=True))
    run = create_run(db, project, max_shots=1)
    engine = FilmRunEngine(db, settings, planner=planner, ltx_ready=lambda: True)
    result = engine.process_next()
    assert result["status"] == "needs_attention"
    assert "exceeding the authorized 1 shot limit" in result["error"]
    assert db.list_storyboards(project["id"]) == []
    assert db.list_jobs(project_id=project["id"]) == []


def test_plan_reference_must_be_linked_to_the_selected_project(tmp_path):
    db, project, settings = make_db(tmp_path)
    plan = plan_data()
    plan["characters"][0]["reference_ids"] = ["not-in-this-project"]
    plan["scenes"][0]["shots"][0]["reference_ids"] = ["not-in-this-project"]
    planner = FakePlanner(plan)
    request = create_run(db, project)
    engine = FilmRunEngine(db, settings, planner=planner, ltx_ready=lambda: True)
    result = engine.process_next()
    assert result["status"] == "needs_attention"
    assert "not linked to this project" in result["error"]
    assert db.list_jobs(project_id=project["id"]) == []


def test_unsupported_audio_capability_blocks_before_plan_or_jobs(tmp_path):
    db, project, settings = make_db(tmp_path)
    plan = plan_data()
    plan["audio_requested"] = True
    planner = FakePlanner(plan)
    run = create_run(db, project)
    result = FilmRunEngine(db, settings, planner=planner, ltx_ready=lambda: True).process_next()
    assert result["status"] == "needs_attention"
    assert "requires unsupported local capability: audio" in result["error"]
    assert db.list_storyboards(project["id"]) == []
    assert db.list_jobs(project_id=project["id"]) == []


def test_prompt_run_does_not_prepare_remotion_manifest_before_creative_review(tmp_path):
    db, project, settings = make_db(tmp_path)
    planner = FakePlanner(plan_data())
    engine = FilmRunEngine(db, settings, planner=planner, ltx_ready=lambda: True)
    run = create_run(db, project)
    for _ in range(5):
        result = engine.process_next()
    storyboard = db.get_storyboard(result["storyboard_id"], include_events=False)
    job = db.list_jobs(project_id=project["id"])[0]
    with db.connect() as connection:
        connection.execute("UPDATE film_batches SET status='completed' WHERE film_run_id=?", (run["id"],))
    LocalWorker(db, settings)._assemble_completed_film(job)
    manifest_path = settings.expanded(settings.output_root) / "storyboards" / storyboard["id"] / "composition-manifest.json"
    assert not manifest_path.exists()


def test_batch_queue_cannot_be_enqueued_for_cancelled_or_unauthorized_run(tmp_path):
    db, project, settings = make_db(tmp_path)
    run = create_run(db, project)
    with db.connect() as connection:
        connection.execute("UPDATE film_runs SET status='cancelled' WHERE id=?", (run["id"],))
    with pytest.raises(RuntimeError, match="stopped or unauthorized"):
        db.create_film_batch("missing-storyboard", 1, 0, film_run_id=run["id"])
    assert db.list_jobs(project_id=project["id"]) == []


def test_restart_between_batch_enqueue_and_checkpoint_reuses_same_jobs(tmp_path):
    db, project, settings = make_db(tmp_path)
    planner = FakePlanner(plan_data())
    engine = FilmRunEngine(db, settings, planner=planner, ltx_ready=lambda: True)
    run = create_run(db, project)
    engine.process_next()
    engine.process_next()
    plan_run = engine.process_next()
    engine.process_next()
    storyboard = db.get_storyboard(plan_run["storyboard_id"], include_events=False)
    with db.connect() as connection:
        connection.execute("UPDATE film_runs SET status='running',stage='generate_shots' WHERE id=?", (run["id"],))
    first_batch = db.create_film_batch(storyboard["id"], storyboard["revision"],
                                       storyboard["style_bible_revision"], film_run_id=run["id"])
    restarted = Database(Settings(_env_file=None, database_path=str(tmp_path / "studio.sqlite3"),
                                  allowed_path_roots=str(tmp_path), output_root=str(tmp_path / "outputs")))
    recovered = restarted.get_film_run(run["id"])
    assert recovered["status"] == "paused"
    assert restarted.get_film_batch(first_batch["id"])["status"] == "paused"
    assert restarted.list_jobs(project_id=project["id"])[0]["status"] == "queued"
    resumed = FilmOrchestrator(restarted).resume(recovered["id"], recovered["revision"])
    resumed_engine = FilmRunEngine(restarted, settings, planner=planner, ltx_ready=lambda: True)
    queued = resumed_engine.process_next()
    assert queued["film_batch_id"] == first_batch["id"]
    assert len(restarted.list_jobs(project_id=project["id"])) == 1


def test_generation_timeout_cancels_fake_adapter_and_stops_the_run_queue(tmp_path):
    db, project, settings = make_db(tmp_path)
    planner = FakePlanner(plan_data(second_shot=True))
    engine = FilmRunEngine(db, settings, planner=planner, ltx_ready=lambda: True)
    run = create_run(db, project, max_seconds=60)
    for _ in range(5):
        engine.process_next()
    jobs = sorted(db.list_jobs(project_id=project["id"]), key=lambda item: item["created_at"])
    assert len(jobs) == 2
    completed_path = tmp_path / "completed-fixture.mp4"
    completed_path.write_bytes(b"fixture, not a video")
    with db.connect() as connection:
        connection.execute("UPDATE jobs SET status='completed',elapsed_seconds=59,result_path=?,verification_metadata=? WHERE id=?",
                           (str(completed_path), '{"verified_output":{"width":448,"height":768,"fps":24,"frames":49}}', jobs[0]["id"]))

    class SlowFakeAdapter:
        cancelled = False
        async def generate(self, payload, progress):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    worker = LocalWorker(db, settings)
    worker.adapter = SlowFakeAdapter()
    asyncio.run(worker.execute(jobs[1]))
    assert worker.adapter.cancelled is True
    assert db.get_job(jobs[1]["id"])["status"] == "failed"
    assert db.get_film_run(run["id"])["status"] == "needs_attention"
    assert "time budget expired" in db.get_film_run(run["id"])["error"]


def test_output_storage_budget_stops_fake_adapter_and_preserves_partial_file(tmp_path):
    db, project, settings = make_db(tmp_path)
    planner = FakePlanner(plan_data(second_shot=True))
    engine = FilmRunEngine(db, settings, planner=planner, ltx_ready=lambda: True)
    cap = 256 * 1024**2
    run = create_run(db, project, max_output_bytes=cap)
    for _ in range(5):
        engine.process_next()
    jobs = sorted(db.list_jobs(project_id=project["id"]), key=lambda item: item["created_at"])

    class OversizeFakeAdapter:
        async def generate(self, payload, progress):
            partial = Path(payload["output_location"]) / f"{payload['job_id']}.partial.mp4"
            with partial.open("wb") as stream:
                stream.truncate(cap)
            await progress(50, "fixture has reached the declared output cap")

    worker = LocalWorker(db, settings)
    worker.adapter = OversizeFakeAdapter()
    asyncio.run(worker.execute(jobs[0]))
    current = db.get_job(jobs[0]["id"])
    assert current["status"] == "cancelled"
    assert Path(current["partial_output_path"]).stat().st_size == cap
    assert db.get_job(jobs[1]["id"])["status"] == "cancelled"
    assert db.get_film_run(run["id"])["status"] == "needs_attention"


@pytest.mark.parametrize(("quality", "size", "expected"), [
    ("draft", (448, 768), (448, 768)),
    ("better", (448, 768), (576, 1024)),
    ("best", (768, 432), (1024, 576)),
    ("better", (640, 640), (832, 832)),
])
def test_quality_scales_resolution_on_the_two_stage_grid(quality, size, expected):
    from app.presets import quality_dimensions

    assert quality_dimensions(*size, quality) == expected


def _queue_single_shot(engine):
    while (run := engine.process_next())["stage"] != "inspect_clips":
        assert run["status"] != "needs_attention", run.get("error")


def test_best_quality_film_run_uses_dev_two_stage_at_higher_resolution(tmp_path):
    from app.adapters import build_ltx_command

    db, project, settings = make_db(tmp_path)
    engine = FilmRunEngine(db, settings, planner=FakePlanner(plan_data()), ltx_ready=lambda: True)
    create_run(db, project, quality="best")
    _queue_single_shot(engine)
    payload = db.list_jobs(project_id=project["id"])[0]["payload"]
    assert (payload["width"], payload["height"], payload["quality"]) == (576, 1024, "best")
    assert payload["distilled_two_stage"] is False
    command = build_ltx_command("ltx", payload, "/out/job.partial.mp4")
    assert "--two-stage" in command and "--distilled" not in command


def test_time_estimate_only_uses_benchmarks_of_the_same_quality(tmp_path):
    db, project, settings = make_db(tmp_path)
    db.add_benchmark("Short", 49, 24, 2.04, elapsed_seconds=99999, quality="draft")
    engine = FilmRunEngine(db, settings, planner=FakePlanner(plan_data()), ltx_ready=lambda: True)
    create_run(db, project, quality="better")
    _queue_single_shot(engine)
    assert db.benchmarks()[0]["quality"] == "draft"
    payload = db.list_jobs(project_id=project["id"])[0]["payload"]
    assert (payload["width"], payload["height"], payload["distilled_two_stage"]) == (576, 1024, True)


def test_direct_jobs_are_benchmarked_as_draft_even_with_a_saved_quality(tmp_path, monkeypatch):
    from app import main
    from app.presets import GenerationSettings

    monkeypatch.setattr(main, "resource_snapshot", lambda path: {})
    monkeypatch.setattr(main.resource_preflight, "assess", lambda metrics: [])
    config = GenerationSettings(prompt="A boat", quality="best", output_location=str(main.settings.expanded(main.settings.output_root)))
    payload = main._generation_payload(config)
    assert payload["quality"] == "draft" and payload["distilled_two_stage"] is True


@pytest.mark.parametrize(("quality", "distilled"), [("best", False), ("better", True), ("draft", True)])
def test_storyboard_regenerate_keeps_the_film_run_quality_and_sound(tmp_path, monkeypatch, quality, distilled):
    from app import main

    db, project, settings = make_db(tmp_path)
    engine = FilmRunEngine(db, settings, planner=FakePlanner(plan_data()), ltx_ready=lambda: True)
    run = create_run(db, project, quality=quality, sound=True)
    _queue_single_shot(engine)
    storyboard_id = db.get_film_run(run["id"])["storyboard_id"]
    monkeypatch.setattr(main, "db", db)
    payload = {"quality": "draft", "distilled_two_stage": True, "audio": False}
    main._apply_film_run_profile(payload, storyboard_id)
    assert payload == {"quality": quality, "distilled_two_stage": distilled, "audio": True}
    untouched = {"quality": "draft", "distilled_two_stage": True, "audio": False}
    main._apply_film_run_profile(untouched, "not-a-film-storyboard")
    assert untouched == {"quality": "draft", "distilled_two_stage": True, "audio": False}


def test_sound_film_run_generates_audio_and_puts_the_sound_line_in_both_prompts(tmp_path):
    from app.adapters import build_ltx_command

    db, project, settings = make_db(tmp_path)
    plan = plan_data()
    plan["audio_requested"] = True
    plan["scenes"][0]["shots"][0]["sound"] = "foghorn, waves against the rocks"
    planner = FakePlanner(plan)
    engine = FilmRunEngine(db, settings, planner=planner, ltx_ready=lambda: True)
    create_run(db, project, sound=True)
    _queue_single_shot(engine)
    payload = db.list_jobs(project_id=project["id"])[0]["payload"]
    assert payload["audio"] is True
    assert "Sound: natural sounds only, foghorn, waves against the rocks; no music, no voices." in payload["prompt"]
    assert "music" in payload["negative_prompt"] and "speech" in payload["negative_prompt"]
    assert "Sound: natural sounds only, foghorn, waves against the rocks; no music, no voices." in payload["continuation_prompt"]
    assert "--no-audio" not in build_ltx_command("ltx", payload, "/out/job.partial.mp4")


def test_silent_film_run_drops_the_sound_line_and_mutes_ltx(tmp_path):
    from app.adapters import build_ltx_command

    db, project, settings = make_db(tmp_path)
    plan = plan_data()
    plan["scenes"][0]["shots"][0]["sound"] = "foghorn"
    engine = FilmRunEngine(db, settings, planner=FakePlanner(plan), ltx_ready=lambda: True)
    create_run(db, project, sound=False)
    _queue_single_shot(engine)
    payload = db.list_jobs(project_id=project["id"])[0]["payload"]
    assert payload["audio"] is False and "Sound:" not in payload["prompt"]
    assert "--no-audio" in build_ltx_command("ltx", payload, "/out/job.partial.mp4")


def test_sound_run_time_estimate_ignores_silent_benchmarks(tmp_path):
    db, project, settings = make_db(tmp_path)
    db.add_benchmark("Short", 49, 24, 2.04, elapsed_seconds=99999, quality="better", audio=False)
    engine = FilmRunEngine(db, settings, planner=FakePlanner(plan_data()), ltx_ready=lambda: True)
    create_run(db, project, quality="better", sound=True)
    _queue_single_shot(engine)
    payload = db.list_jobs(project_id=project["id"])[0]["payload"]
    assert payload["audio"] is True
    assert db.benchmarks()[0]["audio"] == 0


def test_plan_only_authorization_checks_the_time_cap_with_the_runs_quality_and_sound(monkeypatch):
    from app import main

    run = {"id": "run", "project_id": "project", "status": "awaiting_authorization", "stage": "define_visual_bible",
           "prompt": "A frog", "reference_ids": [], "preset": "Short", "budget": {"max_shots": 1, "max_output_bytes": 2**30},
           "checkpoint": {"understand_story": {"title": "Frog"}},
           "request": {"plan_only": True, "generation_profile": {"quality": "best", "audio": False}}}
    seen = []
    monkeypatch.setattr(main.film_orchestrator, "get", lambda run_id: run)
    monkeypatch.setattr(main.film_orchestrator, "authorize", lambda *args: {"authorized": True})
    monkeypatch.setattr(main, "preview_film_run", lambda project_id, request: seen.append(request) or {
        "available_output_bytes": 10**12, "estimated_output_within_cap": True, "generation_time_within_cap": True})
    main.authorize_film_run("run", main.FilmRunAuthorizationRequest(confirmed=True, budget_acknowledged=True, expected_revision=1))
    assert (seen[0].quality, seen[0].sound) == ("best", False)


def test_film_run_chains_only_shots_that_keep_the_framing_and_names_each_shots_cast(tmp_path):
    db, project, settings = make_db(tmp_path)
    plan = plan_data(second_shot=True)
    first, second = plan["scenes"][0]["shots"]
    second["framing"] = "Wide shot from the same hill"
    close = second | {"id": "keeper-close-up", "sequence": 2, "title": "Mira waves",
                      "framing": "Close-up", "characters_present": ["keeper"], "expected_character_count": 1}
    plan["scenes"][0]["shots"].append(close)
    engine = FilmRunEngine(db, settings, planner=FakePlanner(plan), ltx_ready=lambda: True)
    create_run(db, project)
    _queue_single_shot(engine)
    payloads = sorted((job["payload"] for job in db.list_jobs(project_id=project["id"])), key=lambda p: p["shot_order"])
    assert [p["continue_from_shot_id"] is not None for p in payloads] == [False, True, False]
    assert [p["reference_conditioning"] for p in payloads] == ["text-only", "previous-shot", "text-only"]
    assert payloads[1]["continue_from_shot_id"] == payloads[0]["shot_id"]
    cast = "On screen: only one Mira; no additional characters or creatures, and no duplicates."
    assert cast in payloads[2]["continuation_prompt"] and cast in payloads[2]["prompt"]
    assert "one Noah" not in payloads[2]["prompt"]
    assert "Scene character layout" not in payloads[0]["prompt"]


def test_film_run_cuts_instead_of_chaining_when_a_character_leaves_the_shot(tmp_path):
    db, project, settings = make_db(tmp_path)
    plan = plan_data(second_shot=True)
    second = plan["scenes"][0]["shots"][1]
    second |= {"framing": "Wide shot from the same hill", "characters_present": ["keeper"], "expected_character_count": 1}
    engine = FilmRunEngine(db, settings, planner=FakePlanner(plan), ltx_ready=lambda: True)
    create_run(db, project)
    _queue_single_shot(engine)
    payloads = sorted((job["payload"] for job in db.list_jobs(project_id=project["id"])), key=lambda p: p["shot_order"])
    assert [p["continue_from_shot_id"] for p in payloads] == [None, None]


def test_character_references_are_keyed_past_the_kept_frames_and_other_references_guide_the_opening():
    from app.film_run_engine import CHARACTER_REFERENCE_TAIL_FRAMES, reference_anchor_plan

    anchors, generate = reference_anchor_plan([("/refs/frog.png", "character")], 97)
    assert generate == 97 + CHARACTER_REFERENCE_TAIL_FRAMES
    assert anchors == [{"path": "/refs/frog.png", "frame_index": generate - 1, "strength": 0.3, "binding": "guide"}]
    assert reference_anchor_plan([("/refs/river.png", "scene")], 97) == (
        [{"path": "/refs/river.png", "frame_index": 0, "strength": 0.35, "binding": "guide"}], 97)
    assert reference_anchor_plan([], 97) == ([], 97)


def test_two_character_shots_key_both_sheets_inside_the_trimmed_tail():
    from app.film_run_engine import CHARACTER_REFERENCE_TAIL_FRAMES, reference_anchor_plan

    anchors, generate = reference_anchor_plan([("/refs/frog.png", "character"), ("/refs/scorpion.png", "character")], 97)
    assert generate == 97 + CHARACTER_REFERENCE_TAIL_FRAMES + 8 and (generate - 1) % 8 == 0
    assert [a["path"] for a in anchors] == ["/refs/frog.png", "/refs/scorpion.png"]
    assert all(a["frame_index"] - 97 >= CHARACTER_REFERENCE_TAIL_FRAMES - 1 for a in anchors)


def test_quoted_dialogue_is_removed_from_the_visual_prompt():
    from app.film_run_engine import silent_action

    assert silent_action('The scorpion asks "Will you carry me?" and points across.') == "The scorpion asks and points across."
    assert silent_action("He says \u201cno\u201d firmly.") == "He says firmly."


def test_reference_notes_replace_the_planners_guessed_identity():
    from types import SimpleNamespace
    from app.film_run_engine import character_identity

    scorpion = SimpleNamespace(name="Scorpion", identity="A black scorpion", distinguishing_traits=["black"], reference_ids=["r1"])
    linked = {"r1": {"notes": "Reddish-brown clay scorpion with amber tail"}}
    assert character_identity(scorpion, linked, {"r1": "/refs/s.png"}) == "Scorpion: Reddish-brown clay scorpion with amber tail"
    assert "A black scorpion" in character_identity(scorpion, {"r1": {"notes": ""}}, {"r1": "/refs/s.png"})
