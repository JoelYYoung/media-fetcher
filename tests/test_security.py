from unittest.mock import patch

import pytest

from app.security import (
    filter_cookie_file,
    proxy_label,
    site_for_url,
    validate_media_url,
    validate_proxy,
)


def test_validate_media_url_accepts_public_https():
    answers = [(2, 1, 6, "", ("142.250.70.14", 443))]
    with patch("app.security.socket.getaddrinfo", return_value=answers):
        assert validate_media_url("https://www.youtube.com/watch?v=x").startswith(
            "https://"
        )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1:8090/private",
        "http://[::1]/private",
        "https://user@example.com/video",
    ],
)
def test_validate_media_url_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        validate_media_url(url, resolve=False)


def test_validate_media_url_rejects_private_dns_answer():
    answers = [(2, 1, 6, "", ("192.168.1.2", 443))]
    with (
        patch("app.security.socket.getaddrinfo", return_value=answers),
        pytest.raises(ValueError, match="内网"),
    ):
        validate_media_url("https://example.com/media")


def test_site_detection():
    assert site_for_url("https://youtu.be/abc") == "youtube"
    assert site_for_url("https://www.bilibili.com/video/BV1") == "bilibili"
    assert site_for_url("https://example.com/video") is None


def test_cookie_filter_only_keeps_selected_domain():
    raw = (
        b"# Netscape HTTP Cookie File\n"
        b".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n"
        b".google.com\tTRUE\t/\tTRUE\t0\tOTHER\tleak\n"
        b"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN\tok\n"
    )
    result = filter_cookie_file(raw, "youtube")
    assert "SID\tsecret" in result
    assert "LOGIN\tok" in result
    assert "OTHER" not in result
    assert "google.com" not in result


def test_cookie_filter_rejects_wrong_site():
    raw = b"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tSID\tx\n"
    with pytest.raises(ValueError, match="没有找到"):
        filter_cookie_file(raw, "bilibili")


def test_proxy_validation_and_redacted_label():
    value = validate_proxy("socks5://alice:secret@127.0.0.1:1080")
    assert value.endswith(":1080")
    assert proxy_label(value) == "socks5://127.0.0.1:1080"
    with pytest.raises(ValueError):
        validate_proxy("ftp://example.com:21")
