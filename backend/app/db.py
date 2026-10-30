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
            """)
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
        return item

    def get_job(self, job_id: str):
        with self.connect() as db:
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list_jobs(self, limit: int = 100):
        with self.connect() as db:
            return [self._job(row) for row in db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def update_job(self, job_id: str, **values):
        values["updated_at"] = now_iso()
        keys = list(values)
        encoded = [json.dumps(values[k]) if k == "payload" else values[k] for k in keys]
        with self.lock, self.connect() as db:
            db.execute(f"UPDATE jobs SET {', '.join(k+'=?' for k in keys)} WHERE id=?", (*encoded, job_id))
        return self.get_job(job_id)

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
