"""Authentication shared with the existing VibeTypst account database."""

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path

from fastapi import HTTPException, Request

ROOT = Path(__file__).resolve().parents[1]
USER_DB = Path(os.getenv("VIBETYPST_DB", str(ROOT / "data" / "accounts.db")))
COOKIE = "media_fetch_session"
SESSION_DAYS = 30


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def user_db() -> sqlite3.Connection:
    return _connect(USER_DB)


def init() -> None:
    if not USER_DB.exists():
        raise RuntimeError("Account database not found; configure VIBETYPST_DB in .env")
    with user_db() as connection:
        if not connection.execute(
            "SELECT 1 FROM users WHERE locked=0 LIMIT 1"
        ).fetchone():
            raise RuntimeError("No active users are available")
        connection.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))


def check_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), 260_000
        ).hex()
        return hmac.compare_digest(actual, digest)
    except (AttributeError, TypeError, ValueError):
        return False


def user_by_name(username: str) -> dict | None:
    with user_db() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
    return dict(row) if row else None


def user_by_id(user_id: str) -> dict | None:
    with user_db() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def new_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    with user_db() as connection:
        connection.execute(
            "INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",
            (token, user_id, time.time() + SESSION_DAYS * 86400),
        )
    return token


def delete_session(token: str) -> None:
    with user_db() as connection:
        connection.execute("DELETE FROM sessions WHERE token=?", (token,))


def user_for_session(token: str) -> dict | None:
    if not token:
        return None
    with user_db() as connection:
        row = connection.execute(
            "SELECT user_id FROM sessions WHERE token=? AND expires_at>?",
            (token, time.time()),
        ).fetchone()
    user = user_by_id(row["user_id"]) if row else None
    return None if user and user["locked"] else user


def current_user(request: Request) -> dict | None:
    return user_for_session(request.cookies.get(COOKIE, ""))


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "authentication required")
    return user


def public_user(user: dict) -> dict:
    return {"id": user["id"], "username": user["username"], "role": user["role"]}
