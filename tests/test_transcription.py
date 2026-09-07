import asyncio
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app import db
from app.transcription import MAX_VOICE_UPLOAD, TranscriptionRunner


def test_database_migrates_existing_jobs_table(tmp_path: Path, monkeypatch):
    database = tmp_path / "media-fetcher.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL
            )"""
        )
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DOWNLOAD_ROOT", tmp_path / "downloads")

    db.init()

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    assert set(db.TRANSCRIPT_COLUMNS) <= columns


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_audio_is_normalized_into_voice_sized_wav_segments(tmp_path: Path):
    source = tmp_path / "source.m4a"
    segments = tmp_path / "segments"
    segments.mkdir()
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )

    result = asyncio.run(TranscriptionRunner()._make_segments(source, segments))

    assert [path.name for path in result] == ["part-0000.wav"]
    assert 0 < result[0].stat().st_size < MAX_VOICE_UPLOAD
