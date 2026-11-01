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


def test_existing_http_400_failed_run_recovers_only_after_explicit_resume(tmp_path):
    from app.gemini_planning import GeminiPlanningError

    db, _project, settings, _request, run, secrets = setup_run(tmp_path)
    calls = []
    outcomes = [
        GeminiPlanningError("invalid_request", "Gemini rejected the structured StoryPlan request safely.", retryable_by_user=True),
        PlannerResult(StoryPlan.model_validate(valid_plan()), {"provider": "gemini", "model": "gemini-3.8-flash",
            "request_count": 1, "usage": {"total_tokens": 100}, "cost_estimate": None}),
    ]

    class RecoveringPlanner:
        def plan(self, request):
            calls.append(request)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    engine = FilmRunEngine(db, settings, secret_store=secrets,
        gemini_planner_factory=lambda *_: RecoveringPlanner(), ltx_ready=lambda: True)
    failed = engine.process_next()
    assert failed["status"] == "needs_attention"
    assert failed["error"] == "Gemini rejected the structured StoryPlan request safely."
    assert db.film_run_planner_request(run["id"], include_result=False)["status"] == "failed"
    assert db.list_jobs(project_id="project-a") == []
    assert engine.process_next() is None
    assert len(calls) == 1

    resumed = FilmOrchestrator(db).resume(run["id"], failed["revision"])
    assert resumed["status"] == "waiting_for_planner"
    recovered = engine.process_next()
    assert recovered["stage"] == "define_visual_bible"
    ledger = db.film_run_planner_request(run["id"])
    assert ledger["status"] == "completed"
    assert ledger["attempts"] == 2
    assert ledger["result"]["plan"]["title"] == "The Lighthouse Keeper"
    assert len(calls) == 2
    assert db.list_jobs(project_id="project-a") == []


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
    with pytest.raises(FilmRunTransitionError, match="five attempts per hour"):
        db.reserve_film_run_planner_request(run["id"], "gemini", "gemini-3.8-flash", "hash-6")


def test_exhausted_model_can_switch_on_same_run_with_a_separate_attempt_and_restart(tmp_path):
    import pytest
    from app.film_orchestrator import FilmRunTransitionError
    from app.gemini_planning import GeminiPlanningError, STORY_PLAN_SCHEMA_VERSION

    db, project, settings, _request, run, secrets = setup_run(tmp_path)
    db.put_setting("story_planner_gemini", {"status": "connected", "models": [
        {"id": "gemini-3.8-flash"}, {"id": "gemini-2.5-flash"}], "model": "gemini-3.8-flash"})
    calls = []

    class FailingPlanner:
        def plan(self, request):
            calls.append(("old", request))
            raise GeminiPlanningError("invalid_request", "Safe structured request error", retryable_by_user=True)

    failing = FilmRunEngine(db, settings, secret_store=secrets,
        gemini_planner_factory=lambda *_: FailingPlanner(), ltx_ready=lambda: True)
    for attempt in range(3):
        failed = failing.process_next()
        assert failed["status"] == "needs_attention"
        if attempt < 2:
            resumed = FilmOrchestrator(db).resume(run["id"], failed["revision"])
            assert resumed["status"] == "waiting_for_planner"
    assert len(calls) == 3
    old_attempt = db.film_run_planner_request(run["id"], revision=1, include_result=False)
    assert old_attempt["attempts"] == 3 and old_attempt["status"] == "failed"
    with pytest.raises(FilmRunTransitionError, match="all three attempts"):
        FilmOrchestrator(db).resume(run["id"], failed["revision"])

    consent = {"confirmed": True, "model": "gemini-2.5-flash", "media_uploaded": False}
    orchestrator = FilmOrchestrator(db)
    switched = orchestrator.replan_with_gemini(run["id"], failed["revision"], "gemini-2.5-flash",
        "switch-model-key", "same-scope-hash", STORY_PLAN_SCHEMA_VERSION, consent)
    assert switched["id"] == run["id"]
    assert switched["request"]["planner_model"] == "gemini-2.5-flash"
    assert switched["budget"] == run["budget"]
    assert db.film_run_planner_request(run["id"], revision=1, include_result=False)["attempts"] == 3
    new_attempt = db.film_run_planner_request(run["id"], revision=2, include_result=False)
    assert new_attempt["status"] == "retry_authorized" and new_attempt["attempts"] == 0
    assert new_attempt["model"] == "gemini-2.5-flash"
    assert new_attempt["schema_version"] == STORY_PLAN_SCHEMA_VERSION
    assert new_attempt["consent"]["confirmed"] is True
    duplicate = orchestrator.replan_with_gemini(run["id"], failed["revision"], "gemini-2.5-flash",
        "switch-model-key", "same-scope-hash", STORY_PLAN_SCHEMA_VERSION, consent)
    assert duplicate["replan_deduplicated"] is True
    assert len(db.film_run_planner_attempts(run["id"])) == 2

    class NewModelPlanner:
        def plan(self, request):
            calls.append(("new", request))
            return PlannerResult(StoryPlan.model_validate(valid_plan()), {"provider": "gemini",
                "model": "gemini-2.5-flash", "request_count": 1, "usage": {"total_tokens": 88}})

    restarted_db = Database(settings)
    recovered = FilmRunEngine(restarted_db, settings, secret_store=secrets,
        gemini_planner_factory=lambda _key, model, _timeout: NewModelPlanner() if model == "gemini-2.5-flash" else FailingPlanner(),
        ltx_ready=lambda: True).process_next()
    assert recovered["stage"] == "define_visual_bible"
    assert calls[-1][0] == "new"
    assert restarted_db.film_run_planner_request(run["id"], revision=2)["status"] == "completed"
    assert restarted_db.film_run_planner_attempts(run["id"])[0]["attempts"] == 3
    assert restarted_db.film_run_planner_attempts(run["id"])[1]["attempts"] == 1
    assert restarted_db.list_jobs(project_id=project["id"]) == []


def test_replan_requires_explicit_consent_and_is_limited_to_verified_account_models(tmp_path):
    import pytest
    from app.film_orchestrator import FilmRunTransitionError
    from app.gemini_planning import STORY_PLAN_SCHEMA_VERSION

    db, _project, _settings, _request, run, _secrets = setup_run(tmp_path)
    blocked = db.update_film_run(run["id"], run["revision"], status="needs_attention",
        message="simulated failed planning", error="safe diagnostic")
    db.fail_film_run_planner_request(run["id"], "invalid_request")
    orchestrator = FilmOrchestrator(db)
    with pytest.raises(FilmRunTransitionError, match="Explicit consent"):
        orchestrator.replan_with_gemini(run["id"], blocked["revision"], "gemini-unlisted",
            "no-consent", "same-scope", STORY_PLAN_SCHEMA_VERSION, {"confirmed": False})
    assert db.film_run_planner_attempts(run["id"]) == []


def test_replan_http_route_requires_consent_and_account_listed_model_without_sending_request(tmp_path, monkeypatch):
    import pytest
    from fastapi import HTTPException
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "api-global-test.sqlite3"))
    monkeypatch.setenv("ALLOWED_PATH_ROOTS", str(tmp_path))
    from app.config import get_settings
    get_settings.cache_clear()
    from app import main as api
    from app.gemini_planning import GeminiPlanningError
    assert api.FilmRunBudget().max_shots == 1

    db, _project, settings, _request, run, secrets = setup_run(tmp_path)
    db.put_setting("story_planner_gemini", {"status": "connected", "models": [
        {"id": "gemini-3.8-flash", "display_name": "Flash"},
        {"id": "gemini-2.5-flash", "display_name": "Flash 2.5"}], "model": "gemini-3.8-flash"})
    class Failed:
        def plan(self, _request):
            raise GeminiPlanningError("invalid_request", "safe", retryable_by_user=True)
    failed = FilmRunEngine(db, settings, secret_store=secrets,
        gemini_planner_factory=lambda *_: Failed(), ltx_ready=lambda: True).process_next()
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api, "film_orchestrator", FilmOrchestrator(db))
    monkeypatch.setattr(api, "gemini_secret_store", GeminiSecretStore(tmp_path / "unused.json", env={"GEMINI_API_KEY": "fake-key"}))

    with pytest.raises(HTTPException) as no_consent:
        api.replan_film_run(run["id"], api.GeminiReplanRequest(expected_revision=failed["revision"],
            planner_model="gemini-2.5-flash", idempotency_key="consent", confirm_billable_planning=False))
    assert no_consent.value.status_code == 422
    with pytest.raises(HTTPException) as unlisted:
        api.replan_film_run(run["id"], api.GeminiReplanRequest(expected_revision=failed["revision"],
            planner_model="gemini-not-in-account", idempotency_key="unlisted", confirm_billable_planning=True))
    assert unlisted.value.status_code == 400
    accepted = api.replan_film_run(run["id"], api.GeminiReplanRequest(expected_revision=failed["revision"],
        planner_model="gemini-2.5-flash", idempotency_key="accepted", confirm_billable_planning=True))
    assert accepted["id"] == run["id"]
    assert accepted["request"]["planner_model"] == "gemini-2.5-flash"
    assert accepted["status"] == "waiting_for_planner"
    assert db.film_run_planner_request(run["id"], revision=2)["attempts"] == 0


def test_schema_change_creates_a_distinct_revision_without_resetting_prior_ledger(tmp_path):
    from app.gemini_planning import STORY_PLAN_SCHEMA_VERSION

    db, _project, _settings, _request, run, _secrets = setup_run(tmp_path)
    claimed = db.update_film_run(run["id"], run["revision"], status="running", message="claim for schema test")
    db.reserve_film_run_planner_request(run["id"], "gemini", "gemini-3.8-flash", "schema-one",
        revision=1, schema_version=STORY_PLAN_SCHEMA_VERSION)
    db.fail_film_run_planner_request(run["id"], "invalid_request", revision=1)
    blocked = db.update_film_run(run["id"], claimed["revision"], status="needs_attention",
        message="schema rejected", error="safe schema diagnostic")
    revision = FilmOrchestrator(db).replan_with_gemini(run["id"], blocked["revision"], "gemini-3.8-flash",
        "schema-revision-key", "same-scope", "storyplan-schema-revision-test",
        {"confirmed": True, "model": "gemini-3.8-flash", "media_uploaded": False})
    history = db.film_run_planner_attempts(run["id"], include_result=False)
    assert revision["request"]["planner_schema_version"] == "storyplan-schema-revision-test"
    assert [(item["revision"], item["schema_version"], item["attempts"]) for item in history] == [
        (1, STORY_PLAN_SCHEMA_VERSION, 1), (2, "storyplan-schema-revision-test", 0)]
