"""Expiry and disk-quota cleanup for generated media."""

import asyncio
import os
import shutil
import time

from app import db


def remove_job_files(item: dict) -> None:
    path = db.DOWNLOAD_ROOT / str(item["user_id"]) / item["id"]
    try:
        resolved = path.resolve()
        resolved.relative_to(db.DOWNLOAD_ROOT.resolve())
    except ValueError:
        return
    if resolved.exists():
        shutil.rmtree(resolved)


def cleanup_once() -> dict:
    now = time.time()
    quota = int(float(os.getenv("MEDIA_FETCHER_QUOTA_GB", "50")) * 1024**3)
    removed = 0
    with db.connect() as connection:
        expired = [
            dict(row)
            for row in connection.execute(
                """SELECT * FROM jobs
                   WHERE expires_at<=?
                     AND status IN ('completed','failed','cancelled')
                     AND COALESCE(transcript_status, '') NOT IN ('queued','preparing','transcribing')
                   ORDER BY created_at""",
                (now,),
            ).fetchall()
        ]
    for item in expired:
        remove_job_files(item)
        with db.connect() as connection:
            connection.execute("DELETE FROM jobs WHERE id=?", (item["id"],))
        removed += 1

    completed = db_path_sizes()
    total = sum(size for _, size in completed)
    for item, size in completed:
        if total <= quota:
            break
        remove_job_files(item)
        with db.connect() as connection:
            connection.execute("DELETE FROM jobs WHERE id=?", (item["id"],))
        total -= size
        removed += 1
    return {"removed": removed, "bytes": total}


def db_path_sizes() -> list[tuple[dict, int]]:
    with db.connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """SELECT * FROM jobs
                   WHERE status='completed'
                     AND COALESCE(transcript_status, '') NOT IN ('queued','preparing','transcribing')
                   ORDER BY created_at"""
            ).fetchall()
        ]
    result = []
    for item in rows:
        path = db.DOWNLOAD_ROOT / str(item["user_id"]) / item["id"]
        size = (
            sum(child.stat().st_size for child in path.rglob("*") if child.is_file())
            if path.exists()
            else 0
        )
        result.append((item, size))
    return result


async def cleanup_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        await asyncio.to_thread(cleanup_once)
        try:
            await asyncio.wait_for(stop.wait(), timeout=3600)
        except TimeoutError:
            pass
