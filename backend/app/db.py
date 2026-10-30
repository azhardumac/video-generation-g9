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
        with self.connect() as db:
            db.executescript("""
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
                    created_at TEXT NOT NULL, imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_assets (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, category TEXT NOT NULL,
                    relative_path TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    modified_at REAL NOT NULL, mime_type TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS storyboards (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1, archived_at TEXT,
                    manifest_json TEXT, manifest_path TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS storyboard_shots (
                    id TEXT PRIMARY KEY, storyboard_id TEXT NOT NULL,
                    scene_id TEXT, position INTEGER NOT NULL, title TEXT NOT NULL,
                    prompt TEXT NOT NULL, reference_image TEXT,
                    preset TEXT NOT NULL, seed INTEGER, caption TEXT NOT NULL DEFAULT '',
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
            """)
            shot_columns = {row[1] for row in db.execute("PRAGMA table_info(storyboard_shots)")}
            if "scene_id" not in shot_columns:
                db.execute("ALTER TABLE storyboard_shots ADD COLUMN scene_id TEXT")
            shot_job_columns = {row[1] for row in db.execute("PRAGMA table_info(shot_jobs)")}
            if "settings_snapshot" not in shot_job_columns:
                db.execute("ALTER TABLE shot_jobs ADD COLUMN settings_snapshot TEXT")
            existing = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            for column, declaration in (
                ("exit_code", "INTEGER"),
                ("elapsed_seconds", "REAL"),
                ("verification_metadata", "TEXT"),
                ("partial_output_path", "TEXT"),
            ):
                if column not in existing:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {declaration}")
            db.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='running'", (now_iso(),))

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

    def create_job(self, kind: str, payload: dict) -> dict:
        job_id, now = str(uuid4()), now_iso()
        payload["job_id"] = job_id
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO jobs(id,kind,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       (job_id, kind, "queued", json.dumps(payload), now, now))
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
        return {
            "prompt": payload.get("prompt", ""),
            "reference_image": payload.get("reference_image") or None,
            "seed": payload.get("seed"),
            "preset": payload.get("preset", "Short"),
        }

    def get_job(self, job_id: str):
        with self.connect() as db:
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list_jobs(self, limit: int = 100):
        with self.connect() as db:
            return [self._job(row) for row in db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]

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

    def import_project(self, project_id: str, name: str, root: str, assets: list[dict]):
        now = now_iso()
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO projects(id,name,root,created_at,imported_at) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET name=excluded.name,root=excluded.root,imported_at=excluded.imported_at",
                       (project_id, name, root, now, now))
            db.execute("DELETE FROM project_assets WHERE project_id=?", (project_id,))
            db.executemany("INSERT INTO project_assets(id,project_id,category,relative_path,size_bytes,modified_at,mime_type) VALUES(?,?,?,?,?,?,?)",
                           [(a["id"], project_id, a["category"], a["relative_path"], a["size_bytes"], a["modified_at"], a["mime_type"]) for a in assets])

    def list_projects(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY imported_at DESC")]

    def get_project(self, project_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            return dict(row) if row else None

    def project_assets(self, project_id: str):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM project_assets WHERE project_id=? ORDER BY category,relative_path", (project_id,))]

    def get_project_asset(self, project_id: str, asset_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM project_assets WHERE project_id=? AND id=?", (project_id, asset_id)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _story_snapshot(db, storyboard_id: str, revision: int, title: str, timestamp: str):
        scenes = [dict(row) for row in db.execute(
            "SELECT id,title,position FROM storyboard_scenes WHERE storyboard_id=? AND deleted_at IS NULL ORDER BY position,id", (storyboard_id,))]
        shots = [dict(row) for row in db.execute(
            "SELECT s.id,s.scene_id,c.title AS scene_title,s.position,s.title,s.prompt,s.reference_image,s.preset,s.seed,s.caption "
            "FROM storyboard_shots s LEFT JOIN storyboard_scenes c ON c.id=s.scene_id "
            "WHERE s.storyboard_id=? AND s.deleted_at IS NULL ORDER BY s.position,s.id", (storyboard_id,))]
        db.execute("INSERT INTO storyboard_revisions(storyboard_id,revision,title,snapshot_json,created_at) VALUES(?,?,?,?,?)",
                   (storyboard_id, revision, title, json.dumps({"scenes": scenes, "shots": shots}, ensure_ascii=False, sort_keys=True), timestamp))

    def create_storyboard(self, project_id: str, title: str, shots: list[dict]) -> dict:
        storyboard_id, timestamp = str(uuid4()), now_iso()
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO storyboards(id,project_id,title,revision,created_at,updated_at) VALUES(?,?,?,1,?,?)",
                       (storyboard_id, project_id, title, timestamp, timestamp))
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

    def list_storyboards(self):
        with self.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM storyboards WHERE archived_at IS NULL ORDER BY updated_at DESC,id")]
        return [self.get_storyboard(storyboard_id, include_events=False) for storyboard_id in ids]

    def get_storyboard(self, storyboard_id: str, *, include_events: bool = True):
        with self.connect() as db:
            row = db.execute("SELECT * FROM storyboards WHERE id=? AND archived_at IS NULL", (storyboard_id,)).fetchone()
            if row is None:
                return None
            story = dict(row)
            story["manifest"] = json.loads(story.pop("manifest_json")) if story.get("manifest_json") else None
            story["scenes"] = [dict(scene) for scene in db.execute(
                "SELECT id,title,position FROM storyboard_scenes WHERE storyboard_id=? AND deleted_at IS NULL ORDER BY position,id", (storyboard_id,))]
            rows = db.execute("SELECT s.*,c.title AS scene_title FROM storyboard_shots s LEFT JOIN storyboard_scenes c ON c.id=s.scene_id "
                              "WHERE s.storyboard_id=? AND s.deleted_at IS NULL ORDER BY s.position,s.id",
                              (storyboard_id,)).fetchall()
            shots = []
            for item in rows:
                shot = dict(item)
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
                shot["status"] = shot["job"]["status"] if shot["job"] else "planned"
                shots.append(shot)
            story["shots"] = shots
            story["shot_count"] = len(shots)
            return story

    def update_storyboard(self, storyboard_id: str, expected_revision: int, title: str, shots: list[dict]):
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
                    db.execute("UPDATE storyboard_shots SET scene_id=?,position=?,title=?,prompt=?,reference_image=?,preset=?,seed=?,caption=?,updated_at=? WHERE id=?",
                               (scene_id, position, shot["title"], shot["prompt"], shot.get("reference_image"), shot["preset"], shot.get("seed"), shot.get("caption", ""), timestamp, shot["id"]))
                else:
                    db.execute("INSERT INTO storyboard_shots(id,storyboard_id,scene_id,position,title,prompt,reference_image,preset,seed,caption,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                               (str(uuid4()), storyboard_id, scene_id, position, shot["title"], shot["prompt"], shot.get("reference_image"), shot["preset"], shot.get("seed"), shot.get("caption", ""), timestamp, timestamp))
            revision = expected_revision + 1
            db.execute("UPDATE storyboards SET title=?,revision=?,manifest_json=NULL,manifest_path=NULL,updated_at=? WHERE id=?",
                       (title, revision, timestamp, storyboard_id))
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
        return self.update_storyboard(storyboard_id, expected_revision, story["title"], shots)

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
            shot = db.execute("SELECT 1 FROM storyboard_shots s JOIN storyboards b ON b.id=s.storyboard_id "
                              "WHERE s.id=? AND s.storyboard_id=? AND s.deleted_at IS NULL AND b.archived_at IS NULL",
                              (shot_id, storyboard_id)).fetchone()
            if not shot:
                raise KeyError("Shot not found")
            active = db.execute("SELECT 1 FROM shot_jobs sj JOIN jobs j ON j.id=sj.job_id WHERE sj.shot_id=? AND j.status IN ('queued','running') LIMIT 1", (shot_id,)).fetchone()
            if active:
                raise RuntimeError("This shot already has a queued or running job")
            db.execute("INSERT INTO jobs(id,kind,status,progress,payload,created_at,updated_at) VALUES(?, 'generation','queued',0,?,?,?)",
                       (job_id, json.dumps(payload), timestamp, timestamp))
            db.execute("INSERT INTO events(job_id,time,level,message) VALUES(?,?,?,?)", (job_id, timestamp, "info", "Shot generation added to local queue"))
            snapshot = self.generation_settings_snapshot(payload)
            db.execute("INSERT INTO shot_jobs(shot_id,job_id,settings_snapshot,created_at) VALUES(?,?,?,?)",
                       (shot_id, job_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), timestamp))
            db.execute("UPDATE storyboards SET manifest_json=NULL,manifest_path=NULL,updated_at=? WHERE id=?", (timestamp, storyboard_id))
        return self.get_job(job_id)

    def link_existing_shot_job(self, storyboard_id: str, shot_id: str, job_id: str):
        timestamp = now_iso()
        with self.lock, self.connect() as db:
            shot = db.execute("SELECT s.* FROM storyboard_shots s JOIN storyboards b ON b.id=s.storyboard_id "
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
