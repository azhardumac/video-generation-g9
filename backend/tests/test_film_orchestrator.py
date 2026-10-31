import pytest
from app.config import Settings
from app.db import Database
from app.film_orchestrator import FilmOrchestrator, FilmRunConflict, FilmRunTransitionError, enforce_generation_budget


def make_db(tmp_path):
    settings = Settings(_env_file=None, database_path=str(tmp_path / "studio.sqlite3"),
                        allowed_path_roots=str(tmp_path), output_root=str(tmp_path / "outputs"))
    db = Database(settings)
    project = db.create_project({"id":"story", "name":"Story", "root":str(tmp_path),
                                 "aspect_ratio":"9:16", "frame_rate":24, "production_type":"short film"})
    return db, project


def run_request(prompt="A traveler finds a lost key and returns it before the storm."):
    return {"prompt":prompt, "reference_ids":[], "reference_categories":[], "preset":"Short",
            "budget":{"max_shots":12,"max_retries_per_shot":1,"max_generation_seconds":3600,
                      "max_output_bytes":8 * 1024**3},
            "generation_profile":{"frames":49,"fps":24,"width":448,"height":768},
            "authorization":{"confirmed":True,"scope":"bounded local run"}}


def test_film_run_creation_is_idempotent_and_project_scoped(tmp_path):
    db, project = make_db(tmp_path)
    engine = FilmOrchestrator(db)
    request = run_request()
    first = engine.create(project["id"], "request-1", request, authorized=True)
    repeated = engine.create(project["id"], "request-1", request, authorized=True)
    assert first["id"] == repeated["id"]
    assert first["created"] is True and repeated["created"] is False
    assert repeated["status"] == "waiting_for_planner"
    assert repeated["stage"] == "understand_story"
    assert db.list_jobs(project_id=project["id"]) == []
    other = db.create_project({"id":"other", "name":"Other", "root":str(tmp_path),
                               "aspect_ratio":"9:16", "frame_rate":24, "production_type":"short film"})
    assert engine.list(other["id"]) == []
    with pytest.raises(FilmRunConflict, match="Idempotency key"):
        engine.create(project["id"], "request-1", run_request("A different request."), authorized=True)


def test_authorization_is_explicit_and_bounded_controls_persist(tmp_path):
    db, project = make_db(tmp_path)
    engine = FilmOrchestrator(db)
    run = engine.create(project["id"], "request-2", run_request(), authorized=False)
    assert run["status"] == "awaiting_authorization"
    with pytest.raises(FilmRunTransitionError, match="explicit"):
        engine.authorize(run["id"], run["revision"], {"confirmed":False})
    authorized = engine.authorize(run["id"], run["revision"], {"confirmed":True,"budget_acknowledged":True})
    assert authorized["status"] == "waiting_for_planner"
    assert authorized["authorization"]["budget_acknowledged"] is True
    assert authorized["budget"]["max_output_bytes"] == 8 * 1024**3


def test_mock_stage_handler_saves_checkpoint_and_advances_once(tmp_path):
    db, project = make_db(tmp_path)
    engine = FilmOrchestrator(db)
    run = engine.create(project["id"], "request-3", run_request(), authorized=True)
    seen = {}
    def fake_handler(stage, current):
        seen.update(current)
        return {"stage":stage,"outline":"grounded test fixture"}
    result = engine.process_one(run["id"], fake_handler)
    assert seen["stage_idempotency_key"]
    assert result["status"] == "waiting_for_planner"
    assert result["stage"] == "define_visual_bible"
    assert result["completed_stages"] == ["understand_story"]
    assert result["checkpoint"]["understand_story"]["outline"] == "grounded test fixture"
    assert result["revision"] == run["revision"] + 2


def test_missing_production_handler_is_honest_and_never_marks_success(tmp_path):
    db, project = make_db(tmp_path)
    engine = FilmOrchestrator(db)
    run = engine.create(project["id"], "request-4", run_request(), authorized=True)
    blocked = engine.process_one(run["id"])
    assert blocked["status"] == "needs_attention"
    assert "No verified production handler" in blocked["error"]
    assert blocked["completed_stages"] == []
    assert db.list_jobs(project_id=project["id"]) == []


def test_pause_resume_cancel_and_optimistic_revision(tmp_path):
    db, project = make_db(tmp_path)
    engine = FilmOrchestrator(db)
    run = engine.create(project["id"], "request-5", run_request(), authorized=True)
    paused = engine.pause(run["id"], run["revision"])
    assert paused["status"] == "paused"
    with pytest.raises(RuntimeError, match="changed"):
        engine.resume(run["id"], run["revision"])
    resumed = engine.resume(run["id"], paused["revision"])
    cancelled = engine.cancel(run["id"], resumed["revision"])
    assert cancelled["status"] == "cancelled"
    assert db.get_film_run(run["id"])["status"] == "cancelled"
    assert [event["revision"] for event in cancelled["events"]] == [1,2,3,4]


def test_restart_recovers_running_orchestration_at_checkpoint_without_requeue(tmp_path):
    db, project = make_db(tmp_path)
    run = FilmOrchestrator(db).create(project["id"], "request-6", run_request(), authorized=True)
    with db.connect() as connection:
        connection.execute("UPDATE film_runs SET status='running',checkpoint_json=? WHERE id=?",
                           ('{"understand_story":{"checkpoint":"saved"}}',run["id"]))
    restarted = Database(Settings(_env_file=None, database_path=str(tmp_path / "studio.sqlite3"),
                                  allowed_path_roots=str(tmp_path), output_root=str(tmp_path / "outputs")))
    recovered = restarted.get_film_run(run["id"])
    assert recovered["status"] == "paused"
    assert recovered["stage"] == "understand_story"
    assert recovered["checkpoint"]["understand_story"]["checkpoint"] == "saved"
    assert "restarted" in recovered["events"][-1]["message"]
    assert restarted.list_jobs(project_id=project["id"]) == []


def test_plan_and_repair_results_must_stay_within_budget(tmp_path):
    db, project = make_db(tmp_path)
    run = FilmOrchestrator(db).create(project["id"], "request-7", run_request(), authorized=True)
    with pytest.raises(FilmRunTransitionError, match="required budget estimate"):
        enforce_generation_budget(run,{"shot_count":12},"plan_scenes")
    with pytest.raises(FilmRunTransitionError, match="shot count"):
        enforce_generation_budget(run,{"shot_count":13,"estimated_generation_seconds":1000,"estimated_output_bytes":1000},"plan_scenes")
    with pytest.raises(FilmRunTransitionError, match="time budget"):
        enforce_generation_budget(run,{"generated_shots":12,"elapsed_seconds":3601,"output_bytes":1},"generate_shots")
    with pytest.raises(FilmRunTransitionError, match="retry budget"):
        enforce_generation_budget(run,{"retry_attempts":{"shot-1":2}},"repair_failed_shots")


def test_prompt_first_api_requires_confirmation_and_never_queues_jobs(tmp_path, monkeypatch):
    from app import main

    db, project = make_db(tmp_path)
    monkeypatch.setattr(main,"db",db)
    monkeypatch.setattr(main,"film_orchestrator",FilmOrchestrator(db))

    class IdleWorker:
        async def start(self):
            return None
        async def stop(self):
            return None

    monkeypatch.setattr(main,"worker",IdleWorker())
    payload={"prompt":"A small story about a lantern crossing a foggy harbor.","reference_ids":[],
             "preset":"Short","idempotency_key":"http-api-key","initial_confirmation":True,
             "budget":{"max_shots":4,"max_retries_per_shot":0,"max_generation_seconds":1200,
                       "max_output_bytes":256*1024**2}}
    request=main.CreateFilmRunRequest.model_validate(payload)
    preview=main.preview_film_run(project["id"],request)
    assert preview["preview_only"] is True
    assert preview["writes_database"] is False
    assert preview["generation_time_estimate_seconds"] is None
    assert db.list_film_runs(project["id"])==[]
    created=main.create_film_run(project["id"],request)
    repeated=main.create_film_run(project["id"],request)
    capabilities=main.film_orchestration_capabilities()
    assert repeated["id"]==created["id"]
    assert created["status"]=="waiting_for_planner"
    assert created["generation_jobs_created"]==0
    assert capabilities["general_story_planner"]["available"] is False
    assert capabilities["creative_quality_detection"]["available"] is False
    assert db.list_jobs(project_id=project["id"])==[]


def test_logical_shot_and_attempt_reservations_are_idempotent_and_budgeted(tmp_path):
    db, project = make_db(tmp_path)
    request = run_request()
    request["budget"]["max_shots"] = 1
    run = FilmOrchestrator(db).create(project["id"], "shot-run", request, authorized=True)
    shot_request = {"prompt":"A lantern moves through fog.","preset":"Short","seed":17}
    first = db.ensure_film_run_shot(run["id"], "scene-1-shot-1", shot_request)
    repeated = db.ensure_film_run_shot(run["id"], "scene-1-shot-1", shot_request)
    assert first["id"] == repeated["id"]
    assert first["created"] is True and repeated["created"] is False
    assert repeated["shot"] == shot_request
    with pytest.raises(FilmRunConflict, match="different shot settings"):
        db.ensure_film_run_shot(run["id"], "scene-1-shot-1", shot_request | {"seed":18})
    with pytest.raises(FilmRunTransitionError, match="shot-count budget"):
        db.ensure_film_run_shot(run["id"], "scene-1-shot-2", shot_request)
    with db.connect() as connection:
        connection.execute("UPDATE film_runs SET status='running',stage='generate_shots' WHERE id=?",(run["id"],))
    attempt_request = {"prompt":shot_request["prompt"],"seed":shot_request["seed"]}
    attempt0 = db.reserve_film_run_shot_attempt(run["id"], "scene-1-shot-1", 0, attempt_request)
    repeated_attempt0 = db.reserve_film_run_shot_attempt(run["id"], "scene-1-shot-1", 0, attempt_request)
    attempt1 = db.reserve_film_run_shot_attempt(run["id"], "scene-1-shot-1", 1, attempt_request | {"repair":1})
    assert attempt0["idempotency_key"] == repeated_attempt0["idempotency_key"]
    assert repeated_attempt0["created"] is False
    assert attempt1["idempotency_key"] != attempt0["idempotency_key"]
    with pytest.raises(FilmRunTransitionError, match="retry budget"):
        db.reserve_film_run_shot_attempt(run["id"], "scene-1-shot-1", 2, attempt_request)
    assert db.list_jobs(project_id=project["id"]) == []
