import pytest

from app.config import Settings
from app.db import Database
from app.paths import safe_local_path


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
