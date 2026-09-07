"""Media Fetcher web API and static application."""

import asyncio
import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles

from app import auth, cleanup, db, profiles, secret_store
from app.security import (
    SITES,
    filter_cookie_file,
    site_for_url,
    validate_media_url,
    validate_proxy,
)
from app.transcription import TranscriptionRunner
from app.worker import MODES, Runner, is_within, subtitle_files

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
TTL_HOURS = float(os.getenv("MEDIA_FETCHER_TTL_HOURS", "24"))
runner = Runner()
transcription_runner = TranscriptionRunner()
cleanup_stop = asyncio.Event()
cleanup_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global cleanup_task
    auth.init()
    secret_store.init()
    db.init()
    await runner.start()
    await transcription_runner.start()
    cleanup_stop.clear()
    cleanup_task = asyncio.create_task(cleanup.cleanup_loop(cleanup_stop))
    yield
    cleanup_stop.set()
    if cleanup_task:
        await cleanup_task
    await transcription_runner.stop()
    await runner.stop()


app = FastAPI(
    title="Media Fetcher",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/assets", StaticFiles(directory=PUBLIC), name="assets")


@app.get("/favicon.svg")
async def favicon():
    return FileResponse(PUBLIC / "favicon.svg", media_type="image/svg+xml")


@app.get("/login")
async def login_page(request: Request):
    if auth.current_user(request):
        return RedirectResponse("/")
    return FileResponse(PUBLIC / "login.html")


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = auth.user_by_name(username.strip())
    if not user or user["locked"] or not auth.check_password(password, user["pw_hash"]):
        html = (
            (PUBLIC / "login.html")
            .read_text()
            .replace('class="login-error"', 'class="login-error show"')
        )
        return HTMLResponse(html, status_code=401)
    token = auth.new_session(user["id"])
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.COOKIE,
        token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=auth.SESSION_DAYS * 86400,
    )
    return response


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(auth.COOKIE, "")
    if token:
        auth.delete_session(token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE)
    return response


@app.get("/")
async def index(request: Request):
    if not auth.current_user(request):
        return RedirectResponse("/login")
    return FileResponse(PUBLIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "worker": "busy" if runner.active_job else "idle",
        "transcription_worker": ("busy" if transcription_runner.active_job else "idle"),
        "yt_dlp": tool_version(ROOT / ".venv" / "bin" / "yt-dlp"),
        "ffmpeg": tool_version(Path(shutil.which("ffmpeg") or "")),
        "javascript_runtime": tool_version(
            Path(shutil.which("deno") or shutil.which("node") or "")
        ),
    }


@app.get("/api/bootstrap")
async def bootstrap(request: Request):
    user = auth.require_user(request)
    return {
        "user": auth.public_user(user),
        "modes": [{"id": key, "label": label} for key, label in MODES.items()],
        "jobs": [public_job(item) for item in db.list_jobs(user["id"])],
        "profiles": profiles.public_profiles(user["id"]),
        "limits": {"playlist_items": 50, "ttl_hours": TTL_HOURS},
    }


@app.get("/api/jobs")
async def jobs(request: Request):
    user = auth.require_user(request)
    return [public_job(item) for item in db.list_jobs(user["id"])]


@app.post("/api/jobs", status_code=202)
async def create_job(request: Request):
    user = auth.require_user(request)
    body = await request.json()
    try:
        url = validate_media_url(body.get("url") or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    mode = body.get("mode") or "audio_original"
    if mode not in MODES:
        raise HTTPException(400, "不支持的输出格式")
    if body.get("rights_confirmed") is not True:
        raise HTTPException(400, "请确认你有权保存此内容")
    playlist = body.get("playlist") is True
    subtitles = body.get("subtitles") is True and mode.startswith("video_")
    host = (urlsplit(url).hostname or "").lower()
    item = db.create_job(
        user["id"], url, host, site_for_url(url), mode, playlist, subtitles, TTL_HOURS
    )
    await runner.enqueue(item["id"])
    return public_job(item)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    user = auth.require_user(request)
    item = db.get_job(job_id, user["id"])
    if not item:
        raise HTTPException(404, "任务不存在")
    if not await runner.cancel(job_id):
        raise HTTPException(409, "任务已经结束")
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str, request: Request):
    user = auth.require_user(request)
    item = db.get_job(job_id, user["id"])
    if not item:
        raise HTTPException(404, "任务不存在")
    if item.get("transcript_status") in db.ACTIVE_TRANSCRIPT_STATUSES:
        raise HTTPException(409, "语音转写正在进行，完成后才能删除")
    if item["status"] not in db.TERMINAL_STATUSES:
        await runner.cancel(job_id)
    cleanup.remove_job_files(item)
    db.delete_job(job_id, user["id"])
    return {"ok": True}


@app.post("/api/jobs/{job_id}/transcribe", status_code=202)
async def transcribe_job(job_id: str, request: Request):
    user = auth.require_user(request)
    item = db.get_job(job_id, user["id"])
    if not item:
        raise HTTPException(404, "任务不存在")
    if item["status"] != "completed" or not item.get("output_path"):
        raise HTTPException(409, "文件尚未获取完成")
    if item.get("playlist") or Path(item["output_path"]).suffix.lower() == ".zip":
        raise HTTPException(400, "合集压缩包暂不支持转写，请按单条媒体获取")
    if item.get("transcript_status") in db.ACTIVE_TRANSCRIPT_STATUSES:
        raise HTTPException(409, "这个文件正在转写")
    body = await request.json()
    language = body.get("language") or "auto"
    if language not in {"auto", "zh", "en"}:
        raise HTTPException(400, "不支持的转写语言")
    transcript_path = item.get("transcript_path")
    if transcript_path:
        path = Path(transcript_path)
        if is_within(path, db.DOWNLOAD_ROOT / str(user["id"]) / job_id):
            path.unlink(missing_ok=True)
    updated = db.update_job(
        job_id,
        transcript_status="queued",
        transcript_phase="等待转写",
        transcript_progress=0.0,
        transcript_language=language,
        transcript_text=None,
        transcript_error=None,
        transcript_path=None,
        transcript_segments=None,
        transcript_completed_segments=0,
    )
    await transcription_runner.enqueue(job_id)
    return public_job(updated)  # type: ignore[arg-type]


@app.get("/api/jobs/{job_id}/transcript")
async def transcript(job_id: str, request: Request):
    user = auth.require_user(request)
    item = db.get_job(job_id, user["id"])
    if not item:
        raise HTTPException(404, "任务不存在")
    if item.get("transcript_status") != "completed":
        raise HTTPException(409, "转写尚未完成")
    return {
        "text": item.get("transcript_text") or "",
        "language": item.get("transcript_language") or "auto",
        "segments": item.get("transcript_segments") or 1,
    }


@app.get("/api/jobs/{job_id}/transcript/download")
async def download_transcript(job_id: str, request: Request):
    user = auth.require_user(request)
    item = db.get_job(job_id, user["id"])
    if not item or item.get("transcript_status") != "completed":
        raise HTTPException(404, "转写文本不存在或尚未完成")
    name = f"{Path(item.get('output_name') or 'transcript').stem}.txt"
    return PlainTextResponse(
        item.get("transcript_text") or "",
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": (
                'attachment; filename="transcript.txt"; '
                f"filename*=UTF-8''{quote(name, safe='')}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/jobs/{job_id}/download")
async def download(job_id: str, request: Request):
    user = auth.require_user(request)
    item = db.get_job(job_id, user["id"])
    if not item or item["status"] != "completed" or not item.get("output_path"):
        raise HTTPException(404, "文件不存在或尚未完成")
    path = Path(item["output_path"])
    job_root = db.DOWNLOAD_ROOT / str(user["id"]) / job_id
    if not path.is_file() or not is_within(path, job_root):
        raise HTTPException(404, "文件已被清理")
    return FileResponse(
        path,
        filename=item.get("output_name") or path.name,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/jobs/{job_id}/subtitles/{subtitle_id}")
async def download_subtitle(job_id: str, subtitle_id: int, request: Request):
    user = auth.require_user(request)
    item = db.get_job(job_id, user["id"])
    if not item or item["status"] != "completed" or not item.get("subtitles"):
        raise HTTPException(404, "字幕不存在或尚未完成")
    files = job_subtitle_files(item)
    if subtitle_id < 0 or subtitle_id >= len(files):
        raise HTTPException(404, "字幕文件不存在")
    path = files[subtitle_id]
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/x-subrip",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/profiles")
async def list_profiles(request: Request):
    user = auth.require_user(request)
    return profiles.public_profiles(user["id"])


@app.post("/api/profiles/{site}/cookies")
async def upload_cookies(
    site: str, request: Request, file: Annotated[UploadFile, File()]
):
    user = auth.require_user(request)
    if site not in SITES:
        raise HTTPException(404, "不支持的登录站点")
    raw = await file.read(1024 * 1024 + 1)
    try:
        filtered = filter_cookie_file(raw, site)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    profiles.save_cookies(user["id"], site, filtered)
    return profiles.public_profiles(user["id"])


@app.delete("/api/profiles/{site}/cookies")
async def delete_cookies(site: str, request: Request):
    user = auth.require_user(request)
    if site not in SITES:
        raise HTTPException(404, "不支持的登录站点")
    profiles.remove_cookies(user["id"], site)
    return profiles.public_profiles(user["id"])


@app.put("/api/profiles/{site}/proxy")
async def update_proxy(site: str, request: Request):
    user = auth.require_user(request)
    if site not in SITES:
        raise HTTPException(404, "不支持的登录站点")
    body = await request.json()
    try:
        proxy = validate_proxy(body.get("proxy") or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    profiles.save_proxy(user["id"], site, proxy)
    return profiles.public_profiles(user["id"])


def public_job(item: dict) -> dict:
    fields = (
        "id",
        "source_host",
        "site",
        "mode",
        "playlist",
        "subtitles",
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
        "output_name",
        "output_size",
        "created_at",
        "updated_at",
        "expires_at",
        "transcript_status",
        "transcript_phase",
        "transcript_progress",
        "transcript_language",
        "transcript_error",
        "transcript_segments",
        "transcript_completed_segments",
    )
    result = {key: item.get(key) for key in fields}
    result["subtitle_files"] = [
        {
            "id": index,
            "name": path.name,
            "label": subtitle_label(path),
            "format": path.suffix.removeprefix(".").upper(),
        }
        for index, path in enumerate(job_subtitle_files(item))
    ]
    return result


def job_subtitle_files(item: dict) -> list[Path]:
    if item.get("status") != "completed" or not item.get("subtitles"):
        return []
    job_root = db.DOWNLOAD_ROOT / str(item["user_id"]) / item["id"]
    return subtitle_files(job_root) if job_root.is_dir() else []


def subtitle_label(path: Path) -> str:
    tokens = path.name.lower().split(".")
    language = next(
        (token for token in reversed(tokens) if token.startswith(("zh", "en"))), ""
    )
    if language.startswith("zh-hans") or language in {"zh-cn", "zh-sg"}:
        return "中文字幕（简体）"
    if language.startswith("zh-hant") or language in {"zh-tw", "zh-hk"}:
        return "中文字幕（繁体）"
    if language.startswith("zh"):
        return "中文字幕"
    if language.startswith("en"):
        return "English 字幕"
    return "字幕"


def tool_version(path: Path) -> str | None:
    if not path or not path.exists():
        return None
    try:
        version_flag = "-version" if path.name in {"ffmpeg", "ffprobe"} else "--version"
        result = subprocess.run(
            [str(path), version_flag],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return result.stdout.splitlines()[0][:100] if result.returncode == 0 else None
    except (IndexError, OSError, subprocess.SubprocessError):
        return None
