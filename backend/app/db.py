import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4
from contextlib import contextmanager

from .config import Settings
from .paths import safe_local_path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, settings: Settings):
        self.path = safe_local_path(settings.database_path, settings)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()
        self._backup_before_migration(target_version=2)
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
            """)
            shot_columns = {row[1] for row in db.execute("PRAGMA table_info(storyboard_shots)")}
            if "scene_id" not in shot_columns:
                db.execute("ALTER TABLE storyboard_shots ADD COLUMN scene_id TEXT")
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
            ):
                if column not in shot_columns:
                    db.execute(f"ALTER TABLE storyboard_shots ADD COLUMN {column} {declaration}")
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
        snapshot = {
            "prompt": payload.get("shot_prompt", payload.get("prompt", "")),
            "reference_image": payload.get("reference_image") or None,
            "seed": payload.get("seed"),
            "preset": payload.get("preset", "Short"),
        }
        shot_settings = payload.get("shot_settings", payload if "id" in payload else {})
        for key in ("style_context", "style_bible_revision", "width", "height", "fps", "output_location", "framing", "aspect_ratio"):
            value = shot_settings.get(key)
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
            "s.width,s.height,s.fps,s.output_location,s.framing,s.prompt_reviewed,s.reviewed_style_revision "
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
                db.execute("INSERT INTO storyboard_shots(id,storyboard_id,scene_id,position,title,prompt,reference_image,preset,seed,caption,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (shot.get("id") or str(uuid4()), storyboard_id, scene_ids[scene_title], position, shot["title"], shot["prompt"],
                            shot.get("reference_image"), shot.get("preset", "Short"), shot.get("seed"),
                            shot.get("caption", ""), timestamp, timestamp))
            self._story_snapshot(db, storyboard_id, 1, title, timestamp)
        return self.get_storyboard(storyboard_id)

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
                linked = db.execute("SELECT j.*,sj.settings_snapshot AS linked_settings_snapshot FROM shot_jobs sj JOIN jobs j ON j.id=sj.job_id WHERE sj.shot_id=? ORDER BY sj.id DESC LIMIT 1",
                                    (shot["id"],)).fetchone()
                shot["job"] = self._job(linked)
                if shot["job"]:
                    linked_snapshot = shot["job"].pop("linked_settings_snapshot", None)
                    snapshot = json.loads(linked_snapshot) if linked_snapshot else self.generation_settings_snapshot(shot["job"]["payload"])
                    current_settings = self.generation_settings_snapshot(shot)
                    shot["job"]["settings_snapshot"] = snapshot
                    shot["job"]["settings_match"] = snapshot == current_settings
                    shot["settings_match"] = snapshot == current_settings
                if include_events and linked:
                    shot["job"]["events"] = [dict(event) for event in db.execute(
                        "SELECT * FROM events WHERE job_id=? ORDER BY id", (linked["id"],))]
                if not shot["job"]:
                    shot["status"] = "missing"
                elif not shot["job"].get("settings_match", True):
                    shot["status"] = "stale"
                elif shot["job"]["status"] == "completed":
                    verified = (shot["job"].get("verification_metadata") or {}).get("verified_output")
                    shot["status"] = "verified" if verified else "failed"
                else:
                    shot["status"] = shot["job"]["status"]
                shots.append(shot)
            story["shots"] = shots
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
                    db.execute("UPDATE storyboard_shots SET scene_id=?,position=?,title=?,prompt=?,reference_image=?,preset=?,seed=?,caption=?,width=?,height=?,fps=?,output_location=?,framing=?,prompt_reviewed=?,reviewed_style_revision=?,updated_at=? WHERE id=?",
                               (scene_id, position, shot["title"], shot["prompt"], shot.get("reference_image"), shot["preset"], shot.get("seed"), shot.get("caption", ""),
                                shot.get("width"), shot.get("height"), shot.get("fps"), shot.get("output_location"), shot.get("framing", ""), int(shot.get("prompt_reviewed", False)), shot.get("reviewed_style_revision", 0), timestamp, shot["id"]))
                else:
                    db.execute("INSERT INTO storyboard_shots(id,storyboard_id,scene_id,position,title,prompt,reference_image,preset,seed,caption,width,height,fps,output_location,framing,prompt_reviewed,reviewed_style_revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (str(uuid4()), storyboard_id, scene_id, position, shot["title"], shot["prompt"], shot.get("reference_image"), shot["preset"], shot.get("seed"), shot.get("caption", ""),
                                shot.get("width"), shot.get("height"), shot.get("fps"), shot.get("output_location"), shot.get("framing", ""), int(shot.get("prompt_reviewed", False)), shot.get("reviewed_style_revision", 0), timestamp, timestamp))
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
            if snapshot != self.generation_settings_snapshot(dict(shot)):
                raise RuntimeError("Existing output settings do not match this shot; copy the job settings into the shot and save first")
            db.execute("INSERT INTO shot_jobs(shot_id,job_id,settings_snapshot,created_at) VALUES(?,?,?,?)",
                       (shot_id, job_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), timestamp))
            db.execute("UPDATE storyboards SET manifest_json=NULL,manifest_path=NULL,updated_at=? WHERE id=?",
                       (timestamp, storyboard_id))
        return self.get_storyboard(storyboard_id, include_events=False)
