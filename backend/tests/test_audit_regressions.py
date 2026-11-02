import asyncio
import os
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.gemini_secrets import GeminiSecretStore, SecretStoreError
from app.setup_service import _first_line
from app.worker import LocalWorker


def _settings(tmp_path: Path) -> Settings:
    return Settings(database_path=str(tmp_path / "studio.sqlite3"), allowed_path_roots=str(tmp_path),
                    output_root=str(tmp_path / "outputs"))


def test_worker_loop_survives_a_failing_idle_tick(tmp_path):
    settings = _settings(tmp_path)
    worker = LocalWorker(Database(settings), settings)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Film run changed; refresh before updating it")

    worker.film_runs.process_next = flaky

    async def scenario():
        await worker.start()
        await asyncio.sleep(2)
        alive = not worker.task.done()
        await worker.stop()
        return alive

    assert asyncio.run(scenario()) and len(calls) > 1


def test_shutdown_during_generation_with_partial_output_requeues_job(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings)
    worker = LocalWorker(db, settings)
    worker.film_runs.process_next = lambda: None
    job = db.create_job("generation", {"prompt": "x", "preset": "Short", "frames": 49, "fps": 24,
                                       "output_location": settings.output_root}, project_id=None)

    async def slow_generate(payload, progress):
        Path(settings.output_root).mkdir(parents=True, exist_ok=True)
        (Path(settings.output_root) / f"{job['id']}.partial.mp4").write_bytes(b"partial")
        await progress(5)
        await asyncio.sleep(30)

    worker.adapter.generate = slow_generate

    async def scenario():
        await worker.start()
        await asyncio.sleep(1)
        await worker.stop()

    asyncio.run(scenario())
    stored = db.get_job(job["id"])
    assert stored["status"] == "queued" and stored["partial_output_path"].endswith(".partial.mp4")


def test_setup_version_line_tolerates_empty_output():
    assert _first_line("") == "Version unavailable"


def test_non_utf8_secret_file_is_reported_as_malformed(tmp_path):
    path = tmp_path / "gemini.json"
    path.write_bytes(b"\xff\xfe\x00")
    os.chmod(path, 0o600)
    with pytest.raises(SecretStoreError):
        GeminiSecretStore(path, env={}).get()
