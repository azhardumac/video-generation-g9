import json

from app.config import Settings
from app.db import Database
from app.film_orchestrator import FilmOrchestrator
from app.film_run_engine import FilmRunEngine
from app.gemini_planning import PlannerResult
from app.gemini_secrets import GeminiSecretStore
from app.story_planning import StoryPlan
from test_gemini_planning import valid_plan


class FakeGeminiPlanner:
    def __init__(self, request_log):
        self.request_log = request_log

    def plan(self, request):
        self.request_log.append(request)
        return PlannerResult(StoryPlan.model_validate(valid_plan()), {
            "provider": "gemini", "model": "gemini-3.8-flash", "request_count": 1,
            "usage": {"input_tokens": 33, "output_tokens": 250, "total_tokens": 283},
            "cost_estimate": None,
        })


def setup_run(tmp_path):
    settings = Settings(_env_file=None, database_path=str(tmp_path / "db.sqlite3"),
                        allowed_path_roots=str(tmp_path), output_root=str(tmp_path / "outputs"),
                        gemini_secret_path=str(tmp_path / "gemini-secret.json"), gemini_api_key="")
    db = Database(settings)
    project = db.create_project({"id": "project-a", "name": "Private Project Name", "root": str(tmp_path),
                                 "aspect_ratio": "9:16", "frame_rate": 24, "production_type": "short"})
    request = {"prompt": "A lighthouse keeper guides a lost boat home before a storm.",
        "reference_ids": [], "reference_categories": [], "preset": "Short",
        "planner_provider": "gemini", "planner_model": "gemini-3.8-flash",
        "remote_processing_consent": {"confirmed": True, "media_uploaded": False},
        "budget": {"max_shots": 4, "max_retries_per_shot": 1, "max_generation_seconds": 3600,
                   "max_output_bytes": 8 * 1024**3},
        "generation_profile": {"frames": 49, "fps": 24, "width": 448, "height": 768},
        "authorization": {"confirmed": True, "scope": "bounded test"}}
    db.put_setting("story_planner_gemini", {"status": "connected", "models": [{"id": "gemini-3.8-flash"}], "model": "gemini-3.8-flash"})
    run = FilmOrchestrator(db).create(project["id"], "key-1", request, authorized=True)
    fake_secret = GeminiSecretStore(tmp_path / "unused.json", env={"GEMINI_API_KEY": "fake-secret-for-test"})
    return db, project, settings, request, run, fake_secret


def test_gemini_run_persists_validated_plan_and_provenance_before_checkpoint(tmp_path, monkeypatch):
    db, _project, settings, _request, run, secrets = setup_run(tmp_path)
    calls = []
    engine = FilmRunEngine(db, settings, secret_store=secrets,
                           gemini_planner_factory=lambda *_: FakeGeminiPlanner(calls), ltx_ready=lambda: True)
    result = engine.process_next()
    assert result["stage"] == "define_visual_bible"
    assert len(calls) == 1
    assert calls[0]["project_id"] is None
    serialized = json.dumps(calls[0])
    assert "Private Project Name" not in serialized
    assert "fake-secret-for-test" not in serialized
    planner_request = db.film_run_planner_request(run["id"])
    assert planner_request["status"] == "completed"
    assert planner_request["usage"]["total_tokens"] == 283
    assert planner_request["result"]["plan"]["title"] == "The Lighthouse Keeper"
    assert result["checkpoint"]["understand_story"]["planner_provenance"]["provider"] == "gemini"


def test_completed_gemini_result_is_reused_after_checkpoint_crash_and_restart(tmp_path, monkeypatch):
    db, _project, settings, _request, run, secrets = setup_run(tmp_path)
    calls = []
    engine = FilmRunEngine(db, settings, secret_store=secrets,
                           gemini_planner_factory=lambda *_: FakeGeminiPlanner(calls), ltx_ready=lambda: True)
    original_checkpoint = db.checkpoint_film_run

    def crash_after_provider_result(*args, **kwargs):
        raise RuntimeError("simulated process failure before stage checkpoint")

    monkeypatch.setattr(db, "checkpoint_film_run", crash_after_provider_result)
    blocked = engine.process_next()
    assert blocked["status"] == "needs_attention"
    assert db.film_run_planner_request(run["id"])["status"] == "completed"
    assert len(calls) == 1

    monkeypatch.setattr(db, "checkpoint_film_run", original_checkpoint)
    resumed = FilmOrchestrator(db).resume(run["id"], blocked["revision"])
    restarted_db = Database(settings)
    resumed_engine = FilmRunEngine(restarted_db, settings, secret_store=secrets,
        gemini_planner_factory=lambda *_: FakeGeminiPlanner(calls), ltx_ready=lambda: True)
    result = resumed_engine.process_next()
    assert result["stage"] == "define_visual_bible"
    assert len(calls) == 1
    assert resumed["status"] == "waiting_for_planner"


def test_interrupted_or_failed_gemini_request_is_never_repeated_until_user_resumes(tmp_path):
    db, _project, settings, _request, run, secrets = setup_run(tmp_path)
    calls = []

    class TimeoutPlanner:
        def plan(self, _request):
            calls.append("sent")
            from app.gemini_planning import GeminiPlanningError
            raise GeminiPlanningError("timeout", "Gemini outcome unknown; not resending automatically", retryable_by_user=True)

    engine = FilmRunEngine(db, settings, secret_store=secrets,
                           gemini_planner_factory=lambda *_: TimeoutPlanner(), ltx_ready=lambda: True)
    failed = engine.process_next()
    assert failed["status"] == "needs_attention"
    assert db.film_run_planner_request(run["id"], include_result=False)["status"] == "failed"
    assert len(calls) == 1
    assert engine.process_next() is None  # blocked runs are not auto-retried
    resumed = FilmOrchestrator(db).resume(run["id"], failed["revision"])
    assert db.film_run_planner_request(run["id"], include_result=False)["status"] == "retry_authorized"
    assert resumed["status"] == "waiting_for_planner"


def test_identical_active_prompt_submission_with_new_browser_key_reuses_run(tmp_path):
    db, project, _settings, request, first, _secrets = setup_run(tmp_path)
    duplicate = FilmOrchestrator(db).create(project["id"], "refreshed-browser-key", request, authorized=True)
    assert duplicate["id"] == first["id"]
    assert duplicate["created"] is False
    assert duplicate["deduplicated"] is True


def test_gemini_planner_attempts_are_capped_across_runs_per_hour(tmp_path):
    db, project, _settings, request, _run, _secrets = setup_run(tmp_path)
    orchestrator = FilmOrchestrator(db)
    for index in range(5):
        run_request = dict(request) | {"prompt": f"A unique planning story number {index}."}
        run = orchestrator.create(project["id"], f"request-{index}", run_request, authorized=True)
        claimed = db.update_film_run(run["id"], run["revision"], status="running", message="test claim")
        db.reserve_film_run_planner_request(run["id"], "gemini", "gemini-3.8-flash", f"hash-{index}")
    next_request = dict(request) | {"prompt": "A sixth unique planning story."}
    run = orchestrator.create(project["id"], "request-6", next_request, authorized=True)
    claimed = db.update_film_run(run["id"], run["revision"], status="running", message="test claim")
    import pytest
    from app.film_orchestrator import FilmRunTransitionError
    with pytest.raises(FilmRunTransitionError, match="five planning attempts per hour"):
        db.reserve_film_run_planner_request(run["id"], "gemini", "gemini-3.8-flash", "hash-6")
