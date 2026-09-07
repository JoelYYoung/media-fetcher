"""Encrypt site cookies and proxy credentials with a macOS Keychain-held key."""

import os
import subprocess

from cryptography.fernet import Fernet, InvalidToken

SERVICE = "media-fetcher.cookie-key"
ACCOUNT = os.getenv(
    "MEDIA_FETCHER_KEYCHAIN_ACCOUNT", os.getenv("USER", "media-fetcher")
)
_cached_key: bytes | None = None


def _security(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/security", *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def init() -> None:
    global _cached_key
    configured = os.getenv("MEDIA_FETCHER_FERNET_KEY", "").strip()
    if configured:
        Fernet(configured.encode())
        _cached_key = configured.encode()
        return
    found = _security("find-generic-password", "-a", ACCOUNT, "-s", SERVICE, "-w")
    if found.returncode == 0 and found.stdout.strip():
        key = found.stdout.strip().encode()
        Fernet(key)
        _cached_key = key
        return
    key = Fernet.generate_key()
    created = _security(
        "add-generic-password",
        "-U",
        "-a",
        ACCOUNT,
        "-s",
        SERVICE,
        "-w",
        key.decode(),
    )
    if created.returncode != 0:
        raise RuntimeError("无法在 macOS Keychain 中创建 Media Fetcher 加密密钥")
    _cached_key = key


def _fernet() -> Fernet:
    if _cached_key is None:
        init()
    assert _cached_key is not None
    return Fernet(_cached_key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("已保存的登录配置无法解密") from exc
