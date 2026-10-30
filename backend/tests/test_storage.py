import pytest
import asyncio

from app.config import Settings
from app.db import Database
from app.paths import safe_local_path
from app.worker import LocalWorker


def test_path_validation_rejects_paths_outside_allowed_root(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    settings = Settings(_env_file=None, allowed_path_roots=str(root))
    with pytest.raises(ValueError, match="ALLOWED_PATH_ROOTS"):
        safe_local_path(str(tmp_path / "outside.mp4"), settings)


def test_running_job_returns_to_queue_after_restart(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "studio.sqlite3"),
        allowed_path_roots=str(tmp_path),
    )
    first = Database(settings)
    job = first.create_job("generation", {"prompt": "offline test"})
    first.update_job(job["id"], status="running")

    restarted = Database(settings)
    restored = restarted.get_job(job["id"])
    assert restored["status"] == "queued"
    assert restored["payload"]["prompt"] == "offline test"


def test_cancel_wins_against_queue_claim_and_completion(tmp_path):
    settings = Settings(_env_file=None, database_path=str(tmp_path / "studio.sqlite3"),
                        allowed_path_roots=str(tmp_path))
    db = Database(settings)
    job = db.create_job("generation", {"prompt": "offline state test"})
    assert db.cancel_job(job["id"])["status"] == "cancelled"
    assert db.claim_job(job["id"]) is False
    assert db.complete_job(job["id"], result_path="/tmp/never.mp4") is False
    assert db.get_job(job["id"])["status"] == "cancelled"


def test_worker_persists_adapter_failure_without_gpu_or_subprocess(tmp_path):
    async def run():
        settings = Settings(_env_file=None, database_path=str(tmp_path / "studio.sqlite3"),
                            allowed_path_roots=str(tmp_path))
        db = Database(settings)
        job = db.create_job("generation", {"prompt": "offline failure fixture", "output_location": str(tmp_path)})

        class FailingAdapter:
            async def generate(self, payload, on_progress):
                raise RuntimeError("simulated runner failure")

        worker = LocalWorker(db, settings)
        worker.adapter = FailingAdapter()
        await worker.execute(job)
        failed = db.get_job(job["id"])
        assert failed["status"] == "failed"
        assert failed["error"] == "simulated runner failure"
        assert db.events(job["id"])[-1]["level"] == "error"

    asyncio.run(run())
