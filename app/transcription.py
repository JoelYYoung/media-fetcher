"""Serialized, local Voice Studio transcription for completed media jobs."""

import asyncio
import os
import shutil
from pathlib import Path

import httpx

from app import auth, db
from app.worker import is_within

SEGMENT_SECONDS = 780
MAX_VOICE_UPLOAD = 30 * 1024 * 1024


class TranscriptionError(Exception):
    pass


class TranscriptionRunner:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.active_job: str | None = None
        self.active_process: asyncio.subprocess.Process | None = None
        self.voice_url = os.getenv("VOICE_API_URL", "http://127.0.0.1:8092").rstrip("/")

    async def start(self) -> None:
        self.task = asyncio.create_task(
            self._loop(), name="media-fetcher-transcription-worker"
        )
        for item in db.queued_transcriptions():
            await self.queue.put(item["id"])

    async def stop(self) -> None:
        if self.active_process and self.active_process.returncode is None:
            self.active_process.terminate()
        await self.queue.put(None)
        if self.task:
            await self.task

    async def enqueue(self, job_id: str) -> None:
        await self.queue.put(job_id)

    async def _loop(self) -> None:
        while True:
            job_id = await self.queue.get()
            if job_id is None:
                self.queue.task_done()
                break
            item = db.get_job(job_id)
            if item and item.get("transcript_status") == "queued":
                self.active_job = job_id
                try:
                    await self._run(item)
                except Exception as exc:  # noqa: BLE001 - keep the worker alive
                    latest = db.get_job(job_id)
                    if latest:
                        db.update_job(
                            job_id,
                            transcript_status="failed",
                            transcript_phase="转写失败",
                            transcript_error=friendly_error(exc),
                        )
                finally:
                    self.active_process = None
                    self.active_job = None
            self.queue.task_done()

    async def _run(self, item: dict) -> None:
        source = Path(item.get("output_path") or "")
        job_root = db.DOWNLOAD_ROOT / str(item["user_id"]) / item["id"]
        if not source.is_file() or not is_within(source, job_root):
            raise TranscriptionError("媒体文件已被清理，请重新获取")
        if item.get("playlist") or source.suffix.lower() == ".zip":
            raise TranscriptionError("合集压缩包暂不支持转写，请按单条媒体获取")

        segment_root = job_root / ".transcription-segments"
        shutil.rmtree(segment_root, ignore_errors=True)
        segment_root.mkdir(mode=0o700)
        db.update_job(
            item["id"],
            transcript_status="preparing",
            transcript_phase="正在提取音频",
            transcript_progress=0.02,
            transcript_error=None,
            transcript_text=None,
            transcript_path=None,
            transcript_segments=None,
            transcript_completed_segments=0,
        )
        try:
            segments = await self._make_segments(source, segment_root)
            db.update_job(
                item["id"],
                transcript_status="transcribing",
                transcript_phase="等待本机语音模型",
                transcript_progress=0.08,
                transcript_segments=len(segments),
            )
            text_parts = await self._transcribe_segments(item, segments)
            text = "\n\n".join(part.strip() for part in text_parts if part.strip())
            transcript_path = job_root / "transcript.txt"
            transcript_path.write_text(text, encoding="utf-8")
            db.update_job(
                item["id"],
                transcript_status="completed",
                transcript_phase="转写完成",
                transcript_progress=1.0,
                transcript_text=text,
                transcript_error=None,
                transcript_path=str(transcript_path),
                transcript_completed_segments=len(segments),
            )
        finally:
            shutil.rmtree(segment_root, ignore_errors=True)

    async def _make_segments(self, source: Path, segment_root: Path) -> list[Path]:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise TranscriptionError("本机未安装 ffmpeg")
        output = segment_root / "part-%04d.wav"
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "segment",
            "-segment_time",
            str(SEGMENT_SECONDS),
            "-reset_timestamps",
            "1",
            str(output),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self.active_process = process
        _, stderr = await process.communicate()
        self.active_process = None
        if process.returncode:
            detail = stderr.decode(errors="replace").strip().splitlines()
            raise TranscriptionError(
                "无法从文件提取音频" + (f"：{detail[-1][:240]}" if detail else "")
            )
        segments = sorted(segment_root.glob("part-*.wav"))
        if not segments:
            raise TranscriptionError("文件中没有可转写的音轨")
        if any(path.stat().st_size > MAX_VOICE_UPLOAD for path in segments):
            raise TranscriptionError("音频切片超过本机语音服务的 30 MB 限制")
        return segments

    async def _transcribe_segments(self, item: dict, segments: list[Path]) -> list[str]:
        session_token = auth.new_session(item["user_id"])
        timeout = httpx.Timeout(connect=5, read=120, write=120, pool=5)
        results: list[str] = []
        try:
            async with httpx.AsyncClient(
                base_url=self.voice_url,
                cookies={"voice_session": session_token},
                timeout=timeout,
            ) as client:
                for index, segment in enumerate(segments):
                    voice_task_id = await self._submit_segment(
                        client, segment, item.get("transcript_language") or "auto"
                    )
                    try:
                        results.append(
                            await self._wait_for_segment(
                                client, item["id"], voice_task_id, index, len(segments)
                            )
                        )
                    finally:
                        await self._delete_voice_task(client, voice_task_id)
        finally:
            auth.delete_session(session_token)
        return results

    async def _submit_segment(
        self, client: httpx.AsyncClient, segment: Path, language: str
    ) -> str:
        try:
            with segment.open("rb") as audio:
                response = await client.post(
                    "/api/v1/tasks/transcriptions",
                    data={"lang": language},
                    files={"file": (segment.name, audio, "audio/wav")},
                )
            response.raise_for_status()
            task_id = response.json()["task"]["id"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise TranscriptionError(voice_error(exc)) from exc
        return str(task_id)

    async def _wait_for_segment(
        self,
        client: httpx.AsyncClient,
        job_id: str,
        voice_task_id: str,
        index: int,
        total: int,
    ) -> str:
        while True:
            try:
                response = await client.get(f"/api/v1/tasks/{voice_task_id}")
                response.raise_for_status()
                task = response.json()["task"]
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                raise TranscriptionError(voice_error(exc)) from exc
            status = task.get("status")
            voice_progress = max(0.0, min(1.0, float(task.get("progress") or 0) / 100))
            progress = 0.08 + 0.92 * ((index + voice_progress) / total)
            phase = task.get("phase") or "本机模型转写中"
            db.update_job(
                job_id,
                transcript_phase=(
                    f"第 {index + 1}/{total} 段 · {phase}" if total > 1 else phase
                ),
                transcript_progress=min(progress, 0.999),
                transcript_completed_segments=index,
            )
            if status == "completed":
                return str(task.get("result_text") or "")
            if status == "failed":
                raise TranscriptionError(task.get("error") or "本机语音模型转写失败")
            await asyncio.sleep(1)

    @staticmethod
    async def _delete_voice_task(client: httpx.AsyncClient, voice_task_id: str) -> None:
        try:
            await client.delete(f"/api/v1/tasks/{voice_task_id}")
        except httpx.HTTPError:
            pass


def voice_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            detail = exc.response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        return f"本机语音服务拒绝了请求：{detail or exc.response.status_code}"
    if isinstance(exc, httpx.ConnectError):
        return "无法连接本机 Voice Studio（127.0.0.1:8092）"
    return "本机语音服务返回了无效响应"


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, TranscriptionError):
        return str(exc)[:500]
    if isinstance(exc, httpx.HTTPError):
        return voice_error(exc)
    return f"转写失败：{str(exc)[:450]}"
