"""SQLite persistence for jobs and encrypted site profiles."""

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "media-fetcher.db"
DOWNLOAD_ROOT = ROOT / "data" / "downloads"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_TRANSCRIPT_STATUSES = {"queued", "preparing", "transcribing"}

TRANSCRIPT_COLUMNS = {
    "transcript_status": "TEXT",
    "transcript_phase": "TEXT",
    "transcript_progress": "REAL NOT NULL DEFAULT 0",
    "transcript_language": "TEXT NOT NULL DEFAULT 'auto'",
    "transcript_text": "TEXT",
    "transcript_error": "TEXT",
    "transcript_path": "TEXT",
    "transcript_segments": "INTEGER",
    "transcript_completed_segments": "INTEGER NOT NULL DEFAULT 0",
}


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DB_PATH), timeout=10)
    DB_PATH.chmod(0o600)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def init() -> None:
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.chmod(0o700)
    DOWNLOAD_ROOT.chmod(0o700)
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id               TEXT PRIMARY KEY,
              user_id          TEXT NOT NULL,
              source_url       TEXT NOT NULL,
              source_host      TEXT NOT NULL,
              site             TEXT,
              mode             TEXT NOT NULL,
              playlist         INTEGER NOT NULL DEFAULT 0,
              subtitles        INTEGER NOT NULL DEFAULT 0,
              status           TEXT NOT NULL,
              title            TEXT,
              extractor        TEXT,
              duration         REAL,
              item_count       INTEGER,
              thumbnail        TEXT,
              progress         REAL NOT NULL DEFAULT 0,
              downloaded_bytes INTEGER,
              total_bytes      INTEGER,
              speed            REAL,
              eta              REAL,
              error            TEXT,
              output_path      TEXT,
              output_name      TEXT,
              output_size      INTEGER,
              meta_json        TEXT,
              created_at       REAL NOT NULL,
              updated_at       REAL NOT NULL,
              expires_at       REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_user_created
              ON jobs(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_status
              ON jobs(status, created_at);

            CREATE TABLE IF NOT EXISTS site_profiles (
              user_id          TEXT NOT NULL,
              site             TEXT NOT NULL,
              cookie_encrypted TEXT,
              proxy_encrypted  TEXT,
              updated_at       REAL NOT NULL,
              PRIMARY KEY (user_id, site)
            );
            """
        )
        existing = {
            row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
        }
        for name, declaration in TRANSCRIPT_COLUMNS.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")


def create_job(
    user_id: str,
    source_url: str,
    source_host: str,
    site: str | None,
    mode: str,
    playlist: bool,
    subtitles: bool,
    ttl_hours: float,
) -> dict:
    now = time.time()
    job_id = uuid.uuid4().hex
    with connect() as connection:
        connection.execute(
            """INSERT INTO jobs(
              id,user_id,source_url,source_host,site,mode,playlist,subtitles,status,
              created_at,updated_at,expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                str(user_id),
                source_url,
                source_host,
                site,
                mode,
                int(playlist),
                int(subtitles),
                "queued",
                now,
                now,
                now + ttl_hours * 3600,
            ),
        )
    return get_job(job_id, str(user_id))  # type: ignore[return-value]


def get_job(job_id: str, user_id: str | None = None) -> dict | None:
    query = "SELECT * FROM jobs WHERE id=?"
    values: tuple[Any, ...] = (job_id,)
    if user_id is not None:
        query += " AND user_id=?"
        values += (str(user_id),)
    with connect() as connection:
        row = connection.execute(query, values).fetchone()
    return _job(dict(row)) if row else None


def list_jobs(user_id: str, limit: int = 100) -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (str(user_id), limit),
        ).fetchall()
    return [_job(dict(row)) for row in rows]


def queued_jobs() -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at"
        ).fetchall()
    return [_job(dict(row)) for row in rows]


def queued_transcriptions() -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM jobs WHERE transcript_status='queued' ORDER BY updated_at"
        ).fetchall()
    return [_job(dict(row)) for row in rows]


def update_job(job_id: str, **values: Any) -> dict | None:
    if not values:
        return get_job(job_id)
    values["updated_at"] = time.time()
    allowed = {
        "status",
        "title",
        "extractor",
        "duration",
        "item_count",
        "thumbnail",
        "progress",
        "downloaded_bytes",
        "total_bytes",
        "speed",
        "eta",
        "error",
        "output_path",
        "output_name",
        "output_size",
        "meta_json",
        "transcript_status",
        "transcript_phase",
        "transcript_progress",
        "transcript_language",
        "transcript_text",
        "transcript_error",
        "transcript_path",
        "transcript_segments",
        "transcript_completed_segments",
        "updated_at",
    }
    if set(values) - allowed:
        raise ValueError("invalid job update")
    if "meta_json" in values and not isinstance(values["meta_json"], str):
        values["meta_json"] = json.dumps(values["meta_json"], ensure_ascii=False)
    assignments = ",".join(f"{key}=?" for key in values)
    with connect() as connection:
        connection.execute(
            f"UPDATE jobs SET {assignments} WHERE id=?",
            (*values.values(), job_id),
        )
    return get_job(job_id)


def delete_job(job_id: str, user_id: str) -> bool:
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM jobs WHERE id=? AND user_id=?", (job_id, str(user_id))
        )
    return cursor.rowcount > 0


def mark_interrupted() -> None:
    now = time.time()
    with connect() as connection:
        connection.execute(
            """UPDATE jobs SET status='failed', error=?, updated_at=?
               WHERE status IN ('probing','downloading','processing')""",
            ("服务重启，任务已中断，请重新提交", now),
        )
        connection.execute(
            """UPDATE jobs SET transcript_status='failed', transcript_phase='已中断',
                      transcript_error=?, updated_at=?
               WHERE transcript_status IN ('preparing','transcribing')""",
            ("服务重启，转写已中断，请重新开始", now),
        )


def profile(user_id: str, site: str) -> dict | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM site_profiles WHERE user_id=? AND site=?",
            (str(user_id), site),
        ).fetchone()
    return dict(row) if row else None


def save_profile(user_id: str, site: str, **values: Any) -> dict:
    current = profile(user_id, site) or {}
    cookie = values.get("cookie_encrypted", current.get("cookie_encrypted"))
    proxy = values.get("proxy_encrypted", current.get("proxy_encrypted"))
    now = time.time()
    with connect() as connection:
        connection.execute(
            """INSERT INTO site_profiles(user_id,site,cookie_encrypted,proxy_encrypted,updated_at)
               VALUES(?,?,?,?,?) ON CONFLICT(user_id,site) DO UPDATE SET
               cookie_encrypted=excluded.cookie_encrypted,
               proxy_encrypted=excluded.proxy_encrypted,
               updated_at=excluded.updated_at""",
            (str(user_id), site, cookie, proxy, now),
        )
    return profile(user_id, site)  # type: ignore[return-value]


def _job(item: dict) -> dict:
    item["playlist"] = bool(item["playlist"])
    item["subtitles"] = bool(item["subtitles"])
    if item.get("meta_json"):
        try:
            item["meta"] = json.loads(item["meta_json"])
        except json.JSONDecodeError:
            item["meta"] = {}
    item.pop("meta_json", None)
    return item
