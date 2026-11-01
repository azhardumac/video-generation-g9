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


def create_run(db, project, max_shots=4, max_seconds=3600, max_output_bytes=8 * 1024**3):
    request = {"prompt": "A lighthouse keeper guides a lost boat home before a storm.",
        "reference_ids": [], "reference_categories": [], "preset": "Short",
        "budget": {"max_shots": max_shots, "max_retries_per_shot": 1,
                   "max_generation_seconds": max_seconds, "max_output_bytes": max_output_bytes},
        "generation_profile": {"frames": 49, "fps": 24, "width": 448, "height": 768},
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
