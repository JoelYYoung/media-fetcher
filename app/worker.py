"""Single-concurrency yt-dlp worker with progress, limits, and cancellation."""

import asyncio
import json
import os
import signal
import sys
import zipfile
from pathlib import Path
from typing import Any

from app import db, profiles
from app.security import redact_message

MODES = {
    "audio_original": "原始最佳音频",
    "audio_mp3": "MP3 高质量",
    "video_720p": "MP4 最高 720p",
    "video_1080p": "MP4 最高 1080p",
    "video_best": "最佳质量视频",
}
IGNORE_SUFFIXES = {".part", ".ytdl", ".temp", ".tmp"}
SUBTITLE_SUFFIXES = {".ass", ".lrc", ".srt", ".ssa", ".ttml", ".vtt"}


class JobCancelled(Exception):
    pass


class JobLimitExceeded(Exception):
    pass


class Runner:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.active_job: str | None = None
        self.active_process: asyncio.subprocess.Process | None = None
        self.max_file_bytes = int(
            float(os.getenv("MEDIA_FETCHER_MAX_FILE_GB", "10")) * 1024**3
        )
        self.max_duration = (
            float(os.getenv("MEDIA_FETCHER_MAX_DURATION_HOURS", "6")) * 3600
        )

    async def start(self) -> None:
        db.mark_interrupted()
        self.task = asyncio.create_task(self._loop(), name="media-fetcher-worker")
        for item in db.queued_jobs():
            await self.queue.put(item["id"])

    async def stop(self) -> None:
        if self.active_process and self.active_process.returncode is None:
            self._terminate(self.active_process)
        await self.queue.put(None)
        if self.task:
            await self.task

    async def enqueue(self, job_id: str) -> None:
        await self.queue.put(job_id)

    async def cancel(self, job_id: str) -> bool:
        item = db.get_job(job_id)
        if not item or item["status"] in db.TERMINAL_STATUSES:
            return False
        db.update_job(job_id, status="cancelled", error="任务已取消")
        if self.active_job == job_id and self.active_process:
            self._terminate(self.active_process)
        return True

    async def _loop(self) -> None:
        while True:
            job_id = await self.queue.get()
            if job_id is None:
                self.queue.task_done()
                break
            item = db.get_job(job_id)
            if item and item["status"] == "queued":
                self.active_job = job_id
                try:
                    await self._run_job(item)
                except JobCancelled:
                    db.update_job(job_id, status="cancelled", error="任务已取消")
                except JobLimitExceeded as exc:
                    db.update_job(job_id, status="failed", error=str(exc))
                except Exception as exc:  # noqa: BLE001 - worker boundary must survive one bad job
                    latest = db.get_job(job_id)
                    if latest and latest["status"] != "cancelled":
                        db.update_job(
                            job_id,
                            status="failed",
                            error=redact_message(str(exc), item["source_url"]),
                        )
                finally:
                    self.active_process = None
                    self.active_job = None
            self.queue.task_done()

    async def _run_job(self, item: dict) -> None:
        job_id = item["id"]
        job_dir = db.DOWNLOAD_ROOT / item["user_id"] / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(job_dir, 0o700)
        profile = profiles.runtime_profile(item["user_id"], item.get("site"))
        cookie_path: Path | None = None
        try:
            db.update_job(job_id, status="probing", progress=0.0, error=None)
            try:
                metadata = await self._probe(item, None, profile["proxy"])
            except RuntimeError:
                if not profile["cookies"]:
                    raise
                cookie_path = job_dir / ".cookies.txt"
                descriptor = os.open(
                    cookie_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "w") as output:
                    output.write(profile["cookies"])
                metadata = await self._probe(item, cookie_path, profile["proxy"])
            self._validate_metadata(metadata, item["playlist"])
            summary = metadata_summary(metadata)
            db.update_job(job_id, **summary, status="downloading", progress=0.0)
            output_files = await self._download(
                item, job_dir, cookie_path, profile["proxy"]
            )
            if db.get_job(job_id)["status"] == "cancelled":  # type: ignore[index]
                raise JobCancelled()
            db.update_job(job_id, status="processing", progress=1.0)
            if cookie_path:
                cookie_path.unlink(missing_ok=True)
                cookie_path = None
            output_path = package_outputs(
                job_dir,
                output_files,
                summary.get("title") or "media",
                include_sidecars=item["playlist"] and item["subtitles"],
            )
            db.update_job(
                job_id,
                status="completed",
                progress=1.0,
                output_path=str(output_path),
                output_name=output_path.name,
                output_size=output_path.stat().st_size,
                error=None,
                eta=0,
            )
        finally:
            if cookie_path:
                cookie_path.unlink(missing_ok=True)

    async def _probe(self, item: dict, cookie_path: Path | None, proxy: str) -> dict:
        args = self._base_args(item, cookie_path, proxy)
        args += ["--dump-single-json", "--skip-download"]
        if item["playlist"]:
            args += ["--flat-playlist"]
        args += ["--", item["source_url"]]
        code, stdout, stderr = await self._capture(args, timeout=90)
        if code != 0:
            raise RuntimeError(self._friendly_error(stderr or stdout, item, proxy))
        lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        if not lines:
            raise RuntimeError("站点没有返回可下载的媒体信息")
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError("无法解析站点返回的媒体信息") from exc

    async def _download(
        self, item: dict, job_dir: Path, cookie_path: Path | None, proxy: str
    ) -> list[Path]:
        args = self._base_args(item, cookie_path, proxy)
        args += [
            "--newline",
            "--progress",
            "--progress-delta",
            "1",
            "--progress-template",
            "download:MF_PROGRESS:%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s",
            "--print",
            "after_move:MF_FILE:%(filepath)j",
            "--paths",
            str(job_dir),
            "--output",
            "%(title).120B [%(id)s].%(ext)s",
            "--max-filesize",
            str(self.max_file_bytes),
        ]
        args += mode_args(item["mode"])
        if item["subtitles"]:
            args += subtitle_args()
        args += ["--", item["source_url"]]

        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.active_process = process
        stderr_task = asyncio.create_task(process.stderr.read())  # type: ignore[union-attr]
        files: list[Path] = []
        assert process.stdout is not None
        async for raw in process.stdout:
            line = raw.decode(errors="replace").strip()
            if line.startswith("MF_PROGRESS:"):
                values = line[len("MF_PROGRESS:") :].split("|")
                downloaded = number(values, 0)
                total = number(values, 1) or number(values, 2)
                speed = number(values, 3)
                eta = number(values, 4)
                if downloaded and downloaded > self.max_file_bytes:
                    self._terminate(process)
                    await process.wait()
                    await stderr_task
                    raise JobLimitExceeded("文件超过服务允许的最大尺寸")
                progress = (
                    min(downloaded / total, 0.999) if downloaded and total else 0.0
                )
                db.update_job(
                    item["id"],
                    progress=progress,
                    downloaded_bytes=int(downloaded or 0),
                    total_bytes=int(total) if total else None,
                    speed=speed,
                    eta=eta,
                )
            elif line.startswith("MF_FILE:"):
                try:
                    candidate = Path(json.loads(line[len("MF_FILE:") :]))
                    if is_within(candidate, job_dir):
                        files.append(candidate)
                except (json.JSONDecodeError, TypeError):
                    pass
        code = await process.wait()
        stderr = (await stderr_task).decode(errors="replace")
        latest = db.get_job(item["id"])
        if latest and latest["status"] == "cancelled":
            raise JobCancelled()
        if code != 0:
            raise RuntimeError(self._friendly_error(stderr, item, proxy))
        return files

    def _base_args(self, item: dict, cookie_path: Path | None, proxy: str) -> list[str]:
        args = [
            str(Path(sys.executable).parent / "yt-dlp"),
            "--ignore-config",
            "--no-color",
            "--no-warnings",
            "--socket-timeout",
            "20",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--sleep-requests",
            "1",
            "--sleep-interval",
            "2",
            "--max-sleep-interval",
            "5",
            "--ies",
            "default" if item["source_host"] == "b23.tv" else "default,-generic",
        ]
        args += (
            ["--yes-playlist", "--playlist-end", "50"]
            if item["playlist"]
            else ["--no-playlist"]
        )
        if cookie_path:
            args += ["--cookies", str(cookie_path)]
        if proxy:
            args += ["--proxy", proxy]
        return args

    async def _capture(self, args: list[str], timeout: float) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.active_process = process
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except TimeoutError as exc:
            self._terminate(process)
            await process.wait()
            raise RuntimeError("站点响应超时") from exc
        latest = db.get_job(self.active_job) if self.active_job else None
        if latest and latest["status"] == "cancelled":
            raise JobCancelled()
        return (
            process.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    def _validate_metadata(self, metadata: dict, playlist: bool) -> None:
        if metadata.get("_type") == "playlist" or playlist:
            entries = [entry for entry in (metadata.get("entries") or []) if entry]
            if len(entries) > 50:
                raise JobLimitExceeded("播放列表超过 50 项")
            durations = [
                float(entry["duration"]) for entry in entries if entry.get("duration")
            ]
            if durations and any(value > self.max_duration for value in durations):
                raise JobLimitExceeded("播放列表中有媒体超过时长限制")
            return
        duration = metadata.get("duration")
        if duration and float(duration) > self.max_duration:
            raise JobLimitExceeded("媒体超过服务允许的最大时长")

    def _friendly_error(self, message: str, item: dict, proxy: str) -> str:
        lower = message.lower()
        if "sign in" in lower or "login" in lower or "cookies" in lower:
            prefix = "站点要求登录，请在登录配置中导入有效 Cookie。"
        elif "unsupported url" in lower:
            prefix = "该链接没有可用的明确站点提取器。"
        elif "private video" in lower or "members-only" in lower:
            prefix = "当前账号没有访问此媒体的权限。"
        elif "video unavailable" in lower or "not available" in lower:
            prefix = "媒体不存在、不可用或受地区限制。"
        else:
            prefix = "媒体获取失败。"
        detail = redact_message(message, item["source_url"], proxy)
        return f"{prefix} {detail}".strip()

    @staticmethod
    def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def mode_args(mode: str) -> list[str]:
    if mode == "audio_original":
        return ["--format", "ba/b", "--extract-audio", "--audio-format", "best"]
    if mode == "audio_mp3":
        return [
            "--format",
            "ba/b",
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "0",
        ]
    if mode == "video_720p":
        return ["--preset-alias", "mp4", "--format-sort", "res:720"]
    if mode == "video_1080p":
        return ["--preset-alias", "mp4", "--format-sort", "res:1080"]
    if mode == "video_best":
        return []
    raise ValueError("不支持的输出格式")


def subtitle_args() -> list[str]:
    return [
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        "zh.*,en.*",
        "--convert-subs",
        "srt",
        "--embed-subs",
        "--keep-subs",
    ]


def metadata_summary(metadata: dict) -> dict[str, Any]:
    entries = [entry for entry in (metadata.get("entries") or []) if entry]
    duration = metadata.get("duration")
    if not duration and entries:
        known = [float(entry["duration"]) for entry in entries if entry.get("duration")]
        duration = sum(known) if known else None
    return {
        "title": (
            metadata.get("title") or metadata.get("playlist_title") or "未命名媒体"
        )[:500],
        "extractor": (metadata.get("extractor_key") or metadata.get("extractor") or "")[
            :100
        ],
        "duration": duration,
        "item_count": len(entries) if entries else 1,
        "thumbnail": metadata.get("thumbnail") if not entries else None,
        "meta_json": {
            "id": metadata.get("id"),
            "uploader": metadata.get("uploader") or metadata.get("channel"),
            "webpage_url": metadata.get("webpage_url"),
        },
    }


def number(values: list[str], index: int) -> float | None:
    try:
        value = values[index].strip()
        return None if value in {"", "NA", "None"} else float(value)
    except (IndexError, ValueError):
        return None


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def subtitle_files(job_dir: Path) -> list[Path]:
    return sorted(
        (
            path.resolve()
            for path in job_dir.rglob("*")
            if path.is_file()
            and path.suffix.lower() in SUBTITLE_SUFFIXES
            and is_within(path, job_dir)
        ),
        key=lambda path: path.name.casefold(),
    )


def package_outputs(
    job_dir: Path,
    reported: list[Path],
    title: str,
    *,
    include_sidecars: bool = False,
) -> Path:
    existing = [
        path
        for path in reported
        if path.exists()
        and path.is_file()
        and path.suffix not in IGNORE_SUFFIXES
        and is_within(path, job_dir)
    ]
    if not existing:
        existing = [
            path
            for path in job_dir.rglob("*")
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix not in IGNORE_SUFFIXES
            and path.name != "download.zip"
        ]
    if include_sidecars:
        existing.extend(subtitle_files(job_dir))
    unique = list(dict.fromkeys(path.resolve() for path in existing))
    if not unique:
        raise RuntimeError("下载完成但没有找到输出文件")
    if len(unique) == 1:
        return unique[0]
    archive = job_dir / "download.zip"
    with zipfile.ZipFile(
        archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as output:
        for path in unique:
            output.write(path, arcname=path.relative_to(job_dir.resolve()))
    return archive
