import asyncio

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
        self.db.update_job(job_id, status="running", attempts=job["attempts"] + 1)
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
                payload = job["payload"]
                self.db.add_benchmark(payload["preset"], payload["frames"], payload["fps"],
                                      round(payload["frames"] / payload["fps"], 2))
            self.db.update_job(job_id, status="completed", progress=100, result_path=result)
            self.db.event(job_id, "info", "Job completed; verified output: " + result)
        except asyncio.CancelledError:
            if self.stop_event.is_set():
                self.db.update_job(job_id, status="queued", progress=0)
                self.db.event(job_id, "warning", "Worker stopped; job returned to queue")
            else:
                self.db.update_job(job_id, status="cancelled")
                self.db.event(job_id, "warning", "Job cancelled")
        except Exception as exc:
            self.db.update_job(job_id, status="failed", error=str(exc))
            self.db.event(job_id, "error", str(exc))
