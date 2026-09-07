from pathlib import Path

from app.main import subtitle_label


def test_subtitle_labels_include_language_variant():
    assert subtitle_label(Path("video.zh-Hans.srt")) == "中文字幕（简体）"
    assert subtitle_label(Path("video.zh-Hant.srt")) == "中文字幕（繁体）"
    assert subtitle_label(Path("video.en-orig.srt")) == "English 字幕"
