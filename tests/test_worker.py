from pathlib import Path

import pytest

from app.worker import (
    metadata_summary,
    mode_args,
    package_outputs,
    subtitle_args,
    subtitle_files,
)


def test_modes_are_fixed_argument_arrays():
    assert "ba/b" in mode_args("audio_original")
    assert "mp3" in mode_args("audio_mp3")
    assert "res:720" in mode_args("video_720p")
    assert mode_args("video_best") == []
    with pytest.raises(ValueError):
        mode_args("--exec=bad")


def test_metadata_summary_limits_public_fields():
    summary = metadata_summary(
        {
            "id": "abc",
            "title": "A title",
            "duration": 12,
            "extractor_key": "Youtube",
            "uploader": "Owner",
            "formats": [{"url": "secret"}],
        }
    )
    assert summary["title"] == "A title"
    assert summary["item_count"] == 1
    assert "formats" not in summary["meta_json"]


def test_package_single_output(tmp_path: Path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"media")
    assert package_outputs(tmp_path, [media], "video") == media.resolve()


def test_package_multiple_outputs(tmp_path: Path):
    first = tmp_path / "one.mp4"
    second = tmp_path / "two.vtt"
    first.write_bytes(b"video")
    second.write_text("subtitle")
    output = package_outputs(tmp_path, [first, second], "bundle")
    assert output.name == "download.zip"
    assert output.is_file()


def test_subtitles_are_kept_as_srt_sidecars():
    args = subtitle_args()
    assert args[args.index("--convert-subs") + 1] == "srt"
    assert "--embed-subs" in args
    assert "--keep-subs" in args


def test_subtitle_sidecars_are_discovered_and_can_be_bundled(tmp_path: Path):
    media = tmp_path / "video.mp4"
    chinese = tmp_path / "video.zh-Hans.srt"
    english = tmp_path / "video.en.srt"
    media.write_bytes(b"video")
    chinese.write_text("Chinese", encoding="utf-8")
    english.write_text("English", encoding="utf-8")

    assert subtitle_files(tmp_path) == [english.resolve(), chinese.resolve()]
    output = package_outputs(tmp_path, [media], "video", include_sidecars=True)
    assert output.name == "download.zip"
    assert output.is_file()
