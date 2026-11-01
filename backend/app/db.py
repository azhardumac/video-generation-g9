import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import NAMESPACE_URL, uuid4, uuid5
from contextlib import contextmanager

from .config import Settings
from .paths import safe_local_path
from .film_orchestrator import FilmRunConflict, FilmRunTransitionError

MAX_GEMINI_ATTEMPTS_PER_REVISION = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, settings: Settings):
        self.path = safe_local_path(settings.database_path, settings)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()
        self._backup_before_migration(target_version=11)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._execute_schema_script(db, """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
                    result_path TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    exit_code INTEGER, elapsed_seconds REAL, verification_metadata TEXT,
                    partial_output_path TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    time TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS benchmark_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, preset TEXT NOT NULL,
                    frames INTEGER NOT NULL, fps INTEGER NOT NULL, duration REAL NOT NULL,
                    elapsed_seconds REAL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL,
                    created_at TEXT NOT NULL, imported_at TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '', aspect_ratio TEXT,
                    frame_rate INTEGER, production_type TEXT NOT NULL DEFAULT '',
                    workspace_kind TEXT NOT NULL DEFAULT 'imported', archived_at TEXT,
                    style_bible_json TEXT NOT NULL DEFAULT '{}', style_bible_revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS project_assets (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, category TEXT NOT NULL,
                    relative_path TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    modified_at REAL NOT NULL, mime_type TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS storyboards (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1, archived_at TEXT,
                    manifest_json TEXT, manifest_path TEXT, story_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS storyboard_shots (
                    id TEXT PRIMARY KEY, storyboard_id TEXT NOT NULL,
                    scene_id TEXT, position INTEGER NOT NULL, title TEXT NOT NULL,
                    prompt TEXT NOT NULL, reference_image TEXT,
                    preset TEXT NOT NULL, seed INTEGER, caption TEXT NOT NULL DEFAULT '',
                    width INTEGER, height INTEGER, fps INTEGER, output_location TEXT,
                    framing TEXT NOT NULL DEFAULT '', prompt_reviewed INTEGER NOT NULL DEFAULT 0,
                    reviewed_style_revision INTEGER NOT NULL DEFAULT 0,
                    deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS storyboard_scenes (
                    id TEXT PRIMARY KEY, storyboard_id TEXT NOT NULL,
                    title TEXT NOT NULL, position INTEGER NOT NULL,
                    deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(storyboard_id,title)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_storyboard_shot_order_active
                    ON storyboard_shots(storyboard_id,position) WHERE deleted_at IS NULL;
                CREATE TABLE IF NOT EXISTS storyboard_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, storyboard_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, title TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(storyboard_id, revision)
                );
                CREATE TABLE IF NOT EXISTS shot_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, shot_id TEXT NOT NULL,
                    job_id TEXT NOT NULL UNIQUE, settings_snapshot TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_references (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
                    category TEXT NOT NULL CHECK(category IN ('character','scene','style','other')),
                    path TEXT NOT NULL, source_kind TEXT NOT NULL CHECK(source_kind IN ('linked','uploaded')),
                    notes TEXT NOT NULL DEFAULT '', mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL, content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(project_id, path), UNIQUE(project_id, content_hash)
                );
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS film_plans (
                    storyboard_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    source_story TEXT NOT NULL, planner_mode TEXT NOT NULL,
                    target_format TEXT NOT NULL, preset TEXT NOT NULL,
                    plan_metadata_json TEXT NOT NULL, approved_revision INTEGER,
                    approved_style_revision INTEGER, approved_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_style_bible_revisions (
                    project_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    style_bible_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(project_id,revision)
                );
                CREATE TABLE IF NOT EXISTS project_scene_continuity (
                    project_id TEXT PRIMARY KEY, current_revision INTEGER NOT NULL DEFAULT 0,
                    approved_revision INTEGER, approved_at TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_scene_continuity_revisions (
                    project_id TEXT NOT NULL, revision INTEGER NOT NULL, document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(project_id,revision)
                );
                CREATE TABLE IF NOT EXISTS shot_creative_reviews (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, storyboard_id TEXT NOT NULL,
                    shot_id TEXT NOT NULL, job_id TEXT NOT NULL, storyboard_revision INTEGER NOT NULL,
                    continuity_revision INTEGER NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
                    checklist_json TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shot_creative_reviews_latest
                    ON shot_creative_reviews(shot_id,created_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS film_batches (
                    id TEXT PRIMARY KEY, storyboard_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    storyboard_revision INTEGER NOT NULL, style_bible_revision INTEGER NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS film_batch_shots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
                    shot_id TEXT NOT NULL, position INTEGER NOT NULL, status TEXT NOT NULL,
                    job_id TEXT, skip_reason TEXT NOT NULL DEFAULT '',
                    UNIQUE(batch_id,shot_id)
                );
                CREATE INDEX IF NOT EXISTS idx_film_batch_jobs_job ON film_batch_shots(job_id);
                CREATE TABLE IF NOT EXISTS film_runs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL, prompt TEXT NOT NULL, request_json TEXT NOT NULL DEFAULT '{}',
                    reference_ids_json TEXT NOT NULL, preset TEXT NOT NULL, budget_json TEXT NOT NULL,
                    authorization_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL, stage TEXT NOT NULL, completed_stages_json TEXT NOT NULL DEFAULT '[]',
                    checkpoint_json TEXT NOT NULL DEFAULT '{}', storyboard_id TEXT, film_batch_id TEXT,
                    render_job_id TEXT, error TEXT, revision INTEGER NOT NULL DEFAULT 1,
                    archived_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(project_id,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_film_runs_project_updated ON film_runs(project_id,updated_at DESC);
                CREATE TABLE IF NOT EXISTS film_run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    time TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS film_run_planner_requests (
                    run_id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
                    request_hash TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1,
                    result_json TEXT, usage_json TEXT, provenance_json TEXT,
                    error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS film_run_planner_attempts (
                    run_id TEXT NOT NULL, revision INTEGER NOT NULL, provider TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '', schema_version TEXT NOT NULL, response_mode TEXT NOT NULL DEFAULT 'schema',
                    idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL DEFAULT '', scope_hash TEXT NOT NULL DEFAULT '',
                    consent_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, result_json TEXT, usage_json TEXT, provenance_json TEXT,
                    error_code TEXT, error_details_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(run_id,revision), UNIQUE(run_id,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_film_planner_attempts_hourly
                    ON film_run_planner_attempts(provider,updated_at);
                CREATE INDEX IF NOT EXISTS idx_film_run_events_order ON film_run_events(run_id,id);
                CREATE TABLE IF NOT EXISTS film_run_shots (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, shot_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL, shot_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'planned',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(run_id,shot_key)
                );
                CREATE TABLE IF NOT EXISTS film_run_shot_attempts (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, shot_key TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL, job_id TEXT UNIQUE, status TEXT NOT NULL DEFAULT 'reserved',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(run_id,shot_key,attempt_number)
                );
                CREATE INDEX IF NOT EXISTS idx_film_run_shots_order ON film_run_shots(run_id,created_at);
            """)
            shot_columns = {row[1] for row in db.execute("PRAGMA table_info(storyboard_shots)")}
            if "reconciled_job_id" not in shot_columns:
                db.execute("ALTER TABLE storyboard_shots ADD COLUMN reconciled_job_id TEXT")
                shot_columns.add("reconciled_job_id")
            if "scene_id" not in shot_columns:
                db.execute("ALTER TABLE storyboard_shots ADD COLUMN scene_id TEXT")
            film_run_columns = {row[1] for row in db.execute("PRAGMA table_info(film_runs)")}
            if "archived_at" not in film_run_columns:
                db.execute("ALTER TABLE film_runs ADD COLUMN archived_at TEXT")
            shot_job_columns = {row[1] for row in db.execute("PRAGMA table_info(shot_jobs)")}
            if "settings_snapshot" not in shot_job_columns:
                db.execute("ALTER TABLE shot_jobs ADD COLUMN settings_snapshot TEXT")
            project_columns = {row[1] for row in db.execute("PRAGMA table_info(projects)")}
            for column, declaration in (
                ("description", "TEXT NOT NULL DEFAULT ''"),
                ("aspect_ratio", "TEXT"),
                ("frame_rate", "INTEGER"),
                ("production_type", "TEXT NOT NULL DEFAULT ''"),
                ("workspace_kind", "TEXT NOT NULL DEFAULT 'imported'"),
                ("archived_at", "TEXT"),
                ("style_bible_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("style_bible_revision", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in project_columns:
                    db.execute(f"ALTER TABLE projects ADD COLUMN {column} {declaration}")
            storyboard_columns = {row[1] for row in db.execute("PRAGMA table_info(storyboards)")}
            if "story_text" not in storyboard_columns:
                db.execute("ALTER TABLE storyboards ADD COLUMN story_text TEXT NOT NULL DEFAULT ''")
            shot_columns = {row[1] for row in db.execute("PRAGMA table_info(storyboard_shots)")}
            for column, declaration in (
                ("width", "INTEGER"), ("height", "INTEGER"), ("fps", "INTEGER"),
                ("output_location", "TEXT"), ("framing", "TEXT NOT NULL DEFAULT ''"),
                ("prompt_reviewed", "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_style_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("visual_description", "TEXT NOT NULL DEFAULT ''"),
                ("characters_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("reference_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("estimated_seconds", "REAL NOT NULL DEFAULT 0"),
                ("dialogue", "TEXT NOT NULL DEFAULT ''"),
                ("transition_after", "TEXT NOT NULL DEFAULT 'dissolve'"),
                ("generation_payload_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if column not in shot_columns:
                    db.execute(f"ALTER TABLE storyboard_shots ADD COLUMN {column} {declaration}")
            scene_columns = {row[1] for row in db.execute("PRAGMA table_info(storyboard_scenes)")}
            for column, declaration in (
                ("visual_description", "TEXT NOT NULL DEFAULT ''"),
                ("estimated_seconds", "REAL NOT NULL DEFAULT 0"),
                ("transition_after", "TEXT NOT NULL DEFAULT 'cut'"),
            ):
                if column not in scene_columns:
                    db.execute(f"ALTER TABLE storyboard_scenes ADD COLUMN {column} {declaration}")
            existing = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            for column, declaration in (
                ("exit_code", "INTEGER"),
                ("elapsed_seconds", "REAL"),
                ("verification_metadata", "TEXT"),
                ("partial_output_path", "TEXT"),
                ("project_id", "TEXT"),
            ):
                if column not in existing:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {declaration}")
            # Recover legacy job scope from its shot/storyboard when available.
            db.execute("UPDATE jobs SET project_id=(SELECT b.project_id FROM shot_jobs sj "
                       "JOIN storyboard_shots s ON s.id=sj.shot_id JOIN storyboards b ON b.id=s.storyboard_id "
                       "WHERE sj.job_id=jobs.id LIMIT 1) WHERE project_id IS NULL")
            for job in db.execute("SELECT id,payload FROM jobs WHERE kind='render' AND project_id IS NULL"):
                try:
                    payload = json.loads(job["payload"])
                    storyboard_id = payload.get("props", {}).get("manifest", {}).get("storyboard_id")
                except (TypeError, json.JSONDecodeError):
                    storyboard_id = None
                if storyboard_id:
                    story = db.execute("SELECT project_id FROM storyboards WHERE id=?", (storyboard_id,)).fetchone()
                    if story:
                        db.execute("UPDATE jobs SET project_id=? WHERE id=?", (story["project_id"], job["id"]))
            legacy_projects = list(db.execute("SELECT id FROM projects WHERE archived_at IS NULL"))
            if len(legacy_projects) == 1:
                # This app's pre-project-lifecycle build supported only one project.
                db.execute("UPDATE jobs SET project_id=? WHERE project_id IS NULL", (legacy_projects[0]["id"],))
            interrupted = list(db.execute("SELECT id FROM jobs WHERE status='running'"))
            for job in interrupted:
                message = "Application restarted while this job was running; review and retry manually."
                db.execute("UPDATE jobs SET status='failed',error=COALESCE(error,?),updated_at=? WHERE id=?",
                           (message, now_iso(), job["id"]))
                db.execute("INSERT INTO events(job_id,time,level,message) VALUES(?,?,?,?)",
                           (job["id"], now_iso(), "warning", message))
            db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,?)", (now_iso(),))
            db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(2,?)", (now_iso(),))
            db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(3,?)", (now_iso(),))
            db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(4,?)", (now_iso(),))
            db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(5,?)", (now_iso(),))
            db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(6,?)", (now_iso(),))
            for table in ("film_plans", "film_batches"):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if "film_run_id" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN film_run_id TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_film_plans_run ON film_plans(film_run_id) WHERE film_run_id IS NOT NULL")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_film_batches_run ON film_batches(film_run_id) WHERE film_run_id IS NOT NULL")
            db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(7,?)", (now_iso(),))
            db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(8,?)", (now_iso(),))
            # Preserve old single-ledger planning records as revision one. They
            # remain intact in the legacy table while new attempts append safely.
            for legacy in db.execute("SELECT p.*,r.request_json,r.reference_ids_json,r.budget_json FROM film_run_planner_requests p "
                                     "JOIN film_runs r ON r.id=p.run_id"):
                request = json.loads(legacy["request_json"] or "{}")
                consent = request.get("remote_processing_consent", {})
                db.execute("INSERT OR IGNORE INTO film_run_planner_attempts(run_id,revision,provider,model,schema_version,idempotency_key,"
                           "request_hash,scope_hash,consent_json,status,attempts,result_json,usage_json,provenance_json,error_code,created_at,updated_at) "
                           "VALUES(?,1,?,?, 'legacy-unversioned-schema','legacy-revision-1',?,?,?,?,?,?,?,?,?,?,?)",
                           (legacy["run_id"], legacy["provider"], legacy["model"], legacy["request_hash"], legacy["request_hash"],
                            json.dumps(consent, sort_keys=True), legacy["status"], legacy["attempts"], legacy["result_json"],
                            legacy["usage_json"], legacy["provenance_json"], legacy["error_code"], legacy["created_at"], legacy["updated_at"]))
            attempt_columns = {row[1] for row in db.execute("PRAGMA table_info(film_run_planner_attempts)")}
            if "response_mode" not in attempt_columns:
                db.execute("ALTER TABLE film_run_planner_attempts ADD COLUMN response_mode TEXT NOT NULL DEFAULT 'schema'")
            if "error_details_json" not in attempt_columns:
                db.execute("ALTER TABLE film_run_planner_attempts ADD COLUMN error_details_json TEXT")
            db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(9,?)", (now_iso(),))
            db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(10,?)", (now_iso(),))
            db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(11,?)", (now_iso(),))
            for run in db.execute("SELECT id,revision,stage FROM film_runs WHERE status='running'"):
                message = "Application restarted during orchestration; the run was paused at its last durable checkpoint."
                db.execute("UPDATE film_runs SET status='paused',error=?,revision=revision+1,updated_at=? WHERE id=? AND status='running'",
                           (message, now_iso(), run["id"]))
                db.execute("INSERT INTO film_run_events(run_id,revision,time,level,message) VALUES(?,?,?,?,?)",
                           (run["id"], run["revision"] + 1, now_iso(), "warning", message))
                db.execute("UPDATE film_batches SET status='paused',updated_at=? WHERE film_run_id=? AND status='running'",
                           (now_iso(), run["id"]))
            db.execute("INSERT OR IGNORE INTO project_style_bible_revisions(project_id,revision,style_bible_json,created_at) "
                       "SELECT id,style_bible_revision,style_bible_json,? FROM projects", (now_iso(),))
            db.execute("UPDATE film_batches SET status='paused',updated_at=? WHERE status='running' AND EXISTS "
                       "(SELECT 1 FROM film_batch_shots bs JOIN jobs j ON j.id=bs.job_id WHERE bs.batch_id=film_batches.id AND j.status='failed')",
                       (now_iso(),))

    @staticmethod
    def _execute_schema_script(db: sqlite3.Connection, script: str):
        """Execute schema statements without sqlite3.executescript's implicit commits."""
        statement = ""
        for line in script.splitlines():
            statement += line + "\n"
            if sqlite3.complete_statement(statement):
                sql = statement.strip()
                if sql:
                    db.execute(sql)
                statement = ""
        if statement.strip():
            raise sqlite3.OperationalError("Incomplete database schema migration statement")

    def _backup_before_migration(self, target_version: int):
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return
        try:
            with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as source:
                has_migrations = source.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                ).fetchone()
                if has_migrations:
                    current = source.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
                    if current >= target_version:
                        return
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup = self.path.with_name(f"{self.path.name}.pre-migration-{stamp}.bak")
                with sqlite3.connect(backup) as destination:
                    source.backup(destination)
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not create a pre-migration database backup: {exc}") from exc

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()
        finally:
            db.close()

    def create_job(self, kind: str, payload: dict, project_id: str | None = None) -> dict:
        job_id, now = str(uuid4()), now_iso()
        payload["job_id"] = job_id
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO jobs(id,kind,status,payload,project_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (job_id, kind, "queued", json.dumps(payload), project_id, now, now))
            db.execute("INSERT INTO events(job_id,time,level,message) VALUES(?,?,?,?)",
                       (job_id, now, "info", "Job added to local queue"))
        return self.get_job(job_id)

    def _job(self, row):
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        if item.get("verification_metadata"):
            item["verification_metadata"] = json.loads(item["verification_metadata"])
        return item

    @staticmethod
    def generation_settings_snapshot(payload: dict) -> dict:
        nested = payload.get("generation_payload") or payload.get("generation_payload_json") or {}
        if isinstance(nested, str):
            try:
                nested = json.loads(nested)
            except (TypeError, json.JSONDecodeError):
                nested = {}
        snapshot = {
            "prompt": payload.get("shot_prompt", payload.get("prompt", "")),
            "reference_image": payload.get("reference_image") or None,
            "seed": payload.get("seed"),
            "preset": payload.get("preset", "Short"),
        }
        shot_settings = payload.get("shot_settings", payload if "id" in payload else {})
        for key in ("style_context", "style_bible_revision", "width", "height", "fps", "output_location", "framing"):
            value = shot_settings.get(key, payload.get(key, nested.get(key)))
            if value is not None and value != "" and not (key == "style_bible_revision" and value == 0):
                snapshot[key] = value
        return snapshot

    def get_job(self, job_id: str):
        with self.connect() as db:
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list_jobs(self, limit: int = 100, project_id: str | None = None):
        with self.connect() as db:
            if project_id is None:
                rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
            else:
                rows = db.execute("SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, limit))
            return [self._job(row) for row in rows]

    def update_job(self, job_id: str, **values):
        values["updated_at"] = now_iso()
        keys = list(values)
        json_columns = {"payload", "verification_metadata"}
        encoded = [json.dumps(values[k]) if k in json_columns and values[k] is not None else values[k] for k in keys]
        with self.lock, self.connect() as db:
            db.execute(f"UPDATE jobs SET {', '.join(k+'=?' for k in keys)} WHERE id=?", (*encoded, job_id))
        return self.get_job(job_id)

    def claim_job(self, job_id: str) -> bool:
        """Atomically transition queued -> running so a cancel cannot be overwritten."""
        with self.lock, self.connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=? AND status='queued'",
                (now_iso(), job_id),
            )
            return cursor.rowcount == 1

    def complete_job(self, job_id: str, **values) -> bool:
        """Complete only a currently running job; cancellation has precedence if it won first."""
        values["status"] = "completed"
        values["updated_at"] = now_iso()
        keys = list(values)
        json_columns = {"payload", "verification_metadata"}
        encoded = [json.dumps(values[k]) if k in json_columns and values[k] is not None else values[k] for k in keys]
        with self.lock, self.connect() as db:
            cursor = db.execute(
                f"UPDATE jobs SET {', '.join(k+'=?' for k in keys)} WHERE id=? AND status='running'",
                (*encoded, job_id),
            )
            return cursor.rowcount == 1

    def cancel_job(self, job_id: str):
        """Atomically cancel queued/running work without overwriting a completed state."""
        with self.lock, self.connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET status='cancelled', updated_at=? WHERE id=? AND status IN ('queued','running')",
                (now_iso(), job_id),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_job(job_id)

    def fail_job(self, job_id: str, **values) -> bool:
        values["status"] = "failed"
        values["updated_at"] = now_iso()
        keys = list(values)
        json_columns = {"payload", "verification_metadata"}
        encoded = [json.dumps(values[k]) if k in json_columns and values[k] is not None else values[k] for k in keys]
        with self.lock, self.connect() as db:
            cursor = db.execute(
                f"UPDATE jobs SET {', '.join(k+'=?' for k in keys)} WHERE id=? AND status='running'",
                (*encoded, job_id),
            )
            return cursor.rowcount == 1

    def event(self, job_id: str, level: str, message: str):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO events(job_id,time,level,message) VALUES(?,?,?,?)", (job_id, now_iso(), level, message[:2000]))

    def events(self, job_id: str):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM events WHERE job_id=? ORDER BY id", (job_id,))]

    def get_setting(self, key: str):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else None

    def put_setting(self, key: str, value):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                       (key, json.dumps(value), now_iso()))

    def add_benchmark(self, preset: str, frames: int, fps: int, duration: float, elapsed_seconds: float | None = None):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO benchmark_history(preset,frames,fps,duration,elapsed_seconds,created_at) VALUES(?,?,?,?,?,?)",
                       (preset, frames, fps, duration, elapsed_seconds, now_iso()))

    def benchmarks(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM benchmark_history ORDER BY id DESC LIMIT 200")]

    def create_project(self, project: dict):
        now = now_iso()
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO projects(id,name,root,created_at,imported_at,description,aspect_ratio,frame_rate,production_type,workspace_kind) "
                       "VALUES(?,?,?,?,?,?,?,?,?,'created')",
                       (project["id"], project["name"], project["root"], now, now, project.get("description", ""),
                        project["aspect_ratio"], project["frame_rate"], project["production_type"]))
        return self.get_project(project["id"])

    def import_project(self, project_id: str, name: str, root: str, assets: list[dict], *, description: str = ""):
        now = now_iso()
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO projects(id,name,root,created_at,imported_at,description,workspace_kind) VALUES(?,?,?,?,?,?,'imported') "
                       "ON CONFLICT(id) DO UPDATE SET name=excluded.name,root=excluded.root,imported_at=excluded.imported_at "
                       "WHERE projects.workspace_kind='imported' AND projects.archived_at IS NULL",
                       (project_id, name, root, now, now, description))
            for asset in assets:
                existing = db.execute("SELECT id FROM project_assets WHERE project_id=? AND relative_path=?",
                                      (project_id, asset["relative_path"])).fetchone()
                # Keep the identifier already referenced by legacy rows; namespace new IDs by project.
                asset_id = existing["id"] if existing else asset.get("id", str(uuid4()))
                collision = db.execute("SELECT project_id FROM project_assets WHERE id=?", (asset_id,)).fetchone()
                if collision and collision["project_id"] != project_id:
                    asset_id = str(uuid4())
                db.execute("INSERT INTO project_assets(id,project_id,category,relative_path,size_bytes,modified_at,mime_type) "
                           "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET category=excluded.category,"
                           "size_bytes=excluded.size_bytes,modified_at=excluded.modified_at,mime_type=excluded.mime_type "
                           "WHERE project_assets.project_id=excluded.project_id",
                           (asset_id, project_id, asset["category"], asset["relative_path"], asset["size_bytes"],
                            asset["modified_at"], asset["mime_type"]))
        return self.get_project(project_id)

    def update_project(self, project_id: str, **values):
        allowed = {"name", "description", "aspect_ratio", "frame_rate", "production_type"}
        fields = {key: value for key, value in values.items() if key in allowed}
        if not fields:
            return self.get_project(project_id)
        with self.lock, self.connect() as db:
            cursor = db.execute(f"UPDATE projects SET {', '.join(key+'=?' for key in fields)} WHERE id=? AND archived_at IS NULL",
                                (*fields.values(), project_id))
            if cursor.rowcount != 1:
                raise KeyError("Project not found")
        return self.get_project(project_id)

    def archive_project(self, project_id: str):
        with self.lock, self.connect() as db:
            active = db.execute("SELECT 1 FROM jobs WHERE project_id=? AND status IN ('queued','running') LIMIT 1", (project_id,)).fetchone()
            if active:
                raise RuntimeError("Cancel active project jobs before archiving this project")
            cursor = db.execute("UPDATE projects SET archived_at=? WHERE id=? AND archived_at IS NULL", (now_iso(), project_id))
            if cursor.rowcount != 1:
                raise KeyError("Project not found")
            db.execute("UPDATE settings SET value='null',updated_at=? WHERE key='active_project_id' AND value=?",
                       (now_iso(), json.dumps(project_id)))

    def restore_project(self, project_id: str):
        with self.lock, self.connect() as db:
            cursor = db.execute("UPDATE projects SET archived_at=NULL WHERE id=? AND archived_at IS NOT NULL", (project_id,))
            if cursor.rowcount != 1:
                raise KeyError("Archived project not found")
        return self.get_project(project_id)

    def list_projects(self, include_archived: bool = False):
        with self.connect() as db:
            where = "" if include_archived else "WHERE p.archived_at IS NULL"
            return [dict(row) for row in db.execute(
                f"SELECT p.*,(SELECT COUNT(*) FROM storyboard_shots s JOIN storyboards b ON b.id=s.storyboard_id "
                f"WHERE b.project_id=p.id AND b.archived_at IS NULL AND s.deleted_at IS NULL) AS shot_count,"
                f"(SELECT COUNT(*) FROM project_references r WHERE r.project_id=p.id) AS reference_count,"
                f"(SELECT COUNT(*) FROM jobs j WHERE j.project_id=p.id) AS job_count FROM projects p {where} ORDER BY p.imported_at DESC,p.id")]

    def get_project(self, project_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                return None
            project = dict(row)
            project["style_bible"] = json.loads(project.pop("style_bible_json", "{}") or "{}")
            return project

    def update_project_style_bible(self, project_id: str, style_bible: dict):
        with self.lock, self.connect() as db:
            current = db.execute("SELECT style_bible_revision,style_bible_json FROM projects WHERE id=? AND archived_at IS NULL", (project_id,)).fetchone()
            if not current:
                raise KeyError("Project not found")
            db.execute("INSERT OR IGNORE INTO project_style_bible_revisions(project_id,revision,style_bible_json,created_at) VALUES(?,?,?,?)",
                       (project_id, current["style_bible_revision"], current["style_bible_json"], now_iso()))
            cursor = db.execute("UPDATE projects SET style_bible_json=?,style_bible_revision=style_bible_revision+1 WHERE id=? AND archived_at IS NULL",
                                (json.dumps(style_bible, ensure_ascii=False), project_id))
            if cursor.rowcount != 1:
                raise KeyError("Project not found")
        return self.get_project(project_id)

    def project_references(self, project_id: str):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM project_references WHERE project_id=? ORDER BY category,name COLLATE NOCASE,id", (project_id,))]

    def get_project_reference(self, project_id: str, reference_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM project_references WHERE project_id=? AND id=?", (project_id, reference_id)).fetchone()
            return dict(row) if row else None

    def add_project_reference(self, reference: dict):
        now = now_iso()
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO project_references(id,project_id,name,category,path,source_kind,notes,mime_type,size_bytes,content_hash,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (reference["id"], reference["project_id"], reference["name"], reference["category"], reference["path"],
                        reference["source_kind"], reference.get("notes", ""), reference["mime_type"], reference["size_bytes"],
                        reference["content_hash"], now, now))
        return self.get_project_reference(reference["project_id"], reference["id"])

    def update_project_reference(self, project_id: str, reference_id: str, **values):
        allowed = {"name", "category", "path", "source_kind", "notes", "mime_type", "size_bytes", "content_hash"}
        fields = {key: value for key, value in values.items() if key in allowed}
        if not fields:
            return self.get_project_reference(project_id, reference_id)
        fields["updated_at"] = now_iso()
        with self.lock, self.connect() as db:
            cursor = db.execute(f"UPDATE project_references SET {', '.join(key+'=?' for key in fields)} WHERE project_id=? AND id=?",
                                (*fields.values(), project_id, reference_id))
            if cursor.rowcount != 1:
                raise KeyError("Project reference not found")
        return self.get_project_reference(project_id, reference_id)

    def remove_project_reference(self, project_id: str, reference_id: str):
        with self.lock, self.connect() as db:
            cursor = db.execute("DELETE FROM project_references WHERE project_id=? AND id=?", (project_id, reference_id))
            if cursor.rowcount != 1:
                raise KeyError("Project reference not found")

    def get_scene_continuity(self, project_id: str):
        with self.connect() as db:
            current = db.execute("SELECT * FROM project_scene_continuity WHERE project_id=?", (project_id,)).fetchone()
            if not current:
                return {"project_id": project_id, "revision": 0, "approved_revision": None,
                        "approved_at": None, "document": None}
            item = dict(current)
            row = db.execute("SELECT document_json FROM project_scene_continuity_revisions WHERE project_id=? AND revision=?",
                             (project_id, item["current_revision"])).fetchone()
            item["revision"] = item.pop("current_revision")
            item["document"] = json.loads(row["document_json"]) if row else None
            return item

    def save_scene_continuity(self, project_id: str, document: dict):
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            current = db.execute("SELECT current_revision FROM project_scene_continuity WHERE project_id=?", (project_id,)).fetchone()
            revision = (current["current_revision"] if current else 0) + 1
            db.execute("INSERT INTO project_scene_continuity(project_id,current_revision,approved_revision,approved_at,updated_at) "
                       "VALUES(?,?,NULL,NULL,?) ON CONFLICT(project_id) DO UPDATE SET current_revision=excluded.current_revision,"
                       "approved_revision=NULL,approved_at=NULL,updated_at=excluded.updated_at", (project_id, revision, timestamp))
            db.execute("INSERT INTO project_scene_continuity_revisions(project_id,revision,document_json,created_at) VALUES(?,?,?,?)",
                       (project_id, revision, json.dumps(document, ensure_ascii=False), timestamp))
        return self.get_scene_continuity(project_id)

    def approve_scene_continuity(self, project_id: str, revision: int):
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            cursor = db.execute("UPDATE project_scene_continuity SET approved_revision=?,approved_at=?,updated_at=? "
                                "WHERE project_id=? AND current_revision=?", (revision, timestamp, timestamp, project_id, revision))
            if cursor.rowcount != 1:
                raise RuntimeError("Scene continuity changed; save or reload before approval")
        return self.get_scene_continuity(project_id)

    def add_creative_review(self, *, project_id: str, storyboard_id: str, shot_id: str, job_id: str,
                            storyboard_revision: int, continuity_revision: int, decision: str,
                            checklist: dict, reason: str):
        review_id, timestamp = str(uuid4()), now_iso()
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO shot_creative_reviews(id,project_id,storyboard_id,shot_id,job_id,storyboard_revision,"
                       "continuity_revision,decision,checklist_json,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (review_id, project_id, storyboard_id, shot_id, job_id, storyboard_revision,
                        continuity_revision, decision, json.dumps(checklist), reason, timestamp))
        return {"id": review_id, "project_id": project_id, "storyboard_id": storyboard_id, "shot_id": shot_id,
                "job_id": job_id, "storyboard_revision": storyboard_revision, "continuity_revision": continuity_revision,
                "decision": decision, "checklist": checklist, "reason": reason, "created_at": timestamp}

    def latest_creative_review(self, shot_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM shot_creative_reviews WHERE shot_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (shot_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["checklist"] = json.loads(result.pop("checklist_json"))
            return result

    def project_assets(self, project_id: str):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM project_assets WHERE project_id=? ORDER BY category,relative_path", (project_id,))]

    def get_project_asset(self, project_id: str, asset_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM project_assets WHERE project_id=? AND id=?", (project_id, asset_id)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _story_snapshot(db, storyboard_id: str, revision: int, title: str, timestamp: str):
        story_text_row = db.execute("SELECT story_text FROM storyboards WHERE id=?", (storyboard_id,)).fetchone()
        story_text = story_text_row[0] if story_text_row else ""
        scenes = [dict(row) for row in db.execute(
            "SELECT id,title,position FROM storyboard_scenes WHERE storyboard_id=? AND deleted_at IS NULL ORDER BY position,id", (storyboard_id,))]
        shots = [dict(row) for row in db.execute(
            "SELECT s.id,s.scene_id,c.title AS scene_title,s.position,s.title,s.prompt,s.reference_image,s.preset,s.seed,s.caption,"
            "s.width,s.height,s.fps,s.output_location,s.framing,s.prompt_reviewed,s.reviewed_style_revision,"
            "s.visual_description,s.characters_json,s.reference_ids_json,s.estimated_seconds,s.dialogue,s.transition_after,s.generation_payload_json "
            "FROM storyboard_shots s LEFT JOIN storyboard_scenes c ON c.id=s.scene_id "
            "WHERE s.storyboard_id=? AND s.deleted_at IS NULL ORDER BY s.position,s.id", (storyboard_id,))]
        db.execute("INSERT INTO storyboard_revisions(storyboard_id,revision,title,snapshot_json,created_at) VALUES(?,?,?,?,?)",
                   (storyboard_id, revision, title, json.dumps({"story_text": story_text, "scenes": scenes, "shots": shots}, ensure_ascii=False, sort_keys=True), timestamp))

    def create_storyboard(self, project_id: str, title: str, shots: list[dict], story_text: str = "") -> dict:
        storyboard_id, timestamp = str(uuid4()), now_iso()
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO storyboards(id,project_id,title,revision,story_text,created_at,updated_at) VALUES(?,?,?,1,?,?,?)",
                       (storyboard_id, project_id, title, story_text, timestamp, timestamp))
            scene_ids = {}
            for position, shot in enumerate(shots):
                scene_title = (shot.get("scene_title") or "Scene 1").strip()
                if scene_title not in scene_ids:
                    scene_ids[scene_title] = str(uuid4())
                    db.execute("INSERT INTO storyboard_scenes(id,storyboard_id,title,position,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                               (scene_ids[scene_title], storyboard_id, scene_title, len(scene_ids)-1, timestamp, timestamp))
                db.execute("INSERT INTO storyboard_shots(id,storyboard_id,scene_id,position,title,prompt,reference_image,preset,seed,caption,"
                           "visual_description,characters_json,reference_ids_json,estimated_seconds,dialogue,transition_after,generation_payload_json,"
                           "width,height,fps,output_location,framing,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (shot.get("id") or str(uuid4()), storyboard_id, scene_ids[scene_title], position, shot["title"], shot["prompt"],
                            shot.get("reference_image"), shot.get("preset", "Short"), shot.get("seed"),
                            shot.get("caption", ""), shot.get("visual_description", ""), json.dumps(shot.get("characters", [])),
                            json.dumps(shot.get("reference_ids", [])), shot.get("estimated_seconds", 0), shot.get("dialogue", ""),
                            shot.get("transition_after", "dissolve"), json.dumps(shot.get("generation_payload", {}), ensure_ascii=False),
                            shot.get("width"), shot.get("height"), shot.get("fps"), shot.get("output_location"), shot.get("framing", ""), timestamp, timestamp))
            self._story_snapshot(db, storyboard_id, 1, title, timestamp)
        return self.get_storyboard(storyboard_id)

    @staticmethod
    def _film_run(row):
        if row is None:
            return None
        item = dict(row)
        for field, column in (("request", "request_json"), ("reference_ids", "reference_ids_json"), ("budget", "budget_json"),
                              ("authorization", "authorization_json"), ("completed_stages", "completed_stages_json"),
                              ("checkpoint", "checkpoint_json")):
            item[field] = json.loads(item.pop(column) or "{}")
        item["events"] = []
        return item

    def create_film_run(self, project_id: str, idempotency_key: str, request_hash: str, request: dict,
                        *, status: str, stage: str):
        timestamp, run_id = now_iso(), str(uuid4())
        budget = request.get("budget", {})
        authorization = request.get("authorization", {}) if status != "awaiting_authorization" else {}
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # A fresh browser idempotency key must not duplicate active work.
            # Old failed planning runs have no LTX workload and must not shadow
            # a deliberate fresh request. Any active associated LTX job is
            # protected regardless of run status; other blocked stages remain
            # deduplicated to avoid replaying completed/partial production.
            matching = db.execute("SELECT * FROM film_runs WHERE project_id=? AND request_hash=? "
                                  "AND archived_at IS NULL AND ("
                                  "status IN ('awaiting_authorization','waiting_for_planner','running','paused') OR "
                                  "EXISTS(SELECT 1 FROM film_run_shot_attempts a JOIN jobs j ON j.id=a.job_id "
                                  "WHERE a.run_id=film_runs.id AND j.status IN ('queued','running')) OR "
                                  "EXISTS(SELECT 1 FROM film_batches b JOIN film_batch_shots bs ON bs.batch_id=b.id "
                                  "JOIN jobs j ON j.id=bs.job_id WHERE b.film_run_id=film_runs.id AND j.status IN ('queued','running')) OR "
                                  "(status='needs_attention' AND stage!='understand_story')) "
                                  "ORDER BY created_at DESC LIMIT 1", (project_id, request_hash)).fetchone()
            if matching:
                result = self._film_run(matching)
                result["created"] = False
                result["deduplicated"] = True
                return result
            db.execute("INSERT OR IGNORE INTO film_runs(id,project_id,idempotency_key,request_hash,prompt,request_json,reference_ids_json,preset,budget_json,authorization_json,status,stage,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (run_id, project_id, idempotency_key, request_hash, request["prompt"],
                        json.dumps(request, ensure_ascii=False, sort_keys=True),
                        json.dumps(request.get("reference_ids", [])), request.get("preset", "Short"),
                        json.dumps(budget, sort_keys=True), json.dumps(authorization, sort_keys=True),
                        status, stage, timestamp, timestamp))
            row = db.execute("SELECT * FROM film_runs WHERE project_id=? AND idempotency_key=?",
                             (project_id, idempotency_key)).fetchone()
            created = row["id"] == run_id
            if not created and row["request_hash"] != request_hash:
                raise ValueError("Idempotency key was already used for a different film request")
            if created:
                db.execute("INSERT INTO film_run_events(run_id,revision,time,level,message) VALUES(?,?,?,?,?)",
                           (run_id, 1, timestamp, "info", "Prompt and generation budget saved; no generation job was submitted."))
        result = self.get_film_run(row["id"])
        result["created"] = created
        result["deduplicated"] = False
        return result

    def get_film_run(self, run_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM film_runs WHERE id=?", (run_id,)).fetchone()
            result = self._film_run(row)
            if result:
                result["events"] = [dict(event) for event in db.execute(
                    "SELECT revision,time,level,message FROM film_run_events WHERE run_id=? ORDER BY id", (run_id,))]
            return result

    def list_film_runs(self, project_id: str, limit: int = 100, *, include_archived: bool = False):
        with self.connect() as db:
            if include_archived:
                rows = db.execute("SELECT * FROM film_runs WHERE project_id=? ORDER BY updated_at DESC LIMIT ?",
                                  (project_id, limit))
            else:
                rows = db.execute("SELECT * FROM film_runs WHERE project_id=? AND archived_at IS NULL "
                                  "ORDER BY updated_at DESC LIMIT ?", (project_id, limit))
            runs = [self._film_run(row) for row in rows]
            for run in runs:
                run["events"] = [dict(event) for event in db.execute(
                    "SELECT revision,time,level,message FROM film_run_events WHERE run_id=? ORDER BY id", (run["id"],))]
            return runs

    def set_film_run_archived(self, project_id: str, run_id: str, expected_revision: int, *, archived: bool):
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM film_runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
            if not row:
                return None
            if int(row["revision"]) != expected_revision:
                raise FilmRunConflict("Film run changed before the archive action; refresh and retry")
            if archived:
                if row["archived_at"]:
                    return self._film_run(row)
                if row["status"] not in {"needs_attention", "failed", "cancelled", "completed"}:
                    raise FilmRunTransitionError("Only failed, cancelled, completed, or needs-attention film runs can be archived")
                active_job = db.execute(
                    "SELECT 1 FROM film_run_shot_attempts a JOIN jobs j ON j.id=a.job_id "
                    "WHERE a.run_id=? AND j.status IN ('queued','running') "
                    "UNION ALL SELECT 1 FROM film_batches b JOIN film_batch_shots bs ON bs.batch_id=b.id "
                    "JOIN jobs j ON j.id=bs.job_id WHERE b.film_run_id=? AND j.status IN ('queued','running') LIMIT 1",
                    (run_id, run_id)).fetchone()
                if active_job:
                    raise FilmRunTransitionError("This run still has queued or running LTX jobs; let them settle or cancel them before archiving")
            elif not row["archived_at"]:
                return self._film_run(row)

            archived_at = timestamp if archived else None
            new_revision = expected_revision + 1
            db.execute("UPDATE film_runs SET archived_at=?,revision=?,updated_at=? WHERE id=? AND project_id=? AND revision=?",
                       (archived_at, new_revision, timestamp, run_id, project_id, expected_revision))
            message = ("Film run archived from the normal view; history and media are preserved." if archived
                       else "Film run restored to the normal view; history and media are preserved.")
            db.execute("INSERT INTO film_run_events(run_id,revision,time,level,message) VALUES(?,?,?,?,?)",
                       (run_id, new_revision, timestamp, "info", message))
        return self.get_film_run(run_id)

    def film_run_generation_job_count(self, run_id: str) -> int:
        with self.connect() as db:
            row = db.execute("SELECT COUNT(DISTINCT job_id) FROM ("
                "SELECT job_id FROM film_run_shot_attempts WHERE run_id=? AND job_id IS NOT NULL "
                "UNION SELECT bs.job_id FROM film_batches b JOIN film_batch_shots bs ON bs.batch_id=b.id "
                "WHERE b.film_run_id=? AND bs.job_id IS NOT NULL)", (run_id, run_id)).fetchone()
            return int(row[0] or 0)

    def next_film_run(self):
        """Return the oldest explicitly authorized run awaiting a durable stage."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM film_runs WHERE status='waiting_for_planner' "
                             "AND json_extract(authorization_json,'$.confirmed')=1 "
                             "ORDER BY created_at,id LIMIT 1").fetchone()
            return self._film_run(row)

    @staticmethod
    def _planner_attempt(row, *, include_result: bool = True):
        if not row:
            return None
        item = dict(row)
        for field in ("result_json", "usage_json", "provenance_json", "consent_json", "error_details_json"):
            raw = item.pop(field, None)
            item[field.removesuffix("_json")] = json.loads(raw) if raw else None
        if not include_result:
            item.pop("result", None)
        item["remaining_attempts"] = max(0, 3 - int(item.get("attempts", 0)))
        return item

    def film_run_planner_attempts(self, run_id: str, *, include_result: bool = False):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM film_run_planner_attempts WHERE run_id=? ORDER BY revision", (run_id,))
            return [self._planner_attempt(row, include_result=include_result) for row in rows]

    def film_run_planner_request(self, run_id: str, *, include_result: bool = True, revision: int | None = None):
        """Compatibility accessor returning the selected revision or latest attempt."""
        with self.connect() as db:
            if revision is None:
                row = db.execute("SELECT * FROM film_run_planner_attempts WHERE run_id=? ORDER BY revision DESC LIMIT 1", (run_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM film_run_planner_attempts WHERE run_id=? AND revision=?", (run_id, revision)).fetchone()
            if row:
                return self._planner_attempt(row, include_result=include_result)
            legacy = db.execute("SELECT * FROM film_run_planner_requests WHERE run_id=?", (run_id,)).fetchone()
            return self._planner_attempt(legacy, include_result=include_result)

    def reserve_film_run_planner_request(self, run_id: str, provider: str, model: str, request_hash: str, *,
                                         revision: int = 1, schema_version: str = "storyplan-json-v3",
                                         response_mode: str = "schema"):
        """Reserve an attempt before network I/O; no restart can resubmit silently."""
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT status,stage,authorization_json,request_json,project_id FROM film_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise KeyError("Film run not found")
            if run["status"] != "running" or run["stage"] != "understand_story":
                raise FilmRunTransitionError("Planner requests can be reserved only during the authorized story-planning stage")
            if not json.loads(run["authorization_json"] or "{}").get("confirmed"):
                raise FilmRunTransitionError("Story planning requires explicit initial authorization")
            request = json.loads(run["request_json"] or "{}")
            if int(request.get("planner_revision", 1)) != int(revision):
                raise FilmRunConflict("This planning revision is no longer selected for the run")
            existing = db.execute("SELECT * FROM film_run_planner_attempts WHERE run_id=? AND revision=?",
                                  (run_id, revision)).fetchone()
            if existing:
                if (existing["provider"] != provider or existing["model"] != model or
                        existing["schema_version"] != schema_version or existing["response_mode"] != response_mode):
                    raise FilmRunConflict("This run already has a different model, schema, or response format for the selected planning revision")
                if existing["request_hash"] not in {"", "pending", request_hash}:
                    raise FilmRunConflict("This planning revision already reserved a different prompt and project scope")
                if existing["status"] in {"started", "completed", "failed"}:
                    item = self._planner_attempt(existing)
                    item["result"] = json.loads(existing["result_json"]) if existing["result_json"] else None
                    return item | {"created": False}
                if existing["status"] != "retry_authorized":
                    raise FilmRunConflict("This story-planning revision is already reserved")
                if existing["attempts"] >= 3:
                    raise FilmRunTransitionError("This planning revision has used all three explicit attempts. Switch to a new authorized model or schema revision to continue.")
                db.execute("UPDATE film_run_planner_attempts SET request_hash=?,status='started',attempts=attempts+1,error_code=NULL,updated_at=? WHERE run_id=? AND revision=?",
                           (request_hash, timestamp, run_id, revision))
                item = db.execute("SELECT * FROM film_run_planner_attempts WHERE run_id=? AND revision=?", (run_id, revision)).fetchone()
                return self._planner_attempt(item) | {"created": True}
            consent = request.get("planner_replan_consent", {}) if revision > 1 else request.get("remote_processing_consent", {})
            db.execute("INSERT INTO film_run_planner_attempts(run_id,revision,provider,model,schema_version,response_mode,idempotency_key,request_hash,scope_hash,consent_json,status,attempts,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,'started',1,?,?)",
                       (run_id, revision, provider, model or "", schema_version, response_mode, f"revision-{revision}-{request_hash[:16]}", request_hash,
                        hashlib.sha256(json.dumps({"project_id": run["project_id"], "prompt": json.loads(run["request_json"]).get("prompt"),
                            "reference_ids": json.loads(run["request_json"]).get("reference_ids", []), "budget": json.loads(run["request_json"]).get("budget")},
                            ensure_ascii=False, sort_keys=True).encode()).hexdigest(), json.dumps(consent, sort_keys=True), timestamp, timestamp))
            item = db.execute("SELECT * FROM film_run_planner_attempts WHERE run_id=? AND revision=?", (run_id, revision)).fetchone()
            return self._planner_attempt(item) | {"created": True}

    def complete_film_run_planner_request(self, run_id: str, result: dict, *, revision: int = 1):
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM film_run_planner_attempts WHERE run_id=? AND revision=?", (run_id, revision)).fetchone()
            if not row or row["status"] not in {"started", "completed"}:
                raise FilmRunTransitionError("The story-planning attempt reservation is no longer active")
            db.execute("UPDATE film_run_planner_attempts SET status='completed',result_json=?,usage_json=?,provenance_json=?,error_code=NULL,updated_at=? WHERE run_id=? AND revision=?",
                       (json.dumps(result, ensure_ascii=False), json.dumps(result.get("provenance", {}).get("usage", {}), sort_keys=True),
                        json.dumps(result.get("provenance", {}), ensure_ascii=False, sort_keys=True), timestamp, run_id, revision))
        return self.film_run_planner_request(run_id, revision=revision)

    def fail_film_run_planner_request(self, run_id: str, error_code: str, *, revision: int = 1,
                                      error_details: dict | None = None):
        with self.lock, self.connect() as db:
            db.execute("UPDATE film_run_planner_attempts SET status='failed',error_code=?,error_details_json=?,updated_at=? WHERE run_id=? AND revision=? AND status='started'",
                       (error_code[:80], json.dumps(error_details, sort_keys=True) if error_details else None, now_iso(), run_id, revision))

    def authorize_film_run_planner_retry(self, run_id: str):
        """Only explicit user resume authorizes another request in this revision."""
        with self.lock, self.connect() as db:
            row = db.execute("SELECT revision,status,attempts FROM film_run_planner_attempts WHERE run_id=? ORDER BY revision DESC LIMIT 1", (run_id,)).fetchone()
            if row and row["status"] in {"started", "failed"}:
                if row["attempts"] >= 3:
                    raise FilmRunTransitionError("This planning revision has used all three attempts. Use Switch model and retry planning to create a separately tracked revision.")
                db.execute("UPDATE film_run_planner_attempts SET status='retry_authorized',updated_at=? WHERE run_id=? AND revision=?",
                           (now_iso(), run_id, row["revision"]))

    def create_film_run_planner_revision(self, run_id: str, expected_revision: int, model: str,
                                         idempotency_key: str, scope_hash: str, schema_version: str,
                                         response_mode: str,
                                         consent: dict):
        """Create a separately billed, explicitly consented revision without altering the film budget."""
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute("SELECT a.*,r.revision AS run_revision,r.id AS film_run_id FROM film_run_planner_attempts a "
                                   "JOIN film_runs r ON r.id=a.run_id WHERE a.run_id=? AND a.idempotency_key=?",
                                   (run_id, idempotency_key)).fetchone()
            if duplicate:
                if (duplicate["model"] != model or duplicate["schema_version"] != schema_version or
                        duplicate["response_mode"] != response_mode or duplicate["scope_hash"] != scope_hash):
                    raise FilmRunConflict("This planner idempotency key was already used for a different model, schema, response format, or run scope")
                result = self._film_run(db.execute("SELECT * FROM film_runs WHERE id=?", (run_id,)).fetchone())
                result["replan_deduplicated"] = True
                return result
            run = db.execute("SELECT * FROM film_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise KeyError("Film run not found")
            request = json.loads(run["request_json"] or "{}")
            if run["revision"] != expected_revision:
                raise FilmRunConflict("Film run changed; refresh before switching planner models")
            if run["status"] != "needs_attention" or run["stage"] != "understand_story":
                raise FilmRunTransitionError("Only a blocked story-planning stage can be replanned")
            if request.get("planner_provider") != "gemini" or not request.get("remote_processing_consent", {}).get("confirmed"):
                raise FilmRunTransitionError("This run is not authorized for remote Gemini planning")
            if not consent.get("confirmed"):
                raise FilmRunTransitionError("Explicit consent is required for each potentially billable planning revision")
            if response_mode not in {"schema", "json_only"}:
                raise FilmRunTransitionError("Unsupported Gemini response format")
            if db.execute("SELECT 1 FROM film_run_shots WHERE run_id=? LIMIT 1", (run_id,)).fetchone() or \
                    db.execute("SELECT 1 FROM film_batches WHERE film_run_id=? LIMIT 1", (run_id,)).fetchone():
                raise FilmRunTransitionError("This run has existing shot jobs; planner recovery cannot alter a generated film")
            latest = db.execute("SELECT * FROM film_run_planner_attempts WHERE run_id=? ORDER BY revision DESC LIMIT 1", (run_id,)).fetchone()
            if latest and latest["status"] not in {"failed", "started"}:
                raise FilmRunTransitionError("Only a failed or interrupted planning attempt can be replaced")
            next_revision = (latest["revision"] + 1) if latest else 1
            old_model = latest["model"] if latest else ""
            request["planner_model"] = model
            request["planner_revision"] = next_revision
            request["planner_schema_version"] = schema_version
            request["planner_response_mode"] = response_mode
            request["planner_replan_consent"] = consent | {"previous_model": old_model, "new_model": model,
                "scope_hash": scope_hash, "schema_version": schema_version, "response_mode": response_mode, "confirmed_at": timestamp}
            db.execute("INSERT INTO film_run_planner_attempts(run_id,revision,provider,model,schema_version,response_mode,idempotency_key,request_hash,scope_hash,consent_json,status,attempts,created_at,updated_at) "
                       "VALUES(?,?, 'gemini',?,?,?,?, 'pending',?,?, 'retry_authorized',0,?,?)",
                       (run_id, next_revision, model, schema_version, response_mode, idempotency_key, scope_hash,
                        json.dumps(request["planner_replan_consent"], sort_keys=True), timestamp, timestamp))
            message = f"Explicit Gemini replan authorized as revision {next_revision}: {old_model or 'unselected'} → {model}; prior attempts and film budget were preserved."
            db.execute("UPDATE film_runs SET request_json=?,status='waiting_for_planner',error=NULL,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                       (json.dumps(request, ensure_ascii=False, sort_keys=True), timestamp, run_id, expected_revision))
            new_revision = expected_revision + 1
            db.execute("INSERT INTO film_run_events(run_id,revision,time,level,message) VALUES(?,?,?,?,?)",
                       (run_id, new_revision, timestamp, "warning", message))
            updated = db.execute("SELECT * FROM film_runs WHERE id=?", (run_id,)).fetchone()
            result = self._film_run(updated)
            result["events"] = [dict(event) for event in db.execute("SELECT revision,time,level,message FROM film_run_events WHERE run_id=? ORDER BY id", (run_id,))]
            result["replan_deduplicated"] = False
            return result

    def film_run_budget_for_job(self, job_id: str):
        with self.connect() as db:
            row = db.execute("SELECT b.film_run_id,r.budget_json FROM jobs j JOIN film_batch_shots bs ON bs.job_id=j.id "
                             "JOIN film_batches b ON b.id=bs.batch_id JOIN film_runs r ON r.id=b.film_run_id "
                             "WHERE j.id=?", (job_id,)).fetchone()
            if not row:
                return None
            finished = list(db.execute("SELECT j.elapsed_seconds,j.result_path,j.partial_output_path FROM jobs j "
                "JOIN film_batch_shots bs ON bs.job_id=j.id JOIN film_batches b ON b.id=bs.batch_id "
                "WHERE b.film_run_id=? AND j.status NOT IN ('queued','running')", (row["film_run_id"],)))
        output_bytes = 0
        seen_paths = set()
        for item in finished:
            for raw_path in (item["result_path"], item["partial_output_path"]):
                if not raw_path or raw_path in seen_paths:
                    continue
                seen_paths.add(raw_path)
                try:
                    output_bytes += Path(raw_path).stat().st_size
                except OSError:
                    pass
        budget = json.loads(row["budget_json"])
        elapsed = sum(float(item["elapsed_seconds"] or 0) for item in finished)
        return {"run_id": row["film_run_id"], "elapsed_seconds": elapsed, "output_bytes": output_bytes,
                "remaining_seconds": max(0, budget["max_generation_seconds"] - elapsed),
                "max_generation_seconds": budget["max_generation_seconds"],
                "max_output_bytes": budget["max_output_bytes"]}

    def stop_film_run_queue_for_budget(self, run_id: str, message: str):
        timestamp, cancelled = now_iso(), []
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT status,revision FROM film_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                return cancelled
            rows = list(db.execute("SELECT j.id FROM jobs j JOIN film_batch_shots bs ON bs.job_id=j.id "
                                   "JOIN film_batches b ON b.id=bs.batch_id WHERE b.film_run_id=? AND j.status='queued'",
                                   (run_id,)))
            for row in rows:
                if db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND status='queued'",
                              (timestamp, row["id"])).rowcount:
                    cancelled.append(row["id"])
                    db.execute("INSERT INTO events(job_id,time,level,message) VALUES(?,?,?,?)",
                               (row["id"], timestamp, "warning", message[:2000]))
            if run["status"] not in {"needs_attention", "needs_review", "cancelled", "failed", "completed"}:
                db.execute("UPDATE film_runs SET status='needs_attention',error=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                           (message[:2000], timestamp, run_id, run["revision"]))
                db.execute("INSERT INTO film_run_events(run_id,revision,time,level,message) VALUES(?,?,?,?,?)",
                           (run_id, run["revision"] + 1, timestamp, "error", message[:2000]))
        return cancelled

    def transition_film_run_batch(self, run_id: str, action: str):
        """Pause or cancel only the queue batch owned by this run."""
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError("Unsupported film batch transition")
        timestamp, cancelled = now_iso(), []
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = db.execute("SELECT id,status FROM film_batches WHERE film_run_id=?", (run_id,)).fetchone()
            if not batch:
                return []
            if action == "pause" and batch["status"] == "running":
                db.execute("UPDATE film_batches SET status='paused',updated_at=? WHERE id=?", (timestamp, batch["id"]))
            elif action == "resume" and batch["status"] == "paused":
                db.execute("UPDATE film_batches SET status='running',updated_at=? WHERE id=?", (timestamp, batch["id"]))
            elif action == "cancel" and batch["status"] in {"running", "paused", "failed"}:
                rows = list(db.execute("SELECT job_id FROM film_batch_shots WHERE batch_id=?", (batch["id"],)))
                for row in rows:
                    if row["job_id"] and db.execute("UPDATE jobs SET status='cancelled',updated_at=? "
                                                    "WHERE id=? AND status='queued'", (timestamp, row["job_id"])).rowcount:
                        cancelled.append(row["job_id"])
                        db.execute("INSERT INTO events(job_id,time,level,message) VALUES(?,?,?,?)",
                                   (row["job_id"], timestamp, "warning", "Film run cancelled; queued shot retained in history"))
                db.execute("UPDATE film_batches SET status='cancelled',updated_at=? WHERE id=?", (timestamp, batch["id"]))
        return cancelled

    def update_film_run(self, run_id: str, expected_revision: int, *, status: str, message: str,
                        authorization: dict | None = None, error: str | None = None):
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changes = {"status": status, "revision": expected_revision + 1, "updated_at": timestamp, "error": error}
            if authorization is not None:
                changes["authorization_json"] = json.dumps(authorization, sort_keys=True)
            cursor = db.execute("UPDATE film_runs SET " + ",".join(f"{key}=?" for key in changes) +
                                " WHERE id=? AND revision=?", (*changes.values(), run_id, expected_revision))
            if cursor.rowcount != 1:
                raise RuntimeError("Film run changed; refresh before updating it")
            db.execute("INSERT INTO film_run_events(run_id,revision,time,level,message) VALUES(?,?,?,?,?)",
                       (run_id, expected_revision + 1, timestamp, "error" if error else "info", message[:2000]))
        return self.get_film_run(run_id)

    def checkpoint_film_run(self, run_id: str, expected_revision: int, expected_stage: str,
                            next_stage: str, checkpoint: dict, *, status: str, message: str):
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT stage,completed_stages_json,checkpoint_json FROM film_runs WHERE id=? AND revision=? AND status='running'",
                             (run_id, expected_revision)).fetchone()
            if not row or row["stage"] != expected_stage:
                raise RuntimeError("Film run stage changed; checkpoint was not applied")
            completed = json.loads(row["completed_stages_json"])
            completed.append(expected_stage)
            checkpoints = json.loads(row["checkpoint_json"])
            checkpoints[expected_stage] = checkpoint
            cursor = db.execute("UPDATE film_runs SET stage=?,status=?,completed_stages_json=?,checkpoint_json=?,"
                                "storyboard_id=COALESCE(?,storyboard_id),film_batch_id=COALESCE(?,film_batch_id),"
                                "render_job_id=COALESCE(?,render_job_id),revision=revision+1,updated_at=?,error=NULL "
                                "WHERE id=? AND revision=? AND status='running'",
                                (next_stage, status, json.dumps(completed), json.dumps(checkpoints, ensure_ascii=False),
                                 checkpoint.get("storyboard_id"), checkpoint.get("film_batch_id"), checkpoint.get("render_job_id"), timestamp,
                                 run_id, expected_revision))
            if cursor.rowcount != 1:
                raise RuntimeError("Film run changed while saving its checkpoint")
            db.execute("INSERT INTO film_run_events(run_id,revision,time,level,message) VALUES(?,?,?,?,?)",
                       (run_id, expected_revision + 1, timestamp, "info", message[:2000]))
        return self.get_film_run(run_id)

    def ensure_film_run_shot(self, run_id: str, shot_key: str, shot_request: dict):
        """Persist one logical shot once; repeats with different settings conflict."""
        if not shot_key or len(shot_key) > 200:
            raise ValueError("A stable shot key of at most 200 characters is required")
        encoded = json.dumps(shot_request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        import hashlib
        request_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        timestamp, shot_id = now_iso(), str(uuid4())
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT status,authorization_json,budget_json FROM film_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise KeyError("Film run not found")
            if not json.loads(run["authorization_json"]).get("confirmed"):
                raise FilmRunTransitionError("A shot cannot be planned without initial run authorization")
            existing = db.execute("SELECT * FROM film_run_shots WHERE run_id=? AND shot_key=?", (run_id, shot_key)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise FilmRunConflict("Shot idempotency key was reused with different shot settings")
                item = dict(existing)
                item["shot"] = json.loads(item.pop("shot_json"))
                item["created"] = False
                return item
            budget = json.loads(run["budget_json"])
            count = db.execute("SELECT COUNT(*) FROM film_run_shots WHERE run_id=?", (run_id,)).fetchone()[0]
            if count >= budget["max_shots"]:
                raise FilmRunTransitionError("Film run exceeds the approved shot-count budget")
            db.execute("INSERT INTO film_run_shots(id,run_id,shot_key,request_hash,shot_json,status,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,'planned',?,?)",
                       (shot_id, run_id, shot_key, request_hash, encoded, timestamp, timestamp))
        return {"id": shot_id, "run_id": run_id, "shot_key": shot_key, "request_hash": request_hash,
                "shot": shot_request, "status": "planned", "created": True}

    def reserve_film_run_shot_attempt(self, run_id: str, shot_key: str, attempt_number: int, attempt_request: dict):
        """Reserve a stable per-shot attempt key before a future adapter can submit work."""
        import hashlib
        if attempt_number < 0:
            raise ValueError("Attempt number cannot be negative")
        encoded = json.dumps(attempt_request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT status,stage,authorization_json,budget_json FROM film_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise KeyError("Film run not found")
            if run["status"] != "running" or run["stage"] != "generate_shots":
                raise FilmRunTransitionError("Shot attempts can be reserved only during the generation stage")
            if not json.loads(run["authorization_json"]).get("confirmed"):
                raise FilmRunTransitionError("An authorized generation budget is required")
            shot = db.execute("SELECT id FROM film_run_shots WHERE run_id=? AND shot_key=?", (run_id, shot_key)).fetchone()
            if not shot:
                raise KeyError("Film run shot is not planned")
            budget = json.loads(run["budget_json"])
            if attempt_number > budget["max_retries_per_shot"]:
                raise FilmRunTransitionError("Per-shot retry budget is exhausted")
            existing = db.execute("SELECT * FROM film_run_shot_attempts WHERE run_id=? AND shot_key=? AND attempt_number=?",
                                  (run_id, shot_key, attempt_number)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise FilmRunConflict("Shot attempt idempotency key was reused with different settings")
                return dict(existing) | {"created": False}
            key = str(uuid5(NAMESPACE_URL, f"framework-film-run:{run_id}:{shot_key}:{attempt_number}"))
            attempt_id = str(uuid4())
            db.execute("INSERT INTO film_run_shot_attempts(id,run_id,shot_key,attempt_number,idempotency_key,request_hash,status,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,'reserved',?,?)",
                       (attempt_id, run_id, shot_key, attempt_number, key, request_hash, timestamp, timestamp))
        return {"id": attempt_id, "run_id": run_id, "shot_key": shot_key,
                "attempt_number": attempt_number, "idempotency_key": key,
                "request_hash": request_hash, "status": "reserved", "created": True}

    def create_film_plan(self, project_id: str, title: str, source_story: str, shots: list[dict],
                         target_format: str, preset: str, metadata: dict) -> dict:
        storyboard = self.create_storyboard(project_id, title, shots, source_story)
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO film_plans(storyboard_id,project_id,source_story,planner_mode,target_format,preset,plan_metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (storyboard["id"], project_id, source_story, "deterministic-approved-fable", target_format,
                        preset, json.dumps(metadata, ensure_ascii=False, sort_keys=True), timestamp, timestamp))
        return self.get_storyboard(storyboard["id"])

    def create_film_run_plan(self, run_id: str, title: str, source_story: str, shots: list[dict],
                             scenes: list[dict], target_format: str, preset: str, metadata: dict) -> dict:
        """Atomically persist one authorized run's storyboard and approved plan."""
        timestamp, storyboard_id = now_iso(), str(uuid4())
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT project_id,authorization_json,status FROM film_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise KeyError("Film run not found")
            if not json.loads(run["authorization_json"] or "{}").get("confirmed"):
                raise RuntimeError("A plan cannot be persisted without explicit initial authorization")
            if run["status"] != "running":
                raise RuntimeError("The film run stopped before its plan could be persisted")
            existing = db.execute("SELECT storyboard_id FROM film_plans WHERE film_run_id=?", (run_id,)).fetchone()
            if existing:
                storyboard_id = existing["storyboard_id"]
                db.execute("UPDATE film_runs SET storyboard_id=COALESCE(storyboard_id,?),updated_at=? WHERE id=?",
                           (storyboard_id, timestamp, run_id))
            else:
                project = db.execute("SELECT style_bible_revision FROM projects WHERE id=? AND archived_at IS NULL",
                                     (run["project_id"],)).fetchone()
                if not project:
                    raise KeyError("Film run project is unavailable")
                style_revision = project["style_bible_revision"]
                db.execute("INSERT INTO storyboards(id,project_id,title,revision,story_text,created_at,updated_at) VALUES(?,?,?,1,?,?,?)",
                           (storyboard_id, run["project_id"], title, source_story, timestamp, timestamp))
                scene_ids = {}
                scene_by_title = {scene["title"]: scene for scene in scenes}
                for position, shot in enumerate(shots):
                    scene_title = shot["scene_title"]
                    if scene_title not in scene_ids:
                        scene = scene_by_title[scene_title]
                        scene_id = str(uuid4())
                        scene_ids[scene_title] = scene_id
                        db.execute("INSERT INTO storyboard_scenes(id,storyboard_id,title,position,visual_description,estimated_seconds,transition_after,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                   (scene_id, storyboard_id, scene_title, len(scene_ids)-1,
                                    scene.get("visual_description", ""), scene.get("estimated_seconds", 0),
                                    scene.get("transition_after", "cut"), timestamp, timestamp))
                    db.execute("INSERT INTO storyboard_shots(id,storyboard_id,scene_id,position,title,prompt,reference_image,preset,seed,caption,"
                               "visual_description,characters_json,reference_ids_json,estimated_seconds,dialogue,transition_after,generation_payload_json,"
                               "width,height,fps,output_location,framing,prompt_reviewed,reviewed_style_revision,created_at,updated_at) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                               (shot["id"], storyboard_id, scene_ids[scene_title], position, shot["title"], shot["prompt"],
                                shot.get("reference_image"), shot["preset"], shot.get("seed"), shot.get("caption", ""),
                                shot.get("visual_description", ""), json.dumps(shot.get("characters", [])),
                                json.dumps(shot.get("reference_ids", [])), shot.get("estimated_seconds", 0),
                                shot.get("dialogue", ""), shot.get("transition_after", "cut"),
                                json.dumps(shot["generation_payload"], ensure_ascii=False), shot["width"],
                                shot["height"], shot["fps"], shot["output_location"], shot["framing"],
                                style_revision, timestamp, timestamp))
                db.execute("INSERT INTO film_plans(storyboard_id,project_id,source_story,planner_mode,target_format,preset,plan_metadata_json,"
                           "approved_revision,approved_style_revision,approved_at,created_at,updated_at,film_run_id) "
                           "VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?)",
                           (storyboard_id, run["project_id"], source_story, "local-structured-v1", target_format,
                            preset, json.dumps(metadata, ensure_ascii=False, sort_keys=True), style_revision,
                            timestamp, timestamp, timestamp, run_id))
                self._story_snapshot(db, storyboard_id, 1, title, timestamp)
                db.execute("UPDATE film_runs SET storyboard_id=?,updated_at=? WHERE id=?",
                           (storyboard_id, timestamp, run_id))
        return self.get_storyboard(storyboard_id)

    def approve_film_plan(self, storyboard_id: str, expected_revision: int, style_revision: int):
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            current = db.execute("SELECT b.revision,p.style_bible_revision,fp.plan_metadata_json FROM storyboards b JOIN projects p ON p.id=b.project_id JOIN film_plans fp ON fp.storyboard_id=b.id WHERE b.id=? AND b.archived_at IS NULL", (storyboard_id,)).fetchone()
            if not current:
                raise KeyError("Film plan not found")
            if current["revision"] != expected_revision or current["style_bible_revision"] != style_revision:
                raise RuntimeError("The storyboard or style bible changed; refresh and review the saved plan before approving")
            metadata = json.loads(current["plan_metadata_json"])
            if metadata.get("style_bible_revision") != style_revision:
                raise RuntimeError("The style bible changed after this plan was built; build a new plan before approving")
            cursor = db.execute("UPDATE film_plans SET approved_revision=?,approved_style_revision=?,approved_at=?,updated_at=? WHERE storyboard_id=?",
                                (expected_revision, style_revision, timestamp, timestamp, storyboard_id))
            if cursor.rowcount != 1:
                raise KeyError("Film plan not found")
            db.execute("UPDATE storyboard_shots SET prompt_reviewed=1,reviewed_style_revision=?,updated_at=? WHERE storyboard_id=? AND deleted_at IS NULL",
                       (style_revision, timestamp, storyboard_id))
        return self.get_storyboard(storyboard_id)

    @staticmethod
    def _insert_film_job(db, project_id: str, shot: dict, payload: dict, timestamp: str,
                         storyboard_title: str, batch_id: str, position: int) -> str:
        job_id = str(uuid4())
        job_payload = payload | {"job_id": job_id, "storyboard_id": shot["storyboard_id"],
            "shot_id": shot["id"], "film_batch_id": batch_id,
            "job_label": f"LTX shot {position + 1:02d} · {shot['scene_title']} / {shot['title']}",
            "storyboard_title": storyboard_title, "shot_order": position + 1}
        db.execute("INSERT INTO jobs(id,kind,status,progress,payload,project_id,created_at,updated_at) VALUES(?, 'generation','queued',0,?,?,?,?)",
                   (job_id, json.dumps(job_payload, ensure_ascii=False), project_id, timestamp, timestamp))
        db.execute("INSERT INTO events(job_id,time,level,message) VALUES(?,?,?,?)",
                   (job_id, timestamp, "info", "Approved film shot added to the sequential local queue"))
        snapshot = Database.generation_settings_snapshot(job_payload)
        db.execute("INSERT INTO shot_jobs(shot_id,job_id,settings_snapshot,created_at) VALUES(?,?,?,?)",
                   (shot["id"], job_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), timestamp))
        return job_id

    def create_film_batch(self, storyboard_id: str, expected_revision: int, style_revision: int,
                          film_run_id: str | None = None):
        if film_run_id:
            with self.connect() as db:
                existing = db.execute("SELECT id FROM film_batches WHERE film_run_id=?", (film_run_id,)).fetchone()
                run = db.execute("SELECT status,authorization_json FROM film_runs WHERE id=?", (film_run_id,)).fetchone()
            if existing:
                return self.get_film_batch(existing["id"])
            if not run or run["status"] not in {"running", "waiting_for_planner"} or not json.loads(run["authorization_json"] or "{}").get("confirmed"):
                raise RuntimeError("The film run is stopped or unauthorized; no shot jobs were queued")
        loaded = self.get_storyboard(storyboard_id, include_events=False)
        if not loaded or not loaded.get("film_plan"):
            raise KeyError("Film plan not found")
        if loaded["revision"] != expected_revision or loaded["style_bible_revision"] != style_revision:
            raise RuntimeError("The saved film plan changed; refresh and review it before generating")
        if not loaded["film_plan"]["approved"]:
            raise RuntimeError("Approve this exact saved plan revision before generating")
        timestamp, batch_id = now_iso(), str(uuid4())
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if film_run_id:
                existing = db.execute("SELECT id FROM film_batches WHERE film_run_id=?", (film_run_id,)).fetchone()
                if existing:
                    return self.get_film_batch(existing["id"])
                run = db.execute("SELECT status,authorization_json FROM film_runs WHERE id=?", (film_run_id,)).fetchone()
                if not run or run["status"] != "running" or not json.loads(run["authorization_json"] or "{}").get("confirmed"):
                    raise RuntimeError("The film run is stopped or unauthorized; no shot jobs were queued")
            current = db.execute("SELECT revision,project_id FROM storyboards WHERE id=? AND archived_at IS NULL", (storyboard_id,)).fetchone()
            plan = db.execute("SELECT approved_revision,approved_style_revision FROM film_plans WHERE storyboard_id=?", (storyboard_id,)).fetchone()
            if not current or not plan:
                raise KeyError("Film plan not found")
            if current["revision"] != expected_revision or plan["approved_revision"] != expected_revision or plan["approved_style_revision"] != style_revision:
                raise RuntimeError("The saved film plan changed or its approval is stale")
            active_paused = db.execute("SELECT 1 FROM film_batches b JOIN film_batch_shots bs ON bs.batch_id=b.id JOIN jobs j ON j.id=bs.job_id "
                                       "WHERE b.storyboard_id=? AND b.status='paused' AND j.status IN ('queued','running') LIMIT 1",
                                       (storyboard_id,)).fetchone()
            if active_paused:
                raise RuntimeError("A paused film batch still contains queued work. Reconcile verified shots or cancel the duplicate queue before generating missing shots.")
            db.execute("INSERT INTO film_batches(id,storyboard_id,project_id,storyboard_revision,style_bible_revision,status,created_at,updated_at,film_run_id) VALUES(?,?,?,?,?,'running',?,?,?)",
                       (batch_id, storyboard_id, current["project_id"], expected_revision, style_revision, timestamp, timestamp, film_run_id))
            new_jobs = 0
            for position, shot in enumerate(loaded["shots"]):
                existing = shot.get("job")
                if shot.get("status") in ("queued", "running"):
                    raise RuntimeError(f"Shot '{shot['title']}' already has active generation work")
                output_is_current = bool(existing and existing.get("status") == "completed" and
                    existing.get("settings_match") and (existing.get("verification_metadata") or {}).get("verified_output") and
                    existing.get("result_path") and Path(existing["result_path"]).is_file())
                if output_is_current:
                    db.execute("INSERT INTO film_batch_shots(batch_id,shot_id,position,status,job_id,skip_reason) VALUES(?,?,?,'completed',?,?)",
                               (batch_id, shot["id"], position, existing["id"], "Current output is already verified; no duplicate job queued"))
                    continue
                raw = db.execute("SELECT * FROM storyboard_shots WHERE id=? AND storyboard_id=? AND deleted_at IS NULL", (shot["id"], storyboard_id)).fetchone()
                payload = json.loads(raw["generation_payload_json"] or "{}")
                if not payload.get("prompt") or not payload.get("frames"):
                    raise RuntimeError(f"Shot '{shot['title']}' is missing its validated generation payload")
                active = db.execute("SELECT 1 FROM shot_jobs sj JOIN jobs j ON j.id=sj.job_id WHERE sj.shot_id=? AND j.status IN ('queued','running') LIMIT 1", (shot["id"],)).fetchone()
                if active:
                    raise RuntimeError(f"Shot '{shot['title']}' already has active generation work")
                shot_row = dict(raw) | {"storyboard_id": storyboard_id, "scene_title": shot["scene_title"]}
                job_id = self._insert_film_job(db, current["project_id"], shot_row, payload, timestamp,
                                               loaded["title"], batch_id, position)
                db.execute("INSERT INTO film_batch_shots(batch_id,shot_id,position,status,job_id) VALUES(?,?,?,'queued',?)",
                           (batch_id, shot["id"], position, job_id))
                new_jobs += 1
            if new_jobs == 0:
                db.execute("UPDATE film_batches SET status='completed',updated_at=? WHERE id=?", (timestamp, batch_id))
            db.execute("UPDATE storyboards SET manifest_json=NULL,manifest_path=NULL,updated_at=? WHERE id=?", (timestamp, storyboard_id))
        return self.get_film_batch(batch_id)

    def list_film_batches(self, storyboard_id: str):
        with self.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM film_batches WHERE storyboard_id=? ORDER BY created_at DESC,id DESC", (storyboard_id,))]
        return [self.get_film_batch(batch_id) for batch_id in ids]

    def list_film_batches_for_job(self, job_id: str):
        with self.connect() as db:
            ids = [row[0] for row in db.execute("SELECT DISTINCT batch_id FROM film_batch_shots WHERE job_id=?", (job_id,))]
        return [self.get_film_batch(batch_id) for batch_id in ids]

    def get_film_batch(self, batch_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM film_batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                return None
            batch = dict(row)
            shots = []
            for item in db.execute("SELECT bs.*,s.title,s.scene_id,c.title AS scene_title FROM film_batch_shots bs JOIN storyboard_shots s ON s.id=bs.shot_id LEFT JOIN storyboard_scenes c ON c.id=s.scene_id WHERE bs.batch_id=? ORDER BY bs.position", (batch_id,)):
                shot = dict(item)
                job_row = db.execute("SELECT * FROM jobs WHERE id=?", (shot["job_id"],)).fetchone() if shot.get("job_id") else None
                if job_row:
                    job = self._job(job_row)
                    shot["status"] = job["status"]
                    shot["progress"] = job["progress"]
                    shot["job"] = job | {"events": [dict(event) for event in db.execute("SELECT * FROM events WHERE job_id=? ORDER BY id", (job["id"],))]}
                else:
                    shot["job"] = None
                shots.append(shot)
            batch["shots"] = shots
            batch["total_shots"] = len(shots)
            batch["completed_shots"] = sum(1 for shot in shots if shot["status"] == "completed" and
                                             (not shot.get("job") or (shot["job"].get("verification_metadata") or {}).get("verified_output")))
            batch["failed_shots"] = sum(shot["status"] == "failed" for shot in shots)
            running = sum(shot["status"] == "running" for shot in shots)
            queued = sum(shot["status"] == "queued" for shot in shots)
            partial = sum((shot.get("job") or {}).get("progress", 0) for shot in shots if shot["status"] == "running") / 100
            batch["progress"] = round(100 * (batch["completed_shots"] + partial) / max(1, batch["total_shots"]))
            if batch["status"] == "running" and not running and not queued:
                new_status = "completed" if batch["completed_shots"] == batch["total_shots"] else "failed"
                db.execute("UPDATE film_batches SET status=?,updated_at=? WHERE id=?", (new_status, now_iso(), batch_id))
                batch["status"] = new_status
            batch["running_shots"] = running
            batch["queued_shots"] = queued
            return batch

    def reconcile_film_shots(self, storyboard_id: str, expected_revision: int, style_revision: int,
                             source_batch_id: str, canonical_jobs: dict[str, str], duplicate_job_ids: list[str]):
        """Persist verified shot associations and cancel only queued duplicates from paused batches."""
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            current = db.execute("SELECT b.revision,b.project_id,p.style_bible_revision,fp.approved_revision,fp.approved_style_revision "
                                 "FROM storyboards b JOIN projects p ON p.id=b.project_id JOIN film_plans fp ON fp.storyboard_id=b.id "
                                 "WHERE b.id=? AND b.archived_at IS NULL", (storyboard_id,)).fetchone()
            if not current:
                raise KeyError("Film plan not found")
            if (current["revision"] != expected_revision or current["style_bible_revision"] != style_revision or
                    current["approved_revision"] != expected_revision or current["approved_style_revision"] != style_revision):
                raise RuntimeError("The plan changed after approval; review and approve its current revision before reconciling clips")
            batch = db.execute("SELECT * FROM film_batches WHERE id=? AND storyboard_id=? AND project_id=?",
                               (source_batch_id, storyboard_id, current["project_id"])).fetchone()
            if not batch or batch["status"] != "completed" or batch["storyboard_revision"] != expected_revision or batch["style_bible_revision"] != style_revision:
                raise RuntimeError("The selected source batch is not completed for this exact approved plan revision")
            source = {row["shot_id"]: row["job_id"] for row in db.execute(
                "SELECT shot_id,job_id FROM film_batch_shots WHERE batch_id=?", (source_batch_id,))}
            shots = {row["id"]: dict(row) for row in db.execute(
                "SELECT id,storyboard_id FROM storyboard_shots WHERE storyboard_id=? AND deleted_at IS NULL", (storyboard_id,))}
            if set(source) != set(shots) or canonical_jobs != source:
                raise RuntimeError("The selected completed batch does not map exactly one original job to every required shot")
            for shot_id, job_id in canonical_jobs.items():
                job = db.execute("SELECT kind,status,project_id,payload FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not job or job["kind"] != "generation" or job["status"] != "completed" or job["project_id"] != current["project_id"]:
                    raise RuntimeError("A selected verified shot job changed status or project scope; no associations were changed")
                payload = json.loads(job["payload"] or "{}")
                if payload.get("shot_id") != shot_id or payload.get("storyboard_id") != storyboard_id or payload.get("film_batch_id") != source_batch_id:
                    raise RuntimeError("A selected job's immutable film payload does not match the source batch shot")
                linked = db.execute("SELECT 1 FROM shot_jobs WHERE shot_id=? AND job_id=?", (shot_id, job_id)).fetchone()
                if not linked:
                    snapshot = self.generation_settings_snapshot(payload)
                    db.execute("INSERT INTO shot_jobs(shot_id,job_id,settings_snapshot,created_at) VALUES(?,?,?,?)",
                               (shot_id, job_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), timestamp))
                db.execute("UPDATE storyboard_shots SET reconciled_job_id=?,updated_at=? WHERE id=? AND storyboard_id=?",
                           (job_id, timestamp, shot_id, storyboard_id))
            cancelled = []
            touched_paused_batches = set()
            for job_id in dict.fromkeys(duplicate_job_ids):
                row = db.execute("SELECT j.status,j.project_id,j.payload,b.id batch_id,b.status batch_status,b.storyboard_id "
                                 "FROM jobs j JOIN film_batch_shots bs ON bs.job_id=j.id JOIN film_batches b ON b.id=bs.batch_id "
                                 "WHERE j.id=? AND b.storyboard_id=? AND b.project_id=?",
                                 (job_id, storyboard_id, current["project_id"])).fetchone()
                if not row or row["batch_status"] != "paused" or row["status"] != "queued" or job_id in canonical_jobs.values():
                    continue
                payload = json.loads(row["payload"] or "{}")
                if payload.get("film_batch_id") != row["batch_id"] or payload.get("storyboard_id") != storyboard_id:
                    continue
                db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND status='queued'", (timestamp, job_id))
                db.execute("UPDATE film_batch_shots SET status='cancelled' WHERE batch_id=? AND job_id=?", (row["batch_id"], job_id))
                db.execute("INSERT INTO events(job_id,time,level,message) VALUES(?,?,?,?)",
                           (job_id, timestamp, "info", "Queued duplicate cancelled after verified existing shot was reconciled"))
                cancelled.append(job_id)
                touched_paused_batches.add(row["batch_id"])
            for batch_id in touched_paused_batches:
                remaining = db.execute("SELECT 1 FROM film_batch_shots bs JOIN jobs j ON j.id=bs.job_id "
                                       "WHERE bs.batch_id=? AND j.status IN ('queued','running') LIMIT 1", (batch_id,)).fetchone()
                if not remaining:
                    db.execute("UPDATE film_batches SET status='cancelled',updated_at=? WHERE id=? AND status='paused'",
                               (timestamp, batch_id))
            db.execute("UPDATE storyboards SET manifest_json=NULL,manifest_path=NULL,updated_at=? WHERE id=?", (timestamp, storyboard_id))
        return {"storyboard": self.get_storyboard(storyboard_id, include_events=False), "cancelled_job_ids": cancelled}

    def set_film_batch_status(self, batch_id: str, expected: set[str], status: str):
        with self.lock, self.connect() as db:
            marks = ",".join("?" for _ in expected)
            cursor = db.execute(f"UPDATE film_batches SET status=?,updated_at=? WHERE id=? AND status IN ({marks})",
                                (status, now_iso(), batch_id, *sorted(expected)))
            if cursor.rowcount != 1:
                raise RuntimeError("Film batch is no longer in a state that supports this action")
        return self.get_film_batch(batch_id)

    def cancel_film_batch(self, batch_id: str):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT status FROM film_batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise KeyError("Film batch not found")
            if row["status"] not in ("running", "paused", "failed"):
                raise RuntimeError("Film batch cannot be cancelled in its current state")
            timestamp = now_iso()
            job_ids = [item[0] for item in db.execute("SELECT job_id FROM film_batch_shots WHERE batch_id=? AND job_id IS NOT NULL", (batch_id,))]
            for job_id in job_ids:
                db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND status IN ('queued','running')", (timestamp, job_id))
            db.execute("UPDATE film_batches SET status='cancelled',updated_at=? WHERE id=?", (timestamp, batch_id))
        return self.get_film_batch(batch_id), job_ids

    def retry_film_batch_shots(self, batch_id: str, shot_ids: list[str]):
        if not shot_ids or len(shot_ids) != len(set(shot_ids)):
            raise ValueError("Select one or more distinct failed or cancelled shots")
        batch = self.get_film_batch(batch_id)
        if not batch:
            raise KeyError("Film batch not found")
        if batch["status"] not in ("failed", "cancelled", "paused"):
            raise RuntimeError("Pause or finish the current batch before retrying shots")
        loaded = self.get_storyboard(batch["storyboard_id"], include_events=False)
        if not loaded or not loaded.get("film_plan", {}).get("approved") or loaded["revision"] != batch["storyboard_revision"]:
            raise RuntimeError("The approved storyboard revision changed; review and approve it again before retrying")
        selected = {shot["id"]: shot for shot in loaded["shots"]}
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            batch_rows = {row["shot_id"]: dict(row) for row in db.execute("SELECT * FROM film_batch_shots WHERE batch_id=?", (batch_id,))}
            if any(shot_id not in batch_rows or shot_id not in selected for shot_id in shot_ids):
                raise ValueError("A selected shot does not belong to this film batch")
            for shot_id in shot_ids:
                row = batch_rows[shot_id]
                old_status = row["status"]
                if row.get("job_id"):
                    old = db.execute("SELECT status FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
                    old_status = old["status"] if old else old_status
                if old_status not in ("failed", "cancelled"):
                    raise RuntimeError(f"Shot '{selected[shot_id]['title']}' is not failed or cancelled; completed shots are never duplicated")
                shot = selected[shot_id]
                payload = shot.get("generation_payload") or {}
                if not payload.get("prompt") or not payload.get("reference_image"):
                    raise RuntimeError(f"Shot '{shot['title']}' is missing its saved generation payload")
                active = db.execute("SELECT 1 FROM shot_jobs sj JOIN jobs j ON j.id=sj.job_id WHERE sj.shot_id=? AND j.status IN ('queued','running') LIMIT 1", (shot_id,)).fetchone()
                if active:
                    raise RuntimeError(f"Shot '{shot['title']}' already has active generation work")
                row_shot = {"id": shot_id, "storyboard_id": batch["storyboard_id"], "scene_title": shot["scene_title"], "title": shot["title"]}
                job_id = self._insert_film_job(db, batch["project_id"], row_shot, payload, timestamp, loaded["title"], batch_id, row["position"])
                db.execute("UPDATE film_batch_shots SET job_id=?,status='queued',skip_reason='' WHERE batch_id=? AND shot_id=?", (job_id, batch_id, shot_id))
            db.execute("UPDATE film_batches SET status='running',updated_at=? WHERE id=?", (timestamp, batch_id))
        return self.get_film_batch(batch_id)

    def next_queued_job(self):
        with self.connect() as db:
            row = db.execute("SELECT j.* FROM jobs j WHERE j.status='queued' AND NOT EXISTS "
                "(SELECT 1 FROM film_batch_shots bs JOIN film_batches b ON b.id=bs.batch_id WHERE bs.job_id=j.id AND b.status IN ('paused','cancelled')) "
                "ORDER BY j.created_at ASC,j.rowid ASC LIMIT 1").fetchone()
            return self._job(row)

    def list_storyboards(self, project_id: str | None = None):
        with self.connect() as db:
            if project_id is None:
                ids = [row[0] for row in db.execute("SELECT id FROM storyboards WHERE archived_at IS NULL ORDER BY updated_at DESC,id")]
            else:
                ids = [row[0] for row in db.execute("SELECT id FROM storyboards WHERE project_id=? AND archived_at IS NULL ORDER BY updated_at DESC,id", (project_id,))]
        return [self.get_storyboard(storyboard_id, include_events=False) for storyboard_id in ids]

    def get_storyboard(self, storyboard_id: str, *, include_events: bool = True):
        with self.connect() as db:
            row = db.execute("SELECT * FROM storyboards WHERE id=? AND archived_at IS NULL", (storyboard_id,)).fetchone()
            if row is None:
                return None
            story = dict(row)
            story["manifest"] = json.loads(story.pop("manifest_json")) if story.get("manifest_json") else None
            project_row = db.execute("SELECT style_bible_json,style_bible_revision FROM projects WHERE id=?", (story["project_id"],)).fetchone()
            style_bible = json.loads(project_row["style_bible_json"] or "{}") if project_row else {}
            style_context = "\n".join(
                f"{label}: {style_bible.get(key, '').strip()}" for key, label in (
                    ("material", "Material and finish"), ("silhouettes", "Silhouettes"),
                    ("proportions", "Character proportions"), ("composition", "Composition guidance"),
                    ("negative_constraints", "Negative constraints"),
                ) if style_bible.get(key, "").strip()
            )
            story["style_bible"] = style_bible
            story["style_bible_revision"] = project_row["style_bible_revision"] if project_row else 0
            story["scenes"] = [dict(scene) for scene in db.execute(
                "SELECT id,title,position FROM storyboard_scenes WHERE storyboard_id=? AND deleted_at IS NULL ORDER BY position,id", (storyboard_id,))]
            rows = db.execute("SELECT s.*,c.title AS scene_title FROM storyboard_shots s LEFT JOIN storyboard_scenes c ON c.id=s.scene_id "
                              "WHERE s.storyboard_id=? AND s.deleted_at IS NULL ORDER BY s.position,s.id",
                              (storyboard_id,)).fetchall()
            shots = []
            for item in rows:
                shot = dict(item)
                shot["style_context"] = style_context
                shot["style_bible_revision"] = story["style_bible_revision"]
                linked_rows = db.execute("SELECT j.*,sj.settings_snapshot AS linked_settings_snapshot FROM shot_jobs sj JOIN jobs j ON j.id=sj.job_id WHERE sj.shot_id=? ORDER BY sj.id DESC",
                                         (shot["id"],)).fetchall()
                current_settings = self.generation_settings_snapshot(shot)
                candidates = []
                for linked_row in linked_rows:
                    candidate = self._job(linked_row)
                    if not candidate:
                        continue
                    linked_snapshot = candidate.pop("linked_settings_snapshot", None)
                    snapshot = json.loads(linked_snapshot) if linked_snapshot else {}
                    payload_snapshot = self.generation_settings_snapshot(candidate["payload"])
                    for key, value in payload_snapshot.items():
                        if key not in snapshot and current_settings.get(key) == value:
                            snapshot[key] = value
                    candidate["settings_snapshot"] = snapshot or payload_snapshot
                    candidate["settings_match"] = all(key in current_settings and current_settings[key] == value
                                                       for key, value in candidate["settings_snapshot"].items())
                    verified = (candidate["status"] == "completed" and candidate["exit_code"] == 0 and
                                bool((candidate.get("verification_metadata") or {}).get("verified_output")) and
                                bool(candidate.get("result_path")) and Path(candidate["result_path"]).is_file())
                    candidates.append((candidate, verified and candidate["settings_match"]))
                selected = next((item for item, _ in candidates if item["id"] == shot.get("reconciled_job_id")), None)
                if selected is None:
                    selected = next((item for item, valid in candidates if valid), None)
                if selected is None and candidates:
                    selected = candidates[0][0]
                shot["job"] = selected
                if shot["job"]:
                    shot["settings_match"] = shot["job"]["settings_match"]
                if include_events and shot["job"]:
                    shot["job"]["events"] = [dict(event) for event in db.execute(
                        "SELECT * FROM events WHERE job_id=? ORDER BY id", (shot["job"]["id"],))]
                if not shot["job"]:
                    shot["status"] = "missing"
                elif not shot["job"].get("settings_match", True):
                    shot["status"] = "stale"
                elif shot["job"]["status"] == "completed":
                    verified = (shot["job"].get("verification_metadata") or {}).get("verified_output")
                    shot["status"] = "verified" if verified else "failed"
                else:
                    shot["status"] = shot["job"]["status"]
                review_row = db.execute("SELECT * FROM shot_creative_reviews WHERE shot_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                                        (shot["id"],)).fetchone()
                shot["creative_review"] = None
                shot["creative_review_current"] = False
                if review_row:
                    review = dict(review_row)
                    review["checklist"] = json.loads(review.pop("checklist_json"))
                    shot["creative_review"] = review
                    continuity_row = db.execute("SELECT approved_revision FROM project_scene_continuity WHERE project_id=?",
                                                 (story["project_id"],)).fetchone()
                    approved_continuity = continuity_row["approved_revision"] if continuity_row else None
                    shot["creative_review_current"] = bool(
                        shot["job"] and review["job_id"] == shot["job"]["id"] and
                        review["storyboard_revision"] == story["revision"] and
                        approved_continuity == review["continuity_revision"])
                shots.append(shot)
            story["shots"] = shots
            for shot in story["shots"]:
                for field, default in (("characters_json", []), ("reference_ids_json", []), ("generation_payload_json", {})):
                    raw = shot.pop(field, None)
                    try:
                        shot[field.removesuffix("_json")] = json.loads(raw) if raw else default
                    except (TypeError, json.JSONDecodeError):
                        shot[field.removesuffix("_json")] = default
            plan = db.execute("SELECT * FROM film_plans WHERE storyboard_id=?", (storyboard_id,)).fetchone()
            story["film_plan"] = None
            if plan:
                story["film_plan"] = dict(plan) | {"metadata": json.loads(plan["plan_metadata_json"])}
                story["film_plan"].pop("plan_metadata_json", None)
                story["film_plan"]["approved"] = (plan["approved_revision"] == story["revision"] and
                    plan["approved_style_revision"] == story["style_bible_revision"])
            story["shot_count"] = len(shots)
            return story

    def update_storyboard(self, storyboard_id: str, expected_revision: int, title: str, shots: list[dict], story_text: str = ""):
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            story = db.execute("SELECT revision,title FROM storyboards WHERE id=? AND archived_at IS NULL", (storyboard_id,)).fetchone()
            if story is None:
                raise KeyError("Storyboard not found")
            if story["revision"] != expected_revision:
                raise RuntimeError("Storyboard changed since it was loaded; refresh before saving")
            current = {row["id"]: dict(row) for row in db.execute(
                "SELECT * FROM storyboard_shots WHERE storyboard_id=? AND deleted_at IS NULL", (storyboard_id,))}
            existing_scenes = {row["title"]: dict(row) for row in db.execute(
                "SELECT * FROM storyboard_scenes WHERE storyboard_id=?", (storyboard_id,))}
            incoming_ids = {shot["id"] for shot in shots if shot.get("id")}
            if not incoming_ids.issubset(current):
                raise ValueError("Shot id does not belong to this storyboard")
            removed_ids = set(current) - incoming_ids
            if removed_ids:
                marks = ",".join("?" for _ in removed_ids)
                active = db.execute(f"SELECT 1 FROM shot_jobs sj JOIN jobs j ON j.id=sj.job_id WHERE sj.shot_id IN ({marks}) AND j.status IN ('queued','running') LIMIT 1", tuple(removed_ids)).fetchone()
                if active:
                    raise RuntimeError("Cancel active shot jobs before removing those shots")
                db.executemany("UPDATE storyboard_shots SET deleted_at=?,updated_at=? WHERE id=?",
                               [(timestamp, timestamp, shot_id) for shot_id in removed_ids])
            # Move positions to a unique temporary range before assigning the new order.
            db.execute("UPDATE storyboard_shots SET position=position+1000 WHERE storyboard_id=? AND deleted_at IS NULL", (storyboard_id,))
            scene_ids = {}
            for shot in shots:
                scene_title = (shot.get("scene_title") or "Scene 1").strip()
                if not scene_title:
                    scene_title = "Scene 1"
                if scene_title in scene_ids:
                    continue
                saved_scene = existing_scenes.get(scene_title)
                if saved_scene:
                    scene_id = saved_scene["id"]
                    db.execute("UPDATE storyboard_scenes SET position=?,deleted_at=NULL,updated_at=? WHERE id=?",
                               (len(scene_ids), timestamp, scene_id))
                else:
                    scene_id = str(uuid4())
                    db.execute("INSERT INTO storyboard_scenes(id,storyboard_id,title,position,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                               (scene_id, storyboard_id, scene_title, len(scene_ids), timestamp, timestamp))
                scene_ids[scene_title] = scene_id
            active_scene_ids = tuple(scene_ids.values())
            if active_scene_ids:
                marks = ",".join("?" for _ in active_scene_ids)
                db.execute(f"UPDATE storyboard_scenes SET deleted_at=? WHERE storyboard_id=? AND id NOT IN ({marks}) AND deleted_at IS NULL",
                           (timestamp, storyboard_id, *active_scene_ids))
            else:
                db.execute("UPDATE storyboard_scenes SET deleted_at=? WHERE storyboard_id=? AND deleted_at IS NULL", (timestamp, storyboard_id))
            for position, shot in enumerate(shots):
                scene_title = (shot.get("scene_title") or "Scene 1").strip() or "Scene 1"
                scene_id = scene_ids[scene_title]
                if shot.get("id"):
                    db.execute("UPDATE storyboard_shots SET scene_id=?,position=?,title=?,prompt=?,reference_image=?,preset=?,seed=?,caption=?,width=?,height=?,fps=?,output_location=?,framing=?,prompt_reviewed=?,reviewed_style_revision=?,visual_description=?,characters_json=?,reference_ids_json=?,estimated_seconds=?,dialogue=?,transition_after=?,generation_payload_json=?,reconciled_job_id=NULL,updated_at=? WHERE id=?",
                               (scene_id, position, shot["title"], shot["prompt"], shot.get("reference_image"), shot["preset"], shot.get("seed"), shot.get("caption", ""),
                                shot.get("width"), shot.get("height"), shot.get("fps"), shot.get("output_location"), shot.get("framing", ""), int(shot.get("prompt_reviewed", False)), shot.get("reviewed_style_revision", 0),
                                shot.get("visual_description", ""), json.dumps(shot.get("characters", [])), json.dumps(shot.get("reference_ids", [])), shot.get("estimated_seconds", 0), shot.get("dialogue", ""), shot.get("transition_after", "dissolve"), json.dumps(shot.get("generation_payload", {}), ensure_ascii=False), timestamp, shot["id"]))
                else:
                    db.execute("INSERT INTO storyboard_shots(id,storyboard_id,scene_id,position,title,prompt,reference_image,preset,seed,caption,width,height,fps,output_location,framing,prompt_reviewed,reviewed_style_revision,visual_description,characters_json,reference_ids_json,estimated_seconds,dialogue,transition_after,generation_payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (str(uuid4()), storyboard_id, scene_id, position, shot["title"], shot["prompt"], shot.get("reference_image"), shot["preset"], shot.get("seed"), shot.get("caption", ""),
                                shot.get("width"), shot.get("height"), shot.get("fps"), shot.get("output_location"), shot.get("framing", ""), int(shot.get("prompt_reviewed", False)), shot.get("reviewed_style_revision", 0),
                                shot.get("visual_description", ""), json.dumps(shot.get("characters", [])), json.dumps(shot.get("reference_ids", [])), shot.get("estimated_seconds", 0), shot.get("dialogue", ""), shot.get("transition_after", "dissolve"), json.dumps(shot.get("generation_payload", {}), ensure_ascii=False), timestamp, timestamp))
            revision = expected_revision + 1
            db.execute("UPDATE storyboards SET title=?,story_text=?,revision=?,manifest_json=NULL,manifest_path=NULL,updated_at=? WHERE id=?",
                       (title, story_text, revision, timestamp, storyboard_id))
            self._story_snapshot(db, storyboard_id, revision, title, timestamp)
        return self.get_storyboard(storyboard_id)

    def delete_storyboard_shot(self, storyboard_id: str, shot_id: str, expected_revision: int):
        story = self.get_storyboard(storyboard_id, include_events=False)
        if story is None:
            raise KeyError("Storyboard not found")
        if story["revision"] != expected_revision:
            raise RuntimeError("Storyboard changed since it was loaded; refresh before saving")
        shots = [shot for shot in story["shots"] if shot["id"] != shot_id]
        if len(shots) == len(story["shots"]):
            raise KeyError("Shot not found")
        return self.update_storyboard(storyboard_id, expected_revision, story["title"], shots, story.get("story_text", ""))

    def archive_storyboard(self, storyboard_id: str):
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            active = db.execute("SELECT 1 FROM storyboard_shots s JOIN shot_jobs sj ON sj.shot_id=s.id JOIN jobs j ON j.id=sj.job_id "
                                "WHERE s.storyboard_id=? AND j.status IN ('queued','running') LIMIT 1", (storyboard_id,)).fetchone()
            if active:
                raise RuntimeError("Cancel active shot jobs before archiving this storyboard")
            cursor = db.execute("UPDATE storyboards SET archived_at=?,updated_at=? WHERE id=? AND archived_at IS NULL", (timestamp, timestamp, storyboard_id))
            if cursor.rowcount != 1:
                raise KeyError("Storyboard not found")

    def storyboard_revisions(self, storyboard_id: str):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM storyboards WHERE id=?", (storyboard_id,)).fetchone():
                return None
            return [dict(row) | {"snapshot": json.loads(row["snapshot_json"])} for row in db.execute(
                "SELECT * FROM storyboard_revisions WHERE storyboard_id=? ORDER BY revision", (storyboard_id,))]

    def store_storyboard_manifest(self, storyboard_id: str, manifest: dict, manifest_path: str):
        with self.lock, self.connect() as db:
            cursor = db.execute("UPDATE storyboards SET manifest_json=?,manifest_path=?,updated_at=? WHERE id=? AND archived_at IS NULL",
                                (json.dumps(manifest, ensure_ascii=False, sort_keys=True), manifest_path, now_iso(), storyboard_id))
            if cursor.rowcount != 1:
                raise KeyError("Storyboard not found")
        return self.get_storyboard(storyboard_id, include_events=False)

    def invalidate_storyboard_manifest(self, job_id: str):
        with self.lock, self.connect() as db:
            db.execute("UPDATE storyboards SET manifest_json=NULL,manifest_path=NULL,updated_at=? WHERE id IN "
                       "(SELECT s.storyboard_id FROM storyboard_shots s JOIN shot_jobs sj ON sj.shot_id=s.id WHERE sj.job_id=?)",
                       (now_iso(), job_id))

    def create_shot_job(self, storyboard_id: str, shot_id: str, payload: dict):
        job_id, timestamp = str(uuid4()), now_iso()
        payload["job_id"] = job_id
        with self.lock, self.connect() as db:
            shot = db.execute("SELECT b.project_id FROM storyboard_shots s JOIN storyboards b ON b.id=s.storyboard_id "
                              "WHERE s.id=? AND s.storyboard_id=? AND s.deleted_at IS NULL AND b.archived_at IS NULL",
                              (shot_id, storyboard_id)).fetchone()
            if not shot:
                raise KeyError("Shot not found")
            active = db.execute("SELECT 1 FROM shot_jobs sj JOIN jobs j ON j.id=sj.job_id WHERE sj.shot_id=? AND j.status IN ('queued','running') LIMIT 1", (shot_id,)).fetchone()
            if active:
                raise RuntimeError("This shot already has a queued or running job")
            project_id = shot["project_id"]
            db.execute("INSERT INTO jobs(id,kind,status,progress,payload,project_id,created_at,updated_at) VALUES(?, 'generation','queued',0,?,?,?,?)",
                       (job_id, json.dumps(payload), project_id, timestamp, timestamp))
            db.execute("INSERT INTO events(job_id,time,level,message) VALUES(?,?,?,?)", (job_id, timestamp, "info", "Shot generation added to local queue"))
            snapshot = self.generation_settings_snapshot(payload)
            db.execute("INSERT INTO shot_jobs(shot_id,job_id,settings_snapshot,created_at) VALUES(?,?,?,?)",
                       (shot_id, job_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), timestamp))
            db.execute("UPDATE storyboards SET manifest_json=NULL,manifest_path=NULL,updated_at=? WHERE id=?", (timestamp, storyboard_id))
        return self.get_job(job_id)

    def link_existing_shot_job(self, storyboard_id: str, shot_id: str, job_id: str):
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            shot = db.execute("SELECT s.*,b.project_id FROM storyboard_shots s JOIN storyboards b ON b.id=s.storyboard_id "
                              "WHERE s.id=? AND s.storyboard_id=? AND s.deleted_at IS NULL AND b.archived_at IS NULL",
                              (shot_id, storyboard_id)).fetchone()
            if not shot:
                raise KeyError("Shot not found")
            job_row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job_row:
                raise KeyError("Generation job not found")
            job = self._job(job_row)
            if job["kind"] != "generation" or job["status"] != "completed":
                raise RuntimeError("Only a completed generation job can be linked as an existing shot output")
            if job.get("project_id") != shot["project_id"]:
                raise RuntimeError("Generation job belongs to another project or has no project assignment")
            if db.execute("SELECT 1 FROM shot_jobs WHERE job_id=?", (job_id,)).fetchone():
                raise RuntimeError("This generation job is already linked to a storyboard shot")
            snapshot = self.generation_settings_snapshot(job["payload"])
            current_snapshot = self.generation_settings_snapshot(dict(shot))
            if any(key in current_snapshot and current_snapshot[key] != value for key, value in snapshot.items()):
                raise RuntimeError("Existing output settings do not match this shot; copy the job settings into the shot and save first")
            snapshot = {key: value for key, value in snapshot.items() if key in current_snapshot}
            db.execute("INSERT INTO shot_jobs(shot_id,job_id,settings_snapshot,created_at) VALUES(?,?,?,?)",
                       (shot_id, job_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), timestamp))
            db.execute("UPDATE storyboards SET manifest_json=NULL,manifest_path=NULL,updated_at=? WHERE id=?",
                       (timestamp, storyboard_id))
        return self.get_storyboard(storyboard_id, include_events=False)
