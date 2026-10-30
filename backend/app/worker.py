import asyncio
import os
import time
from pathlib import Path

from .adapters import LTXAdapter, RemotionAdapter
from .config import Settings
from .db import Database


class LocalWorker:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self.adapter = LTXAdapter(settings)
        self.remotion = RemotionAdapter(settings)
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
            pending = next((j for j in reversed(self.db.list_jobs()) if j["status"] == "queued"), None)
            if pending:
                self.active_job_id = pending["id"]
                self.active_task = asyncio.create_task(self.execute(pending))
                try:
                    await self.active_task
                finally:
                    self.active_task = None
                    self.active_job_id = None
            else:
                await asyncio.sleep(0.5)

    async def execute(self, job: dict):
        job_id = job["id"]
        if not self.db.claim_job(job_id):
            return
        started = time.monotonic()
        self.db.event(job_id, "info", "Single local worker started job")
        async def progress(value, message=None):
            current = self.db.get_job(job_id)
            if current and current["status"] == "cancelled":
                raise asyncio.CancelledError()
            self.db.update_job(job_id, progress=value)
            if message:
                self.db.event(job_id, "info", message)
        try:
            if job["kind"] == "render":
                result = await self.remotion.render(job["payload"], progress)
            else:
                result = await self.adapter.generate(job["payload"], progress)
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
                                   exit_code=None, partial_output_path=partial if partial and partial.exists() else None)
                suffix = f" Partial output: {partial}" if partial and partial.exists() else " No partial output was retained."
                self.db.event(job_id, "warning", "Job cancelled; child processes in its isolated process group were stopped." + suffix)
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
