"""Input validation and secret filtering for untrusted media URLs."""

import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

SITES = {
    "youtube": {
        "label": "YouTube",
        "domains": ("youtube.com", "youtu.be", "googlevideo.com"),
        "cookie_domains": ("youtube.com",),
    },
    "bilibili": {
        "label": "Bilibili",
        "domains": ("bilibili.com", "b23.tv", "bilibili.tv"),
        "cookie_domains": ("bilibili.com", "bilibili.tv"),
    },
}
PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
MAX_COOKIE_BYTES = 1024 * 1024


def _public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_media_url(value: str, *, resolve: bool = True) -> str:
    value = (value or "").strip()
    if len(value) > 4096:
        raise ValueError("链接过长")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("请输入有效的 HTTP 或 HTTPS 链接")
    if parsed.username or parsed.password:
        raise ValueError("媒体链接不能包含用户名或密码")
    try:
        if not _public_ip(parsed.hostname):
            raise ValueError("不能访问本机或内网地址")
    except ValueError as exc:
        if "内网" in str(exc):
            raise
    if resolve:
        try:
            addresses = {
                item[4][0] for item in socket.getaddrinfo(parsed.hostname, 443)
            }
        except socket.gaierror as exc:
            raise ValueError("无法解析链接中的域名") from exc
        if not addresses or any(not _public_ip(address) for address in addresses):
            raise ValueError("链接解析到了本机或内网地址")
    return value


def site_for_url(value: str) -> str | None:
    host = (urlsplit(value).hostname or "").lower().rstrip(".")
    for site, config in SITES.items():
        if any(
            host == domain or host.endswith("." + domain)
            for domain in config["domains"]
        ):
            return site
    return None


def validate_proxy(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) > 2048:
        raise ValueError("代理地址过长")
    parsed = urlsplit(value)
    if parsed.scheme not in PROXY_SCHEMES or not parsed.hostname:
        raise ValueError("代理仅支持 HTTP、HTTPS、SOCKS5 或 SOCKS5H")
    if parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含查询参数或片段")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口无效") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("代理端口无效")
    return value


def proxy_label(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{parsed.port}" if parsed.port else "")
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def filter_cookie_file(raw: bytes, site: str) -> str:
    if site not in SITES:
        raise ValueError("不支持的登录站点")
    if not raw or len(raw) > MAX_COOKIE_BYTES:
        raise ValueError("Cookie 文件为空或超过 1 MiB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Cookie 文件必须是 UTF-8 文本") from exc
    allowed = SITES[site]["cookie_domains"]
    rows: list[str] = ["# Netscape HTTP Cookie File"]
    kept = 0
    for original in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        http_only = original.startswith("#HttpOnly_")
        if not original or (original.startswith("#") and not http_only):
            continue
        row = original[len("#HttpOnly_") :] if http_only else original
        parts = row.split("\t")
        if len(parts) != 7:
            continue
        domain = parts[0].lstrip(".").lower().rstrip(".")
        if not any(domain == item or domain.endswith("." + item) for item in allowed):
            continue
        parts[0] = ("." if row.startswith(".") else "") + domain
        clean = "\t".join(parts)
        rows.append(("#HttpOnly_" if http_only else "") + clean)
        kept += 1
    if not kept:
        raise ValueError(f"文件中没有找到 {SITES[site]['label']} Cookie")
    return "\n".join(rows) + "\n"


def redact_message(value: str, source_url: str = "", proxy: str = "") -> str:
    value = value or ""
    for secret in (source_url, proxy):
        if secret:
            value = value.replace(secret, "[redacted-url]")
    value = re.sub(
        r"https?://[^\s'\"]+", lambda match: _redact_url(match.group(0)), value
    )
    return value[-4000:].strip()


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value.rstrip(".,);]"))
        host = parsed.hostname or "remote"
        return f"{parsed.scheme}://{host}/…"
    except ValueError:
        return "[redacted-url]"
