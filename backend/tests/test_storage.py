import pytest
import asyncio
import sqlite3

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


def test_running_job_is_failed_safely_after_restart(tmp_path):
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
    assert restored["status"] == "failed"
    assert restored["payload"]["prompt"] == "offline test"
    assert "review and retry manually" in restored["error"]
    assert restarted.events(job["id"])[-1]["level"] == "warning"


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


def test_legacy_schema_migration_is_backed_up_additive_and_idempotent(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as legacy:
        legacy.executescript("""
            CREATE TABLE jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL,progress INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL,result_path TEXT,error TEXT,attempts INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL,time TEXT NOT NULL,level TEXT NOT NULL,message TEXT NOT NULL);
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE benchmark_history(id INTEGER PRIMARY KEY AUTOINCREMENT,preset TEXT NOT NULL,frames INTEGER NOT NULL,fps INTEGER NOT NULL,duration REAL NOT NULL,elapsed_seconds REAL,created_at TEXT NOT NULL);
            CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,root TEXT NOT NULL,created_at TEXT NOT NULL,imported_at TEXT NOT NULL);
            CREATE TABLE project_assets(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,category TEXT NOT NULL,relative_path TEXT NOT NULL,size_bytes INTEGER NOT NULL,modified_at REAL NOT NULL,mime_type TEXT NOT NULL);
            CREATE TABLE storyboards(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,title TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,archived_at TEXT,manifest_json TEXT,manifest_path TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE storyboard_shots(id TEXT PRIMARY KEY,storyboard_id TEXT NOT NULL,position INTEGER NOT NULL,title TEXT NOT NULL,prompt TEXT NOT NULL,reference_image TEXT,preset TEXT NOT NULL,seed INTEGER,caption TEXT NOT NULL DEFAULT '',deleted_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE storyboard_scenes(id TEXT PRIMARY KEY,storyboard_id TEXT NOT NULL,title TEXT NOT NULL,position INTEGER NOT NULL,deleted_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(storyboard_id,title));
            CREATE TABLE storyboard_revisions(id INTEGER PRIMARY KEY AUTOINCREMENT,storyboard_id TEXT NOT NULL,revision INTEGER NOT NULL,title TEXT NOT NULL,snapshot_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(storyboard_id,revision));
            CREATE TABLE shot_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,shot_id TEXT NOT NULL,job_id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL);
            INSERT INTO projects VALUES('legacy','Legacy project','/legacy','2026-01-01','2026-01-01');
            INSERT INTO project_assets VALUES('asset','legacy','character_reference','frog.png',12,1.0,'image/png');
            INSERT INTO jobs(id,kind,status,payload,created_at,updated_at) VALUES('job','generation','completed','{\"prompt\":\"historical\"}','2026-01-01','2026-01-01');
        """)
    settings = Settings(_env_file=None, database_path=str(path), allowed_path_roots=str(tmp_path))
    migrated = Database(settings)
    assert migrated.get_job("job")["payload"]["prompt"] == "historical"
    assert migrated.get_project_asset("legacy", "asset")["relative_path"] == "frog.png"
    with migrated.connect() as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 10
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"project_scene_continuity", "project_scene_continuity_revisions", "shot_creative_reviews", "film_runs", "film_run_events", "film_run_shots", "film_run_shot_attempts", "film_run_planner_requests", "film_run_planner_attempts"}.issubset(tables)
        assert "project_id" in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        assert "film_run_id" in {row[1] for row in db.execute("PRAGMA table_info(film_plans)")}
        assert "film_run_id" in {row[1] for row in db.execute("PRAGMA table_info(film_batches)")}
    backups = list(tmp_path.glob("legacy.sqlite3.pre-migration-*.bak"))
    assert len(backups) == 1
    Database(settings)
    assert len(list(tmp_path.glob("legacy.sqlite3.pre-migration-*.bak"))) == 1


def test_planner_attempt_history_survives_additive_v10_migration(tmp_path):
    from app.film_orchestrator import FilmOrchestrator

    path = tmp_path / "planner-history.sqlite3"
    settings = Settings(_env_file=None, database_path=str(path), allowed_path_roots=str(tmp_path))
    db = Database(settings)
    project = db.create_project({"id": "planner-project", "name": "Planner test", "root": str(tmp_path),
        "aspect_ratio": "9:16", "frame_rate": 24, "production_type": "short"})
    request = {"prompt": "A fox follows a moonlit path.", "reference_ids": [], "preset": "Short",
        "planner_provider": "gemini", "planner_model": "gemini-2.5-flash",
        "remote_processing_consent": {"confirmed": True},
        "authorization": {"confirmed": True},
        "budget": {"max_shots": 1, "max_retries_per_shot": 0, "max_generation_seconds": 60, "max_output_bytes": 1024}}
    run = FilmOrchestrator(db).create(project["id"], "planner-history-key", request, authorized=True)
    claimed = db.update_film_run(run["id"], run["revision"], status="running", message="claim planning")
    db.reserve_film_run_planner_request(run["id"], "gemini", "gemini-2.5-flash", "before-v10",
        schema_version="storyplan-budgeted-v1")
    db.fail_film_run_planner_request(run["id"], "schema_rejected", error_details={"http_status": 400})
    with db.connect() as connection:
        connection.execute("ALTER TABLE film_run_planner_attempts DROP COLUMN response_mode")
        connection.execute("ALTER TABLE film_run_planner_attempts DROP COLUMN error_details_json")
        connection.execute("DELETE FROM schema_migrations WHERE version=10")

    migrated = Database(settings)
    attempt = migrated.film_run_planner_request(run["id"], include_result=False)
    assert attempt["status"] == "failed" and attempt["attempts"] == 1
    assert attempt["error_code"] == "schema_rejected"
    assert attempt["response_mode"] == "schema"
    assert migrated.get_film_run(run["id"])["prompt"] == request["prompt"]
    with migrated.connect() as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 10
    assert list(tmp_path.glob("planner-history.sqlite3.pre-migration-*.bak"))


def test_failed_migration_rolls_back_schema_and_keeps_backup(tmp_path):
    path = tmp_path / "broken-legacy.sqlite3"
    with sqlite3.connect(path) as legacy:
        legacy.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL)")
        legacy.execute("CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,root TEXT NOT NULL,created_at TEXT NOT NULL,imported_at TEXT NOT NULL)")
        legacy.execute("INSERT INTO projects VALUES('legacy','Legacy','/legacy','2026','2026')")
    settings = Settings(_env_file=None, database_path=str(path), allowed_path_roots=str(tmp_path))
    with pytest.raises(sqlite3.OperationalError):
        Database(settings)
    backups = list(tmp_path.glob("broken-legacy.sqlite3.pre-migration-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(path) as db:
        assert "description" not in {row[1] for row in db.execute("PRAGMA table_info(projects)")}
        assert db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_tenth_additive_migration_backs_up_version_nine_database(tmp_path):
    path = tmp_path / "version-two.sqlite3"
    settings = Settings(_env_file=None,database_path=str(path),allowed_path_roots=str(tmp_path))
    first = Database(settings)
    with first.connect() as db:
        db.execute("DELETE FROM schema_migrations WHERE version=10")
    migrated = Database(settings)
    assert migrated is not None
    with migrated.connect() as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 10
    assert len(list(tmp_path.glob("version-two.sqlite3.pre-migration-*.bak"))) == 1
