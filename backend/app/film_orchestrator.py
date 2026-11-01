"""Durable, checkpointed film-run orchestration primitives."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
from typing import Any


STAGES = (
    "understand_story",
    "define_visual_bible",
    "plan_scenes",
    "anchor_visuals",
    "generate_shots",
    "inspect_clips",
    "creative_review",
    "repair_failed_shots",
    "compose_manifest",
    "render_film",
    "done",
)
TERMINAL_STATUSES = {"completed", "cancelled", "failed"}


class FilmRunConflict(RuntimeError):
    """The supplied idempotency key or optimistic revision conflicts with state."""


class FilmRunTransitionError(RuntimeError):
    """A film run cannot make the requested state transition."""


def request_fingerprint(request: dict[str, Any]) -> str:
    stable = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def enforce_generation_budget(run: dict[str, Any], result: dict[str, Any], stage: str) -> None:
    """Reject plans/results beyond the user's caps before the next stage can run."""
    budget = run["budget"]
    if stage == "plan_scenes":
        required = {"shot_count"}
        if not required.issubset(result):
            raise FilmRunTransitionError("Scene planner omitted the shot count")
        if int(result["shot_count"]) < 1:
            raise FilmRunTransitionError("Scene planner returned an invalid budget estimate")
        if int(result["shot_count"]) > budget["max_shots"]:
            raise FilmRunTransitionError("Scene plan exceeds the approved maximum shot count")
        if result.get("estimated_generation_seconds") is not None and int(result["estimated_generation_seconds"]) < 0:
            raise FilmRunTransitionError("Scene planner returned an invalid generation-time estimate")
        if result.get("estimated_output_bytes") is not None and int(result["estimated_output_bytes"]) < 0:
            raise FilmRunTransitionError("Scene planner returned an invalid output-size estimate")
        if result.get("estimated_generation_seconds") is not None and int(result["estimated_generation_seconds"]) > budget["max_generation_seconds"]:
            raise FilmRunTransitionError("Scene plan exceeds the approved generation time budget")
        if result.get("estimated_output_bytes") is not None and int(result["estimated_output_bytes"]) > budget["max_output_bytes"]:
            raise FilmRunTransitionError("Scene plan exceeds the approved output storage budget")
    elif stage == "generate_shots":
        required = {"generated_shots", "elapsed_seconds", "output_bytes"}
        if not required.issubset(result):
            raise FilmRunTransitionError("Generation handler omitted a required budget measurement")
        if int(result["generated_shots"]) < 0 or int(result["elapsed_seconds"]) < 0 or int(result["output_bytes"]) < 0:
            raise FilmRunTransitionError("Generation handler returned an invalid budget measurement")
        if int(result["generated_shots"]) > budget["max_shots"]:
            raise FilmRunTransitionError("Generation result exceeds the approved maximum shot count")
        if int(result.get("elapsed_seconds", 0)) > budget["max_generation_seconds"]:
            raise FilmRunTransitionError("Generation result exceeded the approved time budget")
        if int(result.get("output_bytes", 0)) > budget["max_output_bytes"]:
            raise FilmRunTransitionError("Generation result exceeded the approved output storage budget")
    elif stage == "repair_failed_shots":
        attempts = result.get("retry_attempts")
        if not isinstance(attempts, dict):
            raise FilmRunTransitionError("Repair stage omitted per-shot retry counts")
        if any(int(value) > budget["max_retries_per_shot"] for value in attempts.values()):
            raise FilmRunTransitionError("Repair attempts exceed the approved per-shot retry budget")


class FilmOrchestrator:
    """Persist run lifecycle transitions and low-level deterministic stages.

    The application worker uses FilmRunEngine for production stages. This
    handler-injection method remains for isolated unit tests; HTTP callers
    cannot supply handlers.
    """

    def __init__(self, database):
        self.db = database

    def create(self, project_id: str, idempotency_key: str, request: dict[str, Any], *, authorized: bool):
        if not idempotency_key or len(idempotency_key) > 128:
            raise ValueError("A non-empty idempotency key of at most 128 characters is required")
        if not request.get("prompt", "").strip():
            raise ValueError("A story prompt is required")
        status = "waiting_for_planner" if authorized else "awaiting_authorization"
        try:
            return self.db.create_film_run(project_id, idempotency_key, request_fingerprint(request),
                                           request, status=status, stage=STAGES[0])
        except ValueError as exc:
            if "Idempotency key" in str(exc):
                raise FilmRunConflict(str(exc)) from exc
            raise

    def get(self, run_id: str):
        run = self.db.get_film_run(run_id)
        if not run:
            raise KeyError("Film run not found")
        return run

    def list(self, project_id: str):
        return self.db.list_film_runs(project_id)

    def authorize(self, run_id: str, expected_revision: int, confirmation: dict[str, Any]):
        run = self.get(run_id)
        if run["status"] != "awaiting_authorization":
            raise FilmRunTransitionError("This run is not awaiting initial authorization")
        if not confirmation.get("confirmed"):
            raise FilmRunTransitionError("Initial authorization must be explicit")
        return self.db.update_film_run(run_id, expected_revision, status="waiting_for_planner",
                                       authorization=confirmation, message="Initial run scope authorized; planner capability check is next.")

    def pause(self, run_id: str, expected_revision: int):
        run = self.get(run_id)
        if run["status"] not in {"waiting_for_planner", "running"}:
            raise FilmRunTransitionError("Only an active film run can be paused")
        updated = self.db.update_film_run(run_id, expected_revision, status="paused", message="Paused at a durable checkpoint.")
        self.db.transition_film_run_batch(run_id, "pause")
        return updated

    def resume(self, run_id: str, expected_revision: int):
        run = self.get(run_id)
        if run["status"] not in {"paused", "needs_attention"}:
            raise FilmRunTransitionError("Only a paused or blocked film run can be resumed")
        if run["stage"] == "understand_story":
            # Resume is a user action. A failed or interrupted Gemini request
            # may have incurred a charge; only this explicit action authorizes
            # a new request. Completed output remains cached and is reused.
            self.db.authorize_film_run_planner_retry(run_id)
        updated = self.db.update_film_run(run_id, expected_revision, status="waiting_for_planner", message="Resumed from the last durable checkpoint.")
        self.db.transition_film_run_batch(run_id, "resume")
        return updated

    def replan_with_gemini(self, run_id: str, expected_revision: int, model: str,
                           idempotency_key: str, scope_hash: str, schema_version: str,
                           consent: dict[str, Any]):
        return self.db.create_film_run_planner_revision(run_id, expected_revision, model,
            idempotency_key, scope_hash, schema_version, consent)

    def cancel(self, run_id: str, expected_revision: int):
        run = self.get(run_id)
        if run["status"] in TERMINAL_STATUSES:
            raise FilmRunTransitionError("This film run is already terminal")
        updated = self.db.update_film_run(run_id, expected_revision, status="cancelled", message="Film run cancelled; existing files and jobs are preserved.")
        self.db.transition_film_run_batch(run_id, "cancel")
        return updated

    def process_one(self, run_id: str, handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None):
        """Run one injected stage, persisting a checkpoint before returning."""
        run = self.get(run_id)
        if run["status"] != "waiting_for_planner":
            raise FilmRunTransitionError("Film run is not ready for a stage")
        stage = run["stage"]
        if stage == "done":
            raise FilmRunTransitionError("Film run is already complete")
        if handler is None:
            return self.db.update_film_run(run_id, run["revision"], status="needs_attention",
                                           error=f"No verified production handler is configured for stage: {stage}",
                                           message=f"Stage {stage} needs an implemented capability; no work was started.")
        claimed = self.db.update_film_run(run_id, run["revision"], status="running", message=f"Started checkpointed stage: {stage}.")
        stage_key = hashlib.sha256(f"{run_id}:{stage}".encode("utf-8")).hexdigest()
        claimed_for_handler = claimed | {"stage_idempotency_key": stage_key}
        try:
            result = handler(stage, claimed_for_handler)
            if not isinstance(result, dict):
                raise TypeError("A stage handler must return a checkpoint object")
            # Planning and execution results are checked against the approved hard caps.
            if stage in {"plan_scenes", "generate_shots", "repair_failed_shots"}:
                enforce_generation_budget(claimed, result, stage)
            # Fail before persistence if the handler returned non-JSON state.
            json.dumps(result, ensure_ascii=False)
            next_index = STAGES.index(stage) + 1
            next_stage = STAGES[next_index]
            next_status = "completed" if next_stage == "done" else "waiting_for_planner"
            return self.db.checkpoint_film_run(run_id, claimed["revision"], stage, next_stage,
                                               result, status=next_status,
                                               message=f"Checkpoint saved for {stage}.")
        except Exception as exc:
            current = self.get(run_id)
            if current["status"] == "running":
                return self.db.update_film_run(run_id, current["revision"], status="failed",
                                               error=str(exc)[:2000], message=f"Stage {stage} failed; resume is available from the last checkpoint.")
            raise
