import asyncio
import json
import os
import time
from pathlib import Path

from .adapters import LTXAdapter, RemotionAdapter
from .config import Settings
from .db import Database
from .storyboards import build_composition_manifest
from .paths import safe_local_path
from .film_run_engine import FilmRunEngine
from .gemini_secrets import GeminiSecretStore


class LocalWorker:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self.adapter = LTXAdapter(settings)
        self.remotion = RemotionAdapter(settings)
        self.film_runs = FilmRunEngine(db, settings, secret_store=GeminiSecretStore(
            settings.expanded(settings.gemini_secret_path), env={"GEMINI_API_KEY": settings.gemini_api_key}))
        self.task = None
        self.active_task = None
        self.active_job_id = None
        self.stop_event = asyncio.Event()

    async def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.stop_event.set()
        if self.active_task and not self.active_task.done():
            self.active_task.cancel()
        if self.task:
            await self.task

    def cancel_job(self, job_id: str):
        if self.active_job_id == job_id and self.active_task and not self.active_task.done():
            self.active_task.cancel()

    async def run(self):
        while not self.stop_event.is_set():
            pending = self.db.next_queued_job()
            if pending:
                budget = self.db.film_run_budget_for_job(pending["id"])
                if budget and (budget["remaining_seconds"] <= 0 or budget["output_bytes"] >= budget["max_output_bytes"]):
                    reason = ("Film run generation-time budget was exhausted; remaining queued shots were stopped."
                              if budget["remaining_seconds"] <= 0 else
                              "Film run output-storage budget was reached; remaining queued shots were stopped.")
                    self.db.stop_film_run_queue_for_budget(budget["run_id"], reason)
                    continue
                self.active_job_id = pending["id"]
                self.active_task = asyncio.create_task(self.execute(pending))
                try:
                    await self.active_task
                finally:
                    self.active_task = None
                    self.active_job_id = None
            else:
                # Film orchestration shares this process with the existing
                # single generation worker. It only creates queue records;
                # the normal worker remains the only LTX process runner.
                await asyncio.to_thread(self.film_runs.process_next)
                await asyncio.sleep(0.5)

    async def execute(self, job: dict):
        job_id = job["id"]
        if not self.db.claim_job(job_id):
            return
        started = time.monotonic()
        run_budget = self.db.film_run_budget_for_job(job_id)
        budget_output_exceeded = False
        if run_budget and (run_budget["remaining_seconds"] <= 0 or run_budget["output_bytes"] >= run_budget["max_output_bytes"]):
            reason = "Film run budget reached before this shot started; no inference was launched."
            self.db.update_job(job_id, status="cancelled", error=reason)
            self.db.event(job_id, "warning", reason)
            self.db.stop_film_run_queue_for_budget(run_budget["run_id"], reason)
            return
        self.db.event(job_id, "info", "Single local worker started job")
        async def progress(value, message=None):
            nonlocal budget_output_exceeded
            current = self.db.get_job(job_id)
            if current and current["status"] == "cancelled":
                raise asyncio.CancelledError()
            if run_budget and job["kind"] == "generation":
                partial = self._partial_path(job)
                try:
                    partial_bytes = partial.stat().st_size
                except OSError:
                    partial_bytes = 0
                if run_budget["output_bytes"] + partial_bytes >= run_budget["max_output_bytes"]:
                    budget_output_exceeded = True
                    raise asyncio.CancelledError()
            self.db.update_job(job_id, progress=value)
            if message:
                self.db.event(job_id, "info", message)
        try:
            if job["kind"] == "render":
                result = await self.remotion.render(job["payload"], progress)
            else:
                generation = self.adapter.generate(job["payload"], progress)
                result = (await asyncio.wait_for(generation, timeout=run_budget["remaining_seconds"])
                          if run_budget else await generation)
            result_path = result["output_path"] if isinstance(result, dict) else result
            metadata = result.get("metadata") if isinstance(result, dict) else None
            exit_code = result.get("exit_code", 0) if isinstance(result, dict) else 0
            completed = self.db.complete_job(
                job_id, progress=100, result_path=result_path, exit_code=exit_code,
                elapsed_seconds=round(time.monotonic() - started, 2), verification_metadata=metadata,
            )
            if not completed:
                self._mark_partial_if_present(job, result_path)
                self.db.event(job_id, "warning", "Cancellation won the completion race; output was not marked completed")
                return
            if job["kind"] != "render":
                payload = job["payload"]
                try:
                    self.db.add_benchmark(payload["preset"], payload["frames"], payload["fps"],
                                          round(payload["frames"] / payload["fps"], 2),
                                          elapsed_seconds=round(time.monotonic() - started, 2))
                except Exception as exc:
                    self.db.event(job_id, "warning", "Video completed, but benchmark history could not be updated: " + str(exc))
            self.db.event(job_id, "info", "Job completed; verified output: " + result_path)
            if run_budget:
                updated_budget = self.db.film_run_budget_for_job(job_id)
                if updated_budget and updated_budget["output_bytes"] > updated_budget["max_output_bytes"]:
                    self.db.stop_film_run_queue_for_budget(updated_budget["run_id"],
                        "A verified clip exceeded the approved aggregate output-storage cap; remaining queued shots were stopped and the clip was preserved.")
            if job.get("payload", {}).get("film_batch_id"):
                self._assemble_completed_film(job)
        except asyncio.CancelledError:
            partial = self._partial_path(job)
            current = self.db.get_job(job_id)
            if self.stop_event.is_set() and current and current["status"] != "cancelled":
                self.db.update_job(job_id, status="queued", progress=0,
                                   elapsed_seconds=round(time.monotonic() - started, 2),
                                   partial_output_path=partial if partial and partial.exists() else None)
                self.db.event(job_id, "warning", "Worker stopped; job returned to queue")
            else:
                if current and current["status"] != "cancelled":
                    self.db.update_job(job_id, status="cancelled")
                self.db.update_job(job_id, elapsed_seconds=round(time.monotonic() - started, 2),
                                   exit_code=None, partial_output_path=str(partial) if partial and partial.exists() else None)
                suffix = f" Partial output: {partial}" if partial and partial.exists() else " No partial output was retained."
                message = ("Film run output-storage budget reached during this shot; child processes were stopped."
                           if budget_output_exceeded else
                           "Job cancelled; child processes in its isolated process group were stopped.")
                self.db.event(job_id, "warning", message + suffix)
                if budget_output_exceeded and run_budget:
                    self.db.stop_film_run_queue_for_budget(run_budget["run_id"], message)
        except Exception as exc:
            partial = getattr(exc, "partial_output", None)
            if not partial:
                candidate = self._partial_path(job)
                partial = str(candidate) if candidate and candidate.exists() else None
            failed = self.db.fail_job(job_id, error=str(exc), exit_code=getattr(exc, "exit_code", None),
                                      elapsed_seconds=round(time.monotonic() - started, 2),
                                      partial_output_path=partial)
            if failed:
                self.db.event(job_id, "error", str(exc))
            else:
                self.db.event(job_id, "warning", "Cancellation won the failure race; job remains cancelled.")
            if partial:
                self.db.event(job_id, "warning", "Partial output retained and clearly named: " + partial)
            if run_budget and isinstance(exc, asyncio.TimeoutError):
                self.db.stop_film_run_queue_for_budget(run_budget["run_id"],
                    "The approved aggregate generation-time budget expired during a shot; remaining queued shots were stopped.")

    def _partial_path(self, job: dict) -> Path | None:
        payload = job["payload"]
        if job["kind"] == "render":
            output = Path(payload["output_path"]).expanduser()
            return output.with_name(output.stem + ".partial" + output.suffix)
        output_dir = Path(payload["output_location"]).expanduser()
        return output_dir / (job["id"] + ".partial.mp4")

    def _mark_partial_if_present(self, job: dict, result_path: str) -> None:
        output = Path(result_path)
        cancelled = output.with_name(output.stem + ".cancelled" + output.suffix)
        if output.exists():
            os.replace(output, cancelled)
            self.db.update_job(job["id"], partial_output_path=str(cancelled))

    def _assemble_completed_film(self, job: dict) -> None:
        for batch in self.db.list_film_batches_for_job(job["id"]):
            if batch["status"] != "completed":
                continue
            if batch.get("film_run_id"):
                # Prompt-first runs stop at explicit creative review. Do not
                # prepare a Remotion manifest for unreviewed clips.
                continue
            try:
                storyboard = self.db.get_storyboard(batch["storyboard_id"], include_events=False)
                if not storyboard:
                    raise ValueError("Storyboard was archived before composition assembly")
                output_root = safe_local_path(self.settings.output_root, self.settings)
                manifest = build_composition_manifest(storyboard, output_root)
                directory = safe_local_path(str(output_root / "storyboards" / storyboard["id"]), self.settings)
                directory.mkdir(parents=True, exist_ok=True)
                manifest_path = directory / "composition-manifest.json"
                temporary = manifest_path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
                temporary.replace(manifest_path)
                self.db.store_storyboard_manifest(storyboard["id"], manifest, str(manifest_path))
                self.db.event(job["id"], "info", "All film shots are verified; deterministic Remotion composition manifest is ready")
            except Exception as exc:
                self.db.event(job["id"], "warning", "All shots completed, but automatic composition assembly needs attention: " + str(exc))
