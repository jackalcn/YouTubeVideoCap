from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import needs_ffmpeg
from videocap.library import describe_probe, get_media_duration, probe_media
from videocap.media_tools import ToolRequest, build_ffmpeg_command, reserve_output_path, run_tool


class Progress:
    def __init__(self) -> None:
        self.values: list[float] = []
        self.texts: list[str] = []

    def progress(self, value: float, text: str = "") -> None:
        self.values.append(value)
        self.texts.append(text)


@pytest.fixture
def workspace_video(sample_video: Path, tmp_path: Path) -> Path:
    path = tmp_path / sample_video.name
    shutil.copy(sample_video, path)
    return path


@needs_ffmpeg
def test_trim_copies_a_clip(workspace_video: Path) -> None:
    result = run_tool(ToolRequest("trim", str(workspace_video), {"start": 1.0, "end": 3.0}), Progress())
    assert result.error == ""
    assert result.output_path and result.output_path.exists()
    duration = get_media_duration(result.output_path)
    # Stream copy can only cut at keyframes, so the length is approximate.
    assert duration is not None and 1.0 < duration < 4.5


@needs_ffmpeg
def test_trim_keeps_video_when_the_clip_holds_no_keyframe(tmp_path: Path) -> None:
    # One keyframe at the start only: a cut in the middle has none of its own, as is common in old YouTube videos.
    source = tmp_path / "sparse.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=8:size=160x120:rate=10",
            "-c:v", "libx264", "-g", "1000", "-pix_fmt", "yuv420p", str(source),
        ],
        check=True,
    )
    result = run_tool(ToolRequest("trim", str(source), {"start": 3.0, "end": 5.0}), Progress())
    assert result.output_path
    probe = probe_media(result.output_path)
    assert probe is not None
    assert any(stream["codec_type"] == "video" for stream in probe["streams"])


@needs_ffmpeg
def test_precise_trim_is_exact(workspace_video: Path) -> None:
    result = run_tool(ToolRequest("trim", str(workspace_video), {"start": 1.0, "end": 3.0, "precise": True}), Progress())
    assert result.output_path
    duration = get_media_duration(result.output_path)
    assert duration is not None and duration == pytest.approx(2.0, abs=0.2)


@needs_ffmpeg
def test_to_h264_reports_progress(workspace_video: Path) -> None:
    progress = Progress()
    result = run_tool(ToolRequest("to_h264", str(workspace_video)), progress)
    assert result.output_path and result.output_path.suffix == ".mp4"
    assert progress.values[-1] == 1.0


@needs_ffmpeg
def test_compress_can_lower_resolution(workspace_video: Path) -> None:
    result = run_tool(ToolRequest("compress", str(workspace_video), {"crf": 32, "height": 120}), Progress())
    assert result.output_path
    probe = probe_media(result.output_path)
    assert probe is not None
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    assert video["height"] == 120
    assert video["width"] == 160


@needs_ffmpeg
@pytest.mark.parametrize("audio_format", ["mp3", "m4a", "opus", "flac", "wav"])
def test_extract_audio(workspace_video: Path, audio_format: str) -> None:
    result = run_tool(ToolRequest("extract_audio", str(workspace_video), {"format": audio_format, "bitrate": 128}), Progress())
    assert result.error == ""
    assert result.output_path and result.output_path.suffix == f".{audio_format}"
    probe = probe_media(result.output_path)
    assert probe is not None and {stream["codec_type"] for stream in probe["streams"]} == {"audio"}


@needs_ffmpeg
def test_embed_subtitles(workspace_video: Path, sample_subtitle: Path) -> None:
    result = run_tool(ToolRequest("embed_subtitles", str(workspace_video), {"subtitle_path": str(sample_subtitle)}), Progress())
    assert result.error == ""
    assert result.output_path
    probe = probe_media(result.output_path)
    assert probe is not None
    assert any(stream["codec_type"] == "subtitle" for stream in probe["streams"])
    assert any("字幕" in detail for detail in describe_probe(probe))


@needs_ffmpeg
def test_burn_subtitles_with_awkward_paths(workspace_video: Path, sample_subtitle: Path) -> None:
    # Both paths contain quotes, brackets, and commas, and on Windows the drive colon, which break naive filter strings.
    result = run_tool(ToolRequest("burn_subtitles", str(workspace_video), {"subtitle_path": str(sample_subtitle)}), Progress())
    assert result.error == ""
    assert result.output_path and result.output_path.stat().st_size > 0


@needs_ffmpeg
def test_burning_characters_no_font_has_finishes_with_a_warning(workspace_video: Path) -> None:
    # A private-use character no installed font draws, standing in for Chinese on a server without Chinese fonts.
    subtitle = workspace_video.parent / "boxes.srt"
    subtitle.write_text("1\n00:00:00,500 --> 00:00:02,000\nA \U000f0001 B\n", encoding="utf-8")
    progress = Progress()
    result = run_tool(ToolRequest("burn_subtitles", str(workspace_video), {"subtitle_path": str(subtitle)}), progress)
    assert result.error == "" and result.output_path
    assert "方框" in result.warning
    assert "方框" in progress.texts[-1]

    subtitle.write_text("1\n00:00:00,500 --> 00:00:02,000\nplain words\n", encoding="utf-8")
    result = run_tool(ToolRequest("burn_subtitles", str(workspace_video), {"subtitle_path": str(subtitle)}), progress)
    assert result.output_path and result.warning == ""


def test_ffmpeg_warnings_are_kept_apart_from_its_errors() -> None:
    from videocap.media_tools import _run_ffmpeg

    lines = [
        "[swscaler @ 0000029b131e6800] [warning] deprecated pixel format used, make sure you did set range correctly",
        "[Parsed_subtitles_0 @ 0x748b04003c00] [warning] fontselect: failed to find any fallback with glyph 0x4E2D for font: (Arial, 400, 0)",
        "[in#0 @ 00000243db5ba000] [error] Error opening input: No such file or directory",
    ]
    for exit_code in (0, 1):
        script = f"import sys; print({chr(10).join(lines)!r}); sys.exit({exit_code})"
        result = _run_ffmpeg([sys.executable, "-c", script], None, "燒錄字幕", ".", Progress(), None, (0.0, 1.0))
        if exit_code:
            assert result.error == "ffmpeg 處理失敗：Error opening input: No such file or directory"
        else:
            assert result.error == "" and "方框" in result.warning


@needs_ffmpeg
def test_frame_capture(workspace_video: Path) -> None:
    result = run_tool(ToolRequest("frame", str(workspace_video), {"at": 2.0}), Progress())
    assert result.output_path and result.output_path.suffix == ".jpg"
    assert result.output_path.stat().st_size > 0


@needs_ffmpeg
def test_result_never_overwrites_the_source_or_earlier_results(workspace_video: Path) -> None:
    original = workspace_video.read_bytes()
    first = run_tool(ToolRequest("trim", str(workspace_video), {"start": 1.0}), Progress())
    second = run_tool(ToolRequest("trim", str(workspace_video), {"start": 1.0}), Progress())
    assert first.output_path and second.output_path
    assert first.output_path != second.output_path
    assert workspace_video.read_bytes() == original


@needs_ffmpeg
def test_stop_kills_ffmpeg_and_removes_the_partial_file(workspace_video: Path) -> None:
    result = run_tool(ToolRequest("to_h264", str(workspace_video)), Progress(), should_stop=lambda: True)
    assert result.stopped
    assert result.output_path is None
    assert not list(workspace_video.parent.glob("*_h264*"))


@needs_ffmpeg
def test_failure_reports_ffmpeg_output(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    result = run_tool(ToolRequest("to_h264", str(broken)), Progress())
    assert "ffmpeg 處理失敗" in result.error
    assert not list(tmp_path.glob("*_h264*"))


@needs_ffmpeg
def test_ffmpeg_is_tied_to_the_app(workspace_video: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # tests/test_processes.py shows a tied process ends when the app is killed.
    import videocap.media_tools as media_tools

    started: list[list[str]] = []
    real_start = media_tools.start_tied_process
    monkeypatch.setattr(media_tools, "start_tied_process", lambda command, **options: started.append(command) or real_start(command, **options))
    assert run_tool(ToolRequest("frame", str(workspace_video), {"at": 1.0}), Progress()).output_path
    assert started and Path(started[0][0]).stem.lower() == "ffmpeg"


def test_missing_source(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.media_tools as media_tools

    # Without this a machine lacking ffmpeg would pass on the 「需要 ffmpeg」 error instead.
    monkeypatch.setattr(media_tools, "get_ffmpeg_status", lambda: (True, "ffmpeg"))
    assert run_tool(ToolRequest("trim", "does-not-exist.mp4"), Progress()).error == "找不到來源檔案。"


def test_commands_are_argument_lists_with_the_name_intact(tmp_path: Path) -> None:
    title = '$(rm -rf ~); "quoted" & name.mp4'
    command = build_ffmpeg_command(ToolRequest("to_h264", title), tmp_path / "out.mp4", "ffmpeg")
    assert title in command


def test_unknown_tool_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_ffmpeg_command(ToolRequest("rm", "a.mp4"), tmp_path / "out.mp4", "ffmpeg")


def test_reserve_output_path(tmp_path: Path) -> None:
    (tmp_path / "a.mp4").write_bytes(b"")
    (tmp_path / "a (1).mp4").write_bytes(b"")
    assert reserve_output_path(tmp_path / "a.mp4") == tmp_path / "a (2).mp4"
    assert reserve_output_path(tmp_path / "b.mp4") == tmp_path / "b.mp4"
    # The name is taken as soon as it is handed out.
    assert reserve_output_path(tmp_path / "b.mp4") == tmp_path / "b (1).mp4"


def test_tools_started_together_get_different_files(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from videocap.media_tools import default_output_path

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x")
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: default_output_path(ToolRequest("trim", str(source))), range(8)))
    assert len(set(paths)) == 8


@pytest.fixture
def tools_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import streamlit as st

    import videocap.services as services_module

    monkeypatch.setenv("VIDEOCAP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VIDEOCAP_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("VIDEOCAP_TRUST_REMOTE", "1")
    monkeypatch.setenv("VIDEOCAP_MODE", "local")
    monkeypatch.delenv("VIDEOCAP_PASSWORD", raising=False)
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path / "public")
    st.cache_resource.clear()
    yield tmp_path / "out"
    st.cache_resource.clear()


def tools_page_script() -> None:
    from videocap.ui.context import build_context, init_session_state
    from videocap.ui.page_tools import render_tools_page

    ctx = build_context()
    init_session_state(ctx)
    render_tools_page(ctx)


def run_tools_page() -> Any:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_function(tools_page_script, default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    return app


BIG5_SRT = "1\n00:00:01,000 --> 00:00:02,000\n大家好，歡迎收看\n".encode("cp950")


def test_converter_reads_big5_uploads(tools_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    app = run_tools_page()
    app.file_uploader[0].set_value(("big5.srt", BIG5_SRT, "application/x-subrip"))
    app.run()
    assert not app.exception, app.exception
    assert app.text_area[0].value == "大家好，歡迎收看\n"
    assert not app.warning


def test_converter_refuses_huge_uploads_before_parsing(tools_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_tools as page_tools

    monkeypatch.setattr(page_tools, "parse_subtitles", lambda content: pytest.fail("parsed a huge upload"))
    app = run_tools_page()
    app.file_uploader[0].set_value(("huge.srt", b"1" * (3 * 1024 * 1024), "application/x-subrip"))
    app.run()
    assert "太大" in " ".join(item.value for item in app.error)


def test_subtitle_uploaders_say_in_chinese_how_big_a_file_may_be(tools_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    uploaders = run_tools_page().get("file_uploader")
    # The browser refuses a bigger file before the page sees it, with only Streamlit's English tooltip on its chip.
    assert [(uploader.proto.max_upload_size_mb, uploader.proto.help) for uploader in uploaders] == [
        (2, "最多 2 MB。超過的檔案瀏覽器會直接拒絕。")
    ] * 3


def make_media(path: Path, *arguments: str) -> Path:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *arguments, str(path)], check=True)
    return path


def video_input(seconds: int = 4, size: str = "160x120") -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=10"]


def audio_input(seconds: int = 4) -> list[str]:
    return ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]


def cover_art_song(folder: Path) -> Path:
    cover = make_media(folder / "cover.png", "-f", "lavfi", "-i", "color=red:size=64x64", "-frames:v", "1")
    song = make_media(
        folder / "cover.m4a", *audio_input(), "-i", str(cover),
        "-map", "0", "-map", "1", "-c:a", "aac", "-c:v", "png", "-disposition:v", "attached_pic",
    )
    cover.unlink()
    return song


def streams_of(path: Path, kind: str) -> list[dict[str, Any]]:
    probe = probe_media(path)
    assert probe is not None
    return [stream for stream in probe["streams"] if stream["codec_type"] == kind]


def video_size(path: Path) -> tuple[int, int]:
    video = streams_of(path, "video")[0]
    return video["width"], video["height"]


def leftovers(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir() if ".temp." in path.name or path.stat().st_size == 0)


@needs_ffmpeg
@pytest.mark.parametrize(
    ("name", "arguments", "video_codec", "audio_codec"),
    [
        ("clip.webm", [*video_input(), *audio_input(), "-c:v", "libvpx-vp9", "-c:a", "libopus"], "h264", "aac"),
        ("song.mp3", [*audio_input()], None, "mp3"),
        ("song.opus", [*audio_input(), "-c:a", "libopus"], None, "opus"),
        ("song.ogg", [*audio_input(), "-c:a", "libvorbis"], None, "vorbis"),
        ("song.flac", [*audio_input()], None, "flac"),
        ("song.wav", [*audio_input()], None, "pcm_s16le"),
    ],
)
def test_precise_trim_suits_the_container(tmp_path: Path, name: str, arguments: list[str], video_codec: Any, audio_codec: str) -> None:
    source = make_media(tmp_path / name, *arguments)
    result = run_tool(ToolRequest("trim", str(source), {"start": 1.0, "end": 2.5, "precise": True}), Progress())
    assert result.error == ""
    assert result.output_path is not None
    assert [stream["codec_name"] for stream in streams_of(result.output_path, "audio")] == [audio_codec]
    assert [stream["codec_name"] for stream in streams_of(result.output_path, "video")] == ([video_codec] if video_codec else [])
    assert get_media_duration(result.output_path) == pytest.approx(1.5, abs=0.15)


@needs_ffmpeg
def test_precise_trim_of_audio_with_cover_art(tmp_path: Path) -> None:
    result = run_tool(ToolRequest("trim", str(cover_art_song(tmp_path)), {"start": 1.0, "end": 2.5, "precise": True}), Progress())
    assert result.error == ""
    assert result.output_path and result.output_path.suffix == ".m4a"
    assert get_media_duration(result.output_path) == pytest.approx(1.5, abs=0.15)


@pytest.fixture
def subtitled_mkv(tmp_path: Path) -> Path:
    cues = tmp_path / "cues.srt"
    cues.write_text("\n".join(f"{i + 1}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\nsecond {i}\n" for i in range(20)), encoding="utf-8")
    video = make_media(
        tmp_path / "subs.mkv", *video_input(20), *audio_input(20), "-i", str(cues),
        "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "ass",
    )
    cues.unlink()
    return video


@needs_ffmpeg
def test_precise_trim_of_mkv_with_subtitles_is_exact(subtitled_mkv: Path) -> None:
    result = run_tool(ToolRequest("trim", str(subtitled_mkv), {"start": 10.0, "end": 13.0, "precise": True}), Progress())
    assert result.output_path
    assert get_media_duration(result.output_path) == pytest.approx(3.0, abs=0.1)
    assert float(streams_of(result.output_path, "video")[0]["start_time"]) == pytest.approx(0, abs=0.05)


@needs_ffmpeg
def test_reencoding_keeps_every_audio_track_and_the_subtitles(tmp_path: Path, sample_subtitle: Path) -> None:
    source = make_media(
        tmp_path / "multi.mp4", *video_input(), *audio_input(), *audio_input(), "-i", str(sample_subtitle),
        "-map", "0", "-map", "1", "-map", "2", "-map", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "mov_text",
        "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=jpn", "-metadata:s:s:0", "language=chi",
    )
    for tool, params in (("to_h264", {}), ("compress", {"crf": 40}), ("burn_subtitles", {"subtitle_path": str(sample_subtitle)})):
        result = run_tool(ToolRequest(tool, str(source), params), Progress())
        assert result.error == "", tool
        assert result.output_path
        assert [stream["tags"]["language"] for stream in streams_of(result.output_path, "audio")] == ["eng", "jpn"], tool
        assert [stream["tags"]["language"] for stream in streams_of(result.output_path, "subtitle")] == ["chi"], tool


@needs_ffmpeg
def test_odd_sized_videos_work_in_every_reencoding_tool(tmp_path: Path, sample_subtitle: Path) -> None:
    source = make_media(tmp_path / "odd.webm", *video_input(2, "161x121"), "-c:v", "libvpx-vp9")
    requests = [
        ("to_h264", {}),
        ("compress", {"crf": 28}),
        ("compress", {"crf": 28, "height": 101}),
        ("burn_subtitles", {"subtitle_path": str(sample_subtitle)}),
        ("trim", {"start": 0.5, "end": 1.5, "precise": True}),
    ]
    for tool, params in requests:
        result = run_tool(ToolRequest(tool, str(source), params), Progress())
        assert result.error == "", (tool, result.error)
        assert result.output_path
        width, height = video_size(result.output_path)
        assert width % 2 == 0 and height % 2 == 0, tool


@needs_ffmpeg
def test_compress_resolution_means_the_shorter_side(tmp_path: Path) -> None:
    source = make_media(tmp_path / "vertical.mp4", *video_input(2, "360x640"), "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p")
    smaller = run_tool(ToolRequest("compress", str(source), {"crf": 28, "height": 240}), Progress())
    assert smaller.output_path and video_size(smaller.output_path) == (240, 426)
    # Never enlarges.
    same = run_tool(ToolRequest("compress", str(source), {"crf": 28, "height": 480}), Progress())
    assert same.output_path and video_size(same.output_path) == (360, 640)


@needs_ffmpeg
def test_compress_that_would_grow_the_file_keeps_nothing(tmp_path: Path) -> None:
    source = make_media(tmp_path / "tiny.webm", *video_input(4, "320x240"), "-c:v", "libvpx-vp9", "-crf", "63", "-b:v", "0")
    result = run_tool(ToolRequest("compress", str(source), {"crf": 18}), Progress())
    assert result.output_path is None
    assert "沒有比原檔" in result.error
    assert [path.name for path in tmp_path.iterdir()] == ["tiny.webm"]


@needs_ffmpeg
def test_burn_and_precise_trim_make_video_every_player_can_play(tmp_path: Path, sample_subtitle: Path) -> None:
    sources = [
        make_media(tmp_path / "tenbit.webm", *video_input(2), "-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p10le"),
        make_media(tmp_path / "full.mp4", *video_input(2), "-c:v", "libx264", "-pix_fmt", "yuv444p"),
    ]
    for source in sources:
        for tool, params in (("burn_subtitles", {"subtitle_path": str(sample_subtitle)}), ("trim", {"start": 0.5, "precise": True})):
            result = run_tool(ToolRequest(tool, str(source), params), Progress())
            assert result.output_path, (source.name, tool, result.error)
            video = streams_of(result.output_path, "video")[0]
            assert (video["pix_fmt"], video["profile"]) == ("yuv420p", "High"), (source.name, tool)


@needs_ffmpeg
def test_hdr_video_is_tone_mapped_to_sdr(tmp_path: Path) -> None:
    source = make_media(
        tmp_path / "hdr.webm", *video_input(2), "-c:v", "libvpx-vp9",
        "-vf", "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc,format=yuv420p10le",
    )
    assert streams_of(source, "video")[0]["color_transfer"] == "smpte2084"
    result = run_tool(ToolRequest("to_h264", str(source)), Progress())
    assert result.output_path
    video = streams_of(result.output_path, "video")[0]
    assert (video["pix_fmt"], video.get("color_transfer"), video.get("color_primaries")) == ("yuv420p", "bt709", "bt709")


def test_error_message_keeps_the_cause_and_drops_the_teardown_noise() -> None:
    from videocap.media_tools import summarize_ffmpeg_error

    lines = [
        "[webm @ 000001d2c3a4b5c0] Only VP8 or VP9 or AV1 video and Vorbis or Opus audio and WebVTT subtitles are supported for WebM.",
        "[out#0/webm @ 000001d2c3a4b5c0] Could not write header (incorrect codec parameters ?): Invalid argument",
        "Error while filtering: Invalid argument",
        "[vf#0:0 @ 000001d2c3a4b5c0] Error sending frames to consumers: Invalid argument",
        "[vf#0:0 @ 000001d2c3a4b5c0] Task finished with error code: -22 (Invalid argument)",
        "[vf#0:0 @ 000001d2c3a4b5c0] Terminating thread with return code -22 (Invalid argument)",
        "Conversion failed!",
    ]
    message = summarize_ffmpeg_error(lines)
    assert message.startswith("ffmpeg 處理失敗：這種格式放不進 WebM 檔。")
    assert "Only VP8 or VP9" in message
    assert "Terminating" not in message and "@ 000001" not in message


@needs_ffmpeg
def test_times_past_the_end_are_refused(workspace_video: Path) -> None:
    for tool, params in (("frame", {"at": 100.0}), ("frame", {"at": 6.0}), ("trim", {"start": 10.0, "precise": True}), ("trim", {"start": 10.0})):
        result = run_tool(ToolRequest(tool, str(workspace_video), params), Progress())
        assert result.output_path is None, (tool, params)
        assert "超過了影片長度（00:06）" in result.error
    assert [path.name for path in workspace_video.parent.iterdir()] == [workspace_video.name]


@needs_ffmpeg
def test_an_empty_result_is_a_failure(workspace_video: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.media_tools as media_tools

    # Pretend the video is longer than it is, so the frame lands past the real end, where ffmpeg writes nothing and
    # still exits successfully.
    real = media_tools.read_media_info
    monkeypatch.setattr(media_tools, "read_media_info", lambda path: media_tools.replace(real(path), duration=60.0))
    result = run_tool(ToolRequest("frame", str(workspace_video), {"at": 30.0}), Progress())
    assert result.output_path is None
    # Older ffmpeg builds write nothing and exit 0; newer ones refuse the frame outright. Both must read as a failure.
    assert result.error
    assert [path.name for path in workspace_video.parent.iterdir()] == [workspace_video.name]


@needs_ffmpeg
def test_frame_of_a_title_with_percent_d(tmp_path: Path, sample_video: Path) -> None:
    source = tmp_path / "100%d growth %03d.mp4"
    shutil.copy(sample_video, source)
    result = run_tool(ToolRequest("frame", str(source), {"at": 1.0}), Progress())
    assert result.output_path == tmp_path / "100%d growth %03d_畫面.jpg"
    assert result.output_path.stat().st_size > 0
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted([source.name, result.output_path.name])


@needs_ffmpeg
def test_missing_subtitle_leaves_nothing_behind(workspace_video: Path) -> None:
    missing = str(workspace_video.parent / "gone.srt")
    result = run_tool(ToolRequest("burn_subtitles", str(workspace_video), {"subtitle_path": missing}), Progress())
    assert result.error == "找不到字幕檔，可能已被移動或刪除。"
    assert [path.name for path in workspace_video.parent.iterdir()] == [workspace_video.name]


@needs_ffmpeg
def test_the_library_never_lists_a_result_still_being_written(workspace_video: Path) -> None:
    from videocap.library import scan_folder

    seen: list[tuple[list[str], list[str]]] = []

    def look() -> bool:
        if not seen:
            scan = scan_folder(workspace_video.parent)
            seen.append(([entry.path.name for entry in scan.entries], [path.name for path in scan.partial_files]))
        return False

    result = run_tool(ToolRequest("to_h264", str(workspace_video)), Progress(), should_stop=look)
    assert result.output_path
    assert seen == [([workspace_video.name], [f"{workspace_video.stem}_h264.temp.mp4"])]
    assert {entry.path.name for entry in scan_folder(workspace_video.parent).entries} == {result.output_path.name, workspace_video.name}


@needs_ffmpeg
def test_video_tools_refuse_files_without_video(tmp_path: Path, sample_subtitle: Path) -> None:
    songs = [make_media(tmp_path / "song.mp3", *audio_input()), cover_art_song(tmp_path)]
    for song in songs:
        for tool in ("to_h264", "compress", "burn_subtitles", "embed_subtitles", "frame"):
            result = run_tool(ToolRequest(tool, str(song), {"subtitle_path": str(sample_subtitle)}), Progress())
            assert "沒有影像" in result.error, (song.name, tool)
    assert len(list(tmp_path.iterdir())) == 2


@needs_ffmpeg
def test_embedding_into_mkv_keeps_existing_tracks_and_names_the_language(subtitled_mkv: Path, sample_subtitle: Path) -> None:
    extra = subtitled_mkv.parent / "subs.zh-TW.srt"
    shutil.copy(sample_subtitle, extra)
    result = run_tool(ToolRequest("embed_subtitles", str(subtitled_mkv), {"subtitle_path": str(extra)}), Progress())
    assert result.output_path and result.output_path.suffix == ".mkv"
    subtitles = streams_of(result.output_path, "subtitle")
    assert [stream["codec_name"] for stream in subtitles] == ["ass", "subrip"]
    assert subtitles[1]["tags"]["language"] == "chi"
    assert subtitles[1]["tags"]["title"] == "zh-TW"


@needs_ffmpeg
def test_embedding_into_a_container_without_subtitles_writes_mkv(tmp_path: Path, sample_subtitle: Path) -> None:
    source = make_media(tmp_path / "old.avi", *video_input(2), "-c:v", "mpeg4")
    result = run_tool(ToolRequest("embed_subtitles", str(source), {"subtitle_path": str(sample_subtitle)}), Progress())
    assert result.error == ""
    assert result.output_path == tmp_path / "old_含字幕.mkv"
    assert len(streams_of(result.output_path, "subtitle")) == 1


@needs_ffmpeg
def test_big5_subtitles_can_be_embedded_and_burned(workspace_video: Path) -> None:
    subtitle = workspace_video.parent / "big5.zh-TW.srt"
    subtitle.write_bytes("1\n00:00:00,500 --> 00:00:02,000\n大家好，歡迎收看\n".encode("cp950"))
    for tool in ("embed_subtitles", "burn_subtitles"):
        result = run_tool(ToolRequest(tool, str(workspace_video), {"subtitle_path": str(subtitle)}), Progress())
        assert result.error == "", tool
    embedded = workspace_video.with_name(f"{workspace_video.stem}_含字幕.mp4")
    extracted = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(embedded), "-map", "0:s:0", "-f", "srt", "-"], capture_output=True, check=True
    )
    assert "大家好，歡迎收看" in extracted.stdout.decode("utf-8")


@needs_ffmpeg
def test_relative_paths_are_found(workspace_video: Path, sample_subtitle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shutil.copy(sample_subtitle, workspace_video.parent / "sub.srt")
    monkeypatch.chdir(workspace_video.parent)
    for tool in ("extract_audio", "embed_subtitles"):
        result = run_tool(ToolRequest(tool, workspace_video.name, {"subtitle_path": "sub.srt"}), Progress())
        assert result.error == "", tool
        assert result.output_path and result.output_path.is_absolute()


@needs_ffmpeg
def test_compress_to_a_target_size(tmp_path: Path) -> None:
    source = make_media(
        tmp_path / "busy.mp4", "-f", "lavfi", "-i", "testsrc2=duration=6:size=320x240:rate=25", *audio_input(6),
        "-vf", "noise=alls=30:allf=t", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac",
    )
    assert source.stat().st_size > 1024 * 1024
    progress = Progress()
    result = run_tool(ToolRequest("compress", str(source), {"target_mb": 0.4}), progress)
    assert result.error == ""
    assert result.output_path and result.output_path.stat().st_size <= 0.4 * 1024 * 1024
    assert progress.values[-1] == 1.0
    assert any(0.4 < value < 0.6 for value in progress.values)
    assert not leftovers(tmp_path)

    too_small = run_tool(ToolRequest("compress", str(source), {"target_mb": 0.05}), Progress())
    assert "目標大小太小，這部影片至少要 0.2 MB" in too_small.error


@needs_ffmpeg
def test_a_target_size_the_file_already_meets_keeps_the_original(workspace_video: Path) -> None:
    before = sorted(workspace_video.parent.iterdir())
    result = run_tool(ToolRequest("compress", str(workspace_video), {"target_mb": 50}), Progress())
    # Not a failure, which would offer a retry that can only fail again: the original is the result.
    assert (result.error, result.output_path) == ("", workspace_video)
    assert result.warning == "原檔已經小於 50 MB，不需要壓縮。"
    assert sorted(workspace_video.parent.iterdir()) == before


@needs_ffmpeg
def test_a_target_size_the_file_already_meets_shows_as_done_on_the_tools_page(tools_app: Path, sample_video: Path) -> None:
    import time

    import videocap.services as services_module
    from videocap.jobs import FINISHED_STATES

    tools_app.mkdir(parents=True)
    shutil.copy(sample_video, tools_app / "clip.mp4")
    app = run_tools_page()
    by_label(app.radio, "方式").set_value("target")
    app.run()
    by_label(app.number_input, "目標大小（MB）").set_value(50.0)
    by_label(app.button, "開始壓縮").click()
    app.run()
    job = services_module.get_shared_services(False).manager.list_jobs()[0]
    deadline = time.monotonic() + 20
    while job.state not in FINISHED_STATES and time.monotonic() < deadline:
        time.sleep(0.05)
    app.run()
    assert not app.exception, app.exception
    assert job.state == "done"
    assert not app.error
    assert "原檔已經小於 50 MB，不需要壓縮。" in " ".join(item.value for item in app.warning)
    assert "重試" not in [item.label for item in app.button]


@pytest.fixture
def submitted(monkeypatch: pytest.MonkeyPatch) -> list[ToolRequest]:
    from videocap.services import AppServices

    requests: list[ToolRequest] = []
    monkeypatch.setattr(AppServices, "submit_tool", lambda self, request, owner: requests.append(request))
    return requests


def by_label(widgets: Any, label: str) -> Any:
    return next(widget for widget in widgets if widget.label == label)


def test_a_finished_burn_with_missing_fonts_shows_a_warning(tools_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    import videocap.services as services_module
    from videocap.media_tools import MISSING_FONT_WARNING, ToolResult

    def burn_with_boxes(request: ToolRequest, progress_ui: Any, should_stop: Any = None) -> ToolResult:
        progress_ui.progress(1.0, text=f"燒錄字幕（永久顯示）完成，但{MISSING_FONT_WARNING}")
        return ToolResult(output_path=tools_app / "clip_燒錄字幕.mp4", warning=MISSING_FONT_WARNING)

    monkeypatch.setattr(services_module, "run_tool", burn_with_boxes)
    app = run_tools_page()
    job = services_module.get_shared_services(False).submit_tool(ToolRequest("burn_subtitles", str(tools_app / "clip.mp4")), services_module.LOCAL_OWNER)
    deadline = time.monotonic() + 10
    while job.state != "done" and time.monotonic() < deadline:
        time.sleep(0.05)
    app.run()
    assert not app.exception, app.exception
    # Once, not again under the result.
    assert len([warning for warning in app.warning if "方框" in warning.value]) == 1


@needs_ffmpeg
def test_subtitle_merge_follows_the_source(tools_app: Path, submitted: list[ToolRequest]) -> None:
    tools_app.mkdir(parents=True)
    for name in ("Alpha clip.mp4", "Alpha clip.en.srt", "Bravo clip.mp4", "Bravo clip.zh-TW.srt"):
        (tools_app / name).write_bytes(b"x")
    app = run_tools_page()
    by_label(app.selectbox, "來源檔案").set_value(str(tools_app.resolve() / "Alpha clip.mp4"))
    app.run()
    assert Path(by_label(app.selectbox, "字幕檔").value).name == "Alpha clip.en.srt"

    by_label(app.selectbox, "來源檔案").set_value(str(tools_app.resolve() / "Bravo clip.mp4"))
    app.run()
    assert Path(by_label(app.selectbox, "字幕檔").value).name == "Bravo clip.zh-TW.srt"
    by_label(app.button, "開始處理").click()
    app.run()
    assert [Path(request.params["subtitle_path"]).name for request in submitted] == ["Bravo clip.zh-TW.srt"]


@needs_ffmpeg
def test_a_typed_relative_path_becomes_a_full_path(tools_app: Path, submitted: list[ToolRequest], monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.testing.v1 import AppTest

    folder = tools_app.parent / "elsewhere"
    folder.mkdir()
    (folder / "clip.mp4").write_bytes(b"x")
    monkeypatch.chdir(folder)
    app = AppTest.from_function(tools_page_script, default_timeout=30)
    # As if typed into the box, which keeps what was typed.
    app.session_state["keep_tool_source"] = "clip.mp4"
    app.run()
    by_label(app.button, "抽出音訊").click()
    app.run()
    assert [request.input_path for request in submitted] == [str(folder.resolve() / "clip.mp4")]


@needs_ffmpeg
def test_compress_to_a_target_size_from_the_page(tools_app: Path, submitted: list[ToolRequest]) -> None:
    tools_app.mkdir(parents=True)
    (tools_app / "clip.mp4").write_bytes(b"x")
    app = run_tools_page()
    by_label(app.radio, "方式").set_value("target")
    app.run()
    by_label(app.number_input, "目標大小（MB）").set_value(8.0)
    by_label(app.button, "開始壓縮").click()
    app.run()
    assert [request.params for request in submitted] == [{"target_mb": 8.0, "height": None}]


@needs_ffmpeg
def test_times_past_the_end_are_caught_before_queueing(tools_app: Path, submitted: list[ToolRequest], sample_video: Path) -> None:
    tools_app.mkdir(parents=True)
    shutil.copy(sample_video, tools_app / "clip.mp4")
    app = run_tools_page()
    by_label(app.text_input, "時間點").set_value("10:00")
    by_label(app.button, "擷取畫面").click()
    app.run()
    assert "時間點超過了影片長度（00:06）" in app.error[0].value
    by_label(app.text_input, "開始時間").set_value("0:07")
    by_label(app.button, "開始剪輯").click()
    app.run()
    assert "開始時間超過了影片長度" in app.error[0].value
    assert submitted == []


SIMPLIFIED_SRT = "1\n00:00:01,000 --> 00:00:02,000\n这个软件的视频\n\n2\n00:00:02,500 --> 00:00:04,000\nHello 网络\n".encode("utf-8")


def test_converter_turns_simplified_into_taiwan_traditional(tools_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Public visitors get the converter too.
    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    app = run_tools_page()
    app.file_uploader[0].set_value(("clip.zh-Hans.srt", SIMPLIFIED_SRT, "application/x-subrip"))
    by_label(app.selectbox, "轉成").set_value("srt")
    app.run()
    assert "这个软件的视频" in app.text_area[0].value
    by_label(app.checkbox, "轉成台灣繁體").check()
    app.run()
    assert not app.exception, app.exception
    assert app.text_area[0].value == (
        "1\n00:00:01,000 --> 00:00:02,000\n這個軟體的影片\n\n2\n00:00:02,500 --> 00:00:04,000\nHello 網路\n"
    )


def test_converted_results_are_named_for_their_language() -> None:
    from videocap.ui.page_tools import convert_subtitle_text, result_stem

    assert result_stem("clip.zh-Hans.srt", True) == "clip.zh-TW"
    assert result_stem("clip.zh-Hans.srt", False) == "clip.zh-Hans"
    assert result_stem("clip.srt", True) == "clip.zh-TW"
    assert convert_subtitle_text(SIMPLIFIED_SRT.decode("utf-8"), "txt", traditional=True) == "這個軟體的影片 Hello 網路\n"


def test_converter_explains_a_missing_opencc(tools_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap import chinese

    monkeypatch.setitem(sys.modules, "opencc", None)
    chinese._converter.cache_clear()
    try:
        app = run_tools_page()
        app.file_uploader[0].set_value(("clip.zh-Hans.srt", SIMPLIFIED_SRT, "application/x-subrip"))
        by_label(app.checkbox, "轉成台灣繁體").check()
        app.run()
        assert not app.exception, app.exception
        assert "OpenCC" in app.error[0].value
    finally:
        chinese._converter.cache_clear()


def test_bilingual_subtitles_from_the_page(tools_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.subtitles import parse_subtitles

    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    app = run_tools_page()
    english = b"1\n00:00:01,100 --> 00:00:02,000\nThis software\n\n2\n00:00:09,000 --> 00:00:10,000\nBye\n"
    by_label(app.file_uploader, "上面一行（例如中文）").set_value(("clip.zh-Hans.srt", SIMPLIFIED_SRT, "application/x-subrip"))
    app.run()
    # Nothing is merged until both files are there.
    assert not app.text_area
    by_label(app.file_uploader, "下面一行（例如英文）").set_value(("clip.en.vtt", english, "text/vtt"))
    by_label(app.radio, "存成").set_value("vtt")
    app.run()
    assert not app.exception, app.exception
    merged = app.text_area[0].value
    assert merged.startswith("WEBVTT")
    assert [cue.lines for cue in parse_subtitles(merged)] == [("这个软件的视频", "This software"), ("Hello 网络",), ("Bye",)]

    app.checkbox(key="bilingual_traditional").check()
    app.run()
    assert [cue.lines for cue in parse_subtitles(app.text_area[0].value)][0] == ("這個軟體的影片", "This software")


def test_bilingual_page_explains_a_side_without_cues_and_the_same_file_twice(tools_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    app = run_tools_page()
    top, bottom = by_label(app.file_uploader, "上面一行（例如中文）"), by_label(app.file_uploader, "下面一行（例如英文）")
    top.set_value(("clip.zh-Hans.srt", SIMPLIFIED_SRT, "application/x-subrip"))
    bottom.set_value(("notes.vtt", b"WEBVTT\n\nNOTE nothing to show\n", "text/vtt"))
    app.run()
    assert not app.exception, app.exception
    # The name is escaped for Markdown.
    assert [warning.value.replace("\\", "") for warning in app.warning] == ["「notes.vtt」裡沒有讀到字幕，結果只有另一個檔案的字幕。"]
    assert app.text_area[0].value.startswith("1\n")

    by_label(app.file_uploader, "下面一行（例如英文）").set_value(("copy.srt", SIMPLIFIED_SRT, "application/x-subrip"))
    app.run()
    assert [warning.value for warning in app.warning] == ["上下兩個檔案的字幕一樣，每句會出現兩次。請確認選的是兩種語言的字幕。"]

    by_label(app.file_uploader, "下面一行（例如英文）").set_value(("empty.srt", b"", "application/x-subrip"))
    by_label(app.file_uploader, "上面一行（例如中文）").set_value(("blank.vtt", b"WEBVTT\n", "text/vtt"))
    app.run()
    assert [warning.value for warning in app.warning] == ["這兩個檔案裡沒有讀到字幕內容。"]
    assert not app.text_area


def test_bilingual_upload_size_is_checked_again(tools_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_tools as page_tools

    monkeypatch.setattr(page_tools, "parse_subtitles", lambda content: pytest.fail("parsed a huge upload"))
    app = run_tools_page()
    by_label(app.file_uploader, "上面一行（例如中文）").set_value(("a.srt", SIMPLIFIED_SRT, "application/x-subrip"))
    by_label(app.file_uploader, "下面一行（例如英文）").set_value(("huge.srt", b"1" * (3 * 1024 * 1024), "application/x-subrip"))
    app.run()
    assert "太大" in " ".join(item.value for item in app.error)


def test_bilingual_file_name() -> None:
    from videocap.ui.page_tools import bilingual_name

    assert bilingual_name("clip.zh-Hans.srt", "srt") == "clip.雙語.srt"
    assert bilingual_name("Node.js 入門.vtt", "vtt") == "Node.js 入門.雙語.vtt"


def gray_frame(path: Path, at: float = 1.0) -> list[bytes]:
    """The frame's rows as 8-bit brightness."""
    width, height = video_size(path)
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True,
        check=True,
    ).stdout
    return [raw[row * width : (row + 1) * width] for row in range(height)]


def count_pixels(rows: list[bytes], test: Any) -> int:
    return sum(1 for row in rows for value in row if test(value))


@pytest.fixture
def gray_video(tmp_path: Path) -> tuple[Path, Path]:
    video = make_media(tmp_path / "gray.mp4", "-f", "lavfi", "-i", "color=c=0x808080:size=320x240:rate=10:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p")
    subtitle = tmp_path / "gray.en.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:02,000\nHHHH HHHH\n", encoding="utf-8")
    return video, subtitle


def burn(video: Path, subtitle: Path, **style: Any) -> list[bytes]:
    result = run_tool(ToolRequest("burn_subtitles", str(video), {"subtitle_path": str(subtitle), **style}), Progress())
    assert result.error == "" and result.output_path, result.error
    return gray_frame(result.output_path)


@needs_ffmpeg
def test_burned_subtitles_can_go_on_top(gray_video: tuple[Path, Path]) -> None:
    def bright(value: int) -> bool:
        return value > 220

    bottom = burn(*gray_video, font_size=30)
    top = burn(*gray_video, font_size=30, position="top")
    assert count_pixels(bottom[:80], bright) == 0 and count_pixels(bottom[160:], bright) > 50
    assert count_pixels(top[:80], bright) > 50 and count_pixels(top[160:], bright) == 0


@needs_ffmpeg
def test_burned_subtitle_size_and_box(gray_video: tuple[Path, Path]) -> None:
    def bright(value: int) -> bool:
        return value > 220

    def half_darkened(value: int) -> bool:
        # The gray background under the half-transparent black box.
        return 50 <= value <= 80

    small, large = burn(*gray_video, font_size=12), burn(*gray_video, font_size=30)
    assert count_pixels(large, bright) > 3 * count_pixels(small, bright)
    outline, box = burn(*gray_video, border="outline"), burn(*gray_video, border="box")
    # The box darkens the whole area behind the text, not just a thin edge around each letter.
    assert count_pixels(box, half_darkened) > 4 * count_pixels(outline, half_darkened)


@needs_ffmpeg
def test_burned_subtitles_in_a_named_font(gray_video: tuple[Path, Path]) -> None:
    from videocap.media_tools import default_cjk_font

    font = default_cjk_font()
    if not font:
        pytest.skip("no Chinese font on this machine")
    video, subtitle = gray_video
    subtitle.write_text("1\n00:00:00,000 --> 00:00:02,000\n大家好，歡迎收看\n", encoding="utf-8")
    result = run_tool(ToolRequest("burn_subtitles", str(video), {"subtitle_path": str(subtitle), "font_name": font, "font_size": 22}), Progress())
    assert result.error == "" and result.output_path
    assert result.warning == ""
    # Characters a font name may not carry are dropped rather than breaking the filter.
    result = run_tool(ToolRequest("burn_subtitles", str(video), {"subtitle_path": str(subtitle), "font_name": f"{font}',:[x]=\\"}), Progress())
    assert result.error == "" and result.output_path


HOSTILE_SUBTITLE_NAMES = [
    "a'b,c;d[e]f=g%h 中文 字幕.srt",
    # The part after the last dot is all that reaches the filter graph, so these are the ones that could inject.
    "clip.s'rt",
    "clip.srt,negate",
    "clip.srt;negate",
    "clip.srt]",
    "clip.srt[x]",
    "clip.%d",
    "clip.字幕",
    "clip. srt",
]
if sys.platform != "win32":
    HOSTILE_SUBTITLE_NAMES += ["clip.srt:force_style=FontSize=90", "clip.s\\'rt", "clip.srt\nnegate"]


@needs_ffmpeg
@pytest.mark.parametrize("name", HOSTILE_SUBTITLE_NAMES)
def test_hostile_subtitle_names_cannot_reach_the_filter_graph(gray_video: tuple[Path, Path], name: str) -> None:
    video, subtitle = gray_video
    hostile = subtitle.with_name(name)
    subtitle.rename(hostile)
    rows = burn(video, hostile, font_size=30, font_name="Arial,negate'\n:[x]")
    # Drawn as a subtitle, on a picture no injected filter has changed. A machine without the font falls back to a
    # thinner one, so only a few bright pixels are certain.
    assert count_pixels(rows, lambda value: value > 220) > 5
    assert count_pixels(rows, lambda value: 120 <= value <= 136) > 0.8 * sum(len(row) for row in rows)
    embedded = run_tool(ToolRequest("embed_subtitles", str(video), {"subtitle_path": str(hostile)}), Progress())
    assert embedded.error == "" and embedded.output_path
    assert len(streams_of(embedded.output_path, "subtitle")) == 1


@needs_ffmpeg
@pytest.mark.parametrize("font", ["Microsoft JhengHei\nnegate", "微軟 正黑體;%d", "x',:[0:v]=\\\\overlay", "\r\n\t"])
def test_hostile_font_names_never_break_the_burn(gray_video: tuple[Path, Path], font: str) -> None:
    rows = burn(*gray_video, font_size=30, font_name=font)
    assert count_pixels(rows, lambda value: value > 220) > 5


def test_style_goes_only_to_srt_and_vtt() -> None:
    from videocap.media_tools import burn_subtitle_filter

    style = {"font_name": "Noto Sans CJK TC", "font_size": 22, "position": "top", "border": "box"}
    srt = burn_subtitle_filter(ToolRequest("burn_subtitles", "a.mp4", {"subtitle_path": "a.zh.srt", **style}))
    assert srt == (
        r"subtitles=subtitle.srt:force_style=FontName=Noto Sans CJK TC\,FontSize=22\,Alignment=6\,BorderStyle=3"
        r"\,OutlineColour=&H80000000\,BackColour=&H80000000\,Outline=1\,Shadow=0"
    )
    assert burn_subtitle_filter(ToolRequest("burn_subtitles", "a.mp4", {"subtitle_path": "a.ass", **style})) == "subtitles=subtitle.ass"
    # Nothing chosen keeps today's filter exactly.
    assert burn_subtitle_filter(ToolRequest("burn_subtitles", "a.mp4", {"subtitle_path": "a.vtt"})) == "subtitles=subtitle.vtt"


def test_filter_values_are_escaped_for_both_readings() -> None:
    from videocap.media_tools import burn_force_style, escape_filter_value

    assert escape_filter_value("a,b") == r"a\,b"
    assert escape_filter_value("a:b") == r"a\\:b"
    assert escape_filter_value("it's") == r"it\\\'s"
    assert escape_filter_value("x[1];y") == r"x\[1\]\;y"
    assert burn_force_style({"font_name": "Evil, Font=1:'x'", "font_size": 999}) == "FontName=Evil Font1x,FontSize=120"
    assert burn_force_style({"position": "middle", "border": "shadow"}) == ""


def test_default_cjk_font_is_installed() -> None:
    from videocap.media_tools import default_cjk_font

    font = default_cjk_font()
    if sys.platform == "win32":
        assert font in {"Microsoft JhengHei", "MingLiU", "Microsoft YaHei", "SimSun"}


@needs_ffmpeg
def test_burn_style_from_the_page(tools_app: Path, submitted: list[ToolRequest], monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_tools as page_tools

    monkeypatch.setattr(page_tools, "default_cjk_font", lambda: "Noto Sans CJK TC")
    tools_app.mkdir(parents=True)
    for name in ("clip.mp4", "clip.zh-TW.srt"):
        (tools_app / name).write_bytes(b"x")
    app = run_tools_page()
    # Embedding has no look to choose.
    assert not [widget for widget in app.selectbox if widget.label == "字體大小"]
    app.radio(key="merge_mode").set_value("burn_subtitles")
    app.run()
    assert by_label(app.text_input, "字型").value == "Noto Sans CJK TC"
    by_label(app.button, "開始處理").click()
    app.run()
    assert submitted[-1].params == {"subtitle_path": str(tools_app.resolve() / "clip.zh-TW.srt"), "font_name": "Noto Sans CJK TC"}

    by_label(app.selectbox, "字體大小").set_value("特大")
    by_label(app.radio, "位置").set_value("top")
    by_label(app.radio, "文字背景").set_value("box")
    by_label(app.text_input, "字型").set_value("")
    by_label(app.button, "開始處理").click()
    app.run()
    assert not app.exception, app.exception
    assert submitted[-1].tool == "burn_subtitles"
    assert submitted[-1].params == {"subtitle_path": str(tools_app.resolve() / "clip.zh-TW.srt"), "font_size": 30, "position": "top", "border": "box"}


class FakeFfmpeg:
    """Answers the encoder checks the way ffmpeg would on a machine with the given encoders built in and working."""

    def __init__(self, listed: list[str], working: list[str], listing_code: int = 0) -> None:
        self.listed, self.working, self.listing_code = listed, working, listing_code
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> tuple[int, str]:
        self.commands.append(command)
        if "-encoders" in command:
            lines = ["Encoders:", " ------", " V....D libx264              libx264 H.264 / AVC (codec h264)"]
            lines += [f" V....D {name:<20} hardware H.264 (codec h264)" for name in self.listed]
            return self.listing_code, "\n".join(lines)
        encoder = command[command.index("-c:v") + 1]
        return (0 if encoder in self.working else 1), ""

    def tried(self) -> list[str]:
        return [command[command.index("-c:v") + 1] for command in self.commands if "-c:v" in command]


def test_hardware_encoder_detection_test_encodes_what_ffmpeg_lists() -> None:
    from videocap.media_tools import detect_hardware_encoder

    # Listed but with no device behind it, as ffmpeg builds for Windows list NVENC everywhere.
    fake = FakeFfmpeg(listed=["h264_nvenc", "h264_qsv", "h264_amf"], working=["h264_qsv", "h264_amf"])
    assert detect_hardware_encoder("ffmpeg", fake) == "h264_qsv"
    assert fake.tried() == ["h264_nvenc", "h264_qsv"]
    test_encode = fake.commands[1]
    assert test_encode[:3] == ["ffmpeg", "-hide_banner", "-nostdin"] and test_encode[-3:] == ["-f", "null", "-"]

    nothing_works = FakeFfmpeg(listed=["h264_nvenc", "h264_amf"], working=[])
    assert detect_hardware_encoder("ffmpeg", nothing_works) == ""
    assert nothing_works.tried() == ["h264_nvenc", "h264_amf"]
    # An encoder that is not listed is never tried, even if it would work.
    assert detect_hardware_encoder("ffmpeg", FakeFfmpeg(listed=["h264_amf"], working=["h264_nvenc", "h264_amf"])) == "h264_amf"
    assert detect_hardware_encoder("ffmpeg", FakeFfmpeg(listed=[], working=["h264_nvenc"])) == ""
    broken = FakeFfmpeg(listed=["h264_nvenc"], working=["h264_nvenc"], listing_code=-1)
    assert detect_hardware_encoder("ffmpeg", broken) == "" and broken.tried() == []


@needs_ffmpeg
def test_detection_with_the_real_ffmpeg_finds_a_working_encoder_or_none() -> None:
    from videocap.media_tools import HARDWARE_H264_ENCODERS, detect_hardware_encoder
    from videocap.tooling import get_ffmpeg_status

    ffmpeg = get_ffmpeg_status()[1]
    encoder = detect_hardware_encoder(ffmpeg)
    assert encoder in {"", *HARDWARE_H264_ENCODERS}
    if encoder:
        frame = ["-f", "lavfi", "-i", "color=c=black:size=256x256:rate=1", "-frames:v", "1", "-c:v", encoder, "-f", "null", "-"]
        assert subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", *frame], capture_output=True).returncode == 0


def test_hardware_encoder_is_detected_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.media_tools as media_tools

    calls: list[str] = []
    monkeypatch.setattr(media_tools, "detect_hardware_encoder", lambda ffmpeg: calls.append(ffmpeg) or "h264_nvenc")
    monkeypatch.setattr(media_tools, "_hardware_encoders", {})
    assert [media_tools.hardware_h264_encoder("ffmpeg") for _ in range(3)] == ["h264_nvenc"] * 3
    assert calls == ["ffmpeg"]


def test_no_gpu_is_remembered_and_concurrent_pages_detect_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    import videocap.media_tools as media_tools

    calls: list[str] = []

    def slow_nothing(ffmpeg: str) -> str:
        calls.append(ffmpeg)
        time.sleep(0.3)
        return ""

    monkeypatch.setattr(media_tools, "detect_hardware_encoder", slow_nothing)
    monkeypatch.setattr(media_tools, "_hardware_encoders", {})
    # Two visitors opening the tools page at the same moment.
    threads = [threading.Thread(target=media_tools.hardware_h264_encoder, args=("ffmpeg",)) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert media_tools.hardware_h264_encoder("ffmpeg") == ""
    assert calls == ["ffmpeg"]
    assert media_tools.hardware_h264_encoder("other-ffmpeg") == "" and calls == ["ffmpeg", "other-ffmpeg"]


def test_a_hung_encoder_check_is_cut_short() -> None:
    import time

    import videocap.media_tools as media_tools

    # A page waits for detection, so one check must never take long.
    assert media_tools.ENCODER_TEST_SECONDS <= 10
    started = time.monotonic()
    assert media_tools.run_quietly([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1) == (-1, "")
    assert time.monotonic() - started < 8
    # After one check hangs, as a stuck driver does, the other encoders are not tried too.
    fake = FakeFfmpeg(listed=["h264_nvenc", "h264_qsv", "h264_amf"], working=["h264_qsv"])

    def hangs_on_nvenc(command: list[str]) -> tuple[int, str]:
        return (-1, "") if "h264_nvenc" in command else fake(command)

    assert media_tools.detect_hardware_encoder("ffmpeg", hangs_on_nvenc) == ""
    assert fake.tried() == []


def test_visitors_never_trigger_gpu_detection(tools_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.media_tools as media_tools
    import videocap.ui.page_tools as page_tools

    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    tools_app.mkdir(parents=True)
    (tools_app / "clip.mp4").write_bytes(b"x")
    monkeypatch.setattr(page_tools, "hardware_h264_encoder", lambda ffmpeg: pytest.fail("a visitor's page probed the GPU"))
    monkeypatch.setattr(media_tools, "detect_hardware_encoder", lambda *args: pytest.fail("a visitor's page probed the GPU"))
    app = run_tools_page()
    assert not [box for box in app.checkbox if box.label == page_tools.GPU_LABEL]


def video_codec_options(command: list[str]) -> list[str]:
    start = command.index("-c:v")
    end = next(index for index in range(start, len(command)) if command[index] in ("-c:a", "-an"))
    return command[start:end]


def test_gpu_commands_map_quality_to_each_encoder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.media_tools as media_tools

    out = tmp_path / "out.mp4"
    expected = {
        "h264_nvenc": ["-c:v", "h264_nvenc", "-profile:v", "high", "-rc", "vbr", "-cq", "32", "-b:v", "0", "-preset", "p5"],
        "h264_qsv": ["-c:v", "h264_qsv", "-profile:v", "high", "-global_quality", "32", "-preset", "medium"],
        "h264_amf": ["-c:v", "h264_amf", "-profile:v", "high", "-rc", "cqp", "-qp_i", "32", "-qp_p", "32", "-qp_b", "32", "-quality", "balanced"],
    }
    for encoder, options in expected.items():
        monkeypatch.setattr(media_tools, "hardware_h264_encoder", lambda ffmpeg, encoder=encoder: encoder)
        command = build_ffmpeg_command(ToolRequest("compress", "in.mp4", {"crf": 32, "gpu": True}), out, "ffmpeg")
        assert video_codec_options(command) == options
    burn = build_ffmpeg_command(ToolRequest("burn_subtitles", "in.mp4", {"subtitle_path": "a.srt", "gpu": True}), out, "ffmpeg")
    assert video_codec_options(burn)[:2] == ["-c:v", "h264_amf"]

    libx264 = ["-c:v", "libx264", "-crf", "20", "-preset", "medium"]
    # Unchecked, a target size, and tools without the option all stay on libx264.
    assert video_codec_options(build_ffmpeg_command(ToolRequest("to_h264", "in.mp4"), out, "ffmpeg")) == libx264
    target = build_ffmpeg_command(ToolRequest("compress", "in.mp4", {"target_mb": 5, "gpu": True}), out, "ffmpeg")
    assert target[target.index("-c:v") + 1] == "libx264"
    trim = build_ffmpeg_command(ToolRequest("trim", "in.mp4", {"start": 1, "end": 2, "precise": True, "gpu": True}), out, "ffmpeg")
    assert trim[trim.index("-c:v") + 1] == "libx264"
    monkeypatch.setattr(media_tools, "hardware_h264_encoder", lambda ffmpeg: "")
    assert video_codec_options(build_ffmpeg_command(ToolRequest("to_h264", "in.mp4", {"gpu": True}), out, "ffmpeg")) == libx264


@needs_ffmpeg
def test_a_failing_gpu_encoder_falls_back_to_libx264(workspace_video: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.media_tools as media_tools

    # Detection said yes, but the encoder fails once encoding starts, as with a driver that is too old.
    monkeypatch.setattr(media_tools, "hardware_h264_encoder", lambda ffmpeg: "h264_no_such_encoder")
    progress = Progress()
    result = run_tool(ToolRequest("to_h264", str(workspace_video), {"gpu": True}), progress)
    assert result.error == "" and result.output_path
    assert "顯示卡加速無法使用" in result.warning
    assert media_tools.GPU_FALLBACK_TEXT in progress.texts
    assert streams_of(result.output_path, "video")[0]["codec_name"] == "h264"
    assert not leftovers(workspace_video.parent)


@needs_ffmpeg
def test_real_gpu_encoding(workspace_video: Path, sample_subtitle: Path) -> None:
    from videocap.media_tools import hardware_h264_encoder
    from videocap.tooling import get_ffmpeg_status

    if not hardware_h264_encoder(get_ffmpeg_status()[1]):
        pytest.skip("no hardware H.264 encoder on this machine")
    for tool, params in (("to_h264", {}), ("compress", {"crf": 40}), ("burn_subtitles", {"subtitle_path": str(sample_subtitle)})):
        result = run_tool(ToolRequest(tool, str(workspace_video), {**params, "gpu": True}), Progress())
        assert result.error == "" and result.output_path, (tool, result.error)
        assert result.warning == "", tool
        video = streams_of(result.output_path, "video")[0]
        assert (video["codec_name"], video["pix_fmt"], video["profile"]) == ("h264", "yuv420p", "High"), tool


@needs_ffmpeg
def test_gpu_option_on_the_page(tools_app: Path, submitted: list[ToolRequest], monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_tools as page_tools

    tools_app.mkdir(parents=True)
    for name in ("clip.mp4", "clip.zh-TW.srt"):
        (tools_app / name).write_bytes(b"x")
    monkeypatch.setattr(page_tools, "hardware_h264_encoder", lambda ffmpeg: "")
    app = run_tools_page()
    assert not [box for box in app.checkbox if box.label == page_tools.GPU_LABEL]

    monkeypatch.setattr(page_tools, "hardware_h264_encoder", lambda ffmpeg: "h264_nvenc")
    app.run()
    app.checkbox(key="convert_gpu").check()
    by_label(app.button, "轉成 H.264 MP4").click()
    app.run()
    app.checkbox(key="compress_gpu").check()
    by_label(app.button, "開始壓縮").click()
    app.run()
    app.radio(key="merge_mode").set_value("burn_subtitles")
    app.run()
    app.checkbox(key="burn_gpu").check()
    by_label(app.button, "開始處理").click()
    app.run()
    assert not app.exception, app.exception
    assert [(request.tool, request.params.get("gpu")) for request in submitted] == [("to_h264", True), ("compress", True), ("burn_subtitles", True)]

    # A target size needs libx264's two passes, so the option is not offered there.
    app.radio(key="compress_mode").set_value("target")
    app.run()
    assert not [box for box in app.checkbox if box.key == "compress_gpu"]


# 音量標準化, 合併影片 and 轉成 GIF


def loudness(path: Path) -> float:
    """The integrated loudness in LUFS, as ffmpeg's EBU R128 meter measures it."""
    report = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stderr
    return float(re.findall(r"^\s+I:\s+(-?[\d.]+) LUFS", report, re.MULTILINE)[-1])


def stream_md5(path: Path, stream: str) -> str:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", stream, "-c", "copy", "-f", "md5", "-"],
        capture_output=True, text=True, check=True,
    ).stdout


def frame_count(path: Path) -> int:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return int(probe.stdout.strip())


def quiet_input() -> list[str]:
    return [*audio_input(), "-af", "volume=0.05"]


@needs_ffmpeg
def test_loudness_normalization_keeps_the_picture_as_it_was(tmp_path: Path) -> None:
    source = make_media(tmp_path / "quiet talk.mp4", *video_input(), *quiet_input(), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac")
    assert loudness(source) < -40
    progress = Progress()
    result = run_tool(ToolRequest("loudnorm", str(source), {"loudness": -16}), progress)
    assert result.error == "" and result.output_path
    assert result.output_path.name == "quiet talk_音量標準化.mp4"
    assert abs(loudness(result.output_path) + 16) < 1
    # Copied, not compressed again.
    assert stream_md5(result.output_path, "0:v:0") == stream_md5(source, "0:v:0")
    assert progress.texts[-1] == "音量標準化完成"
    assert not leftovers(tmp_path)


@needs_ffmpeg
@pytest.mark.parametrize(("suffix", "codec"), [(".m4a", "aac"), (".mp3", "mp3"), (".opus", "opus"), (".flac", "flac"), (".webm", "opus")])
def test_loudness_normalization_keeps_each_files_format(tmp_path: Path, suffix: str, codec: str) -> None:
    source = make_media(tmp_path / f"song{suffix}", *quiet_input())
    result = run_tool(ToolRequest("loudnorm", str(source), {"loudness": -23}), Progress())
    assert result.error == "" and result.output_path and result.output_path.suffix == suffix
    assert [stream["codec_name"] for stream in streams_of(result.output_path, "audio")] == [codec]
    assert abs(loudness(result.output_path) + 23) < 1


@needs_ffmpeg
def test_loudness_normalization_keeps_the_cover_of_a_song(tmp_path: Path) -> None:
    result = run_tool(ToolRequest("loudnorm", str(cover_art_song(tmp_path)), {"loudness": -16}), Progress())
    assert result.error == "" and result.output_path
    assert [stream["disposition"]["attached_pic"] for stream in streams_of(result.output_path, "video")] == [1]


@needs_ffmpeg
def test_loudness_normalization_needs_sound(tmp_path: Path) -> None:
    source = make_media(tmp_path / "silent.mp4", *video_input(), "-c:v", "libx264", "-pix_fmt", "yuv420p")
    result = run_tool(ToolRequest("loudnorm", str(source), {"loudness": -16}), Progress())
    assert result.error == "「silent.mp4」沒有聲音，音量標準化只能用在有聲音的檔案上。"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["silent.mp4"]


@pytest.fixture
def parts(tmp_path: Path) -> list[Path]:
    """Three videos that a plain stream copy could never join: different sizes, codecs, frame and sample rates, and
    one without sound, as a screen recording may be."""
    return [
        make_media(tmp_path / "Part 1.mp4", *video_input(2, "320x240"), *audio_input(2), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"),
        make_media(
            tmp_path / "Part 2.mkv",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=200x100:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=3:sample_rate=22050",
            "-c:v", "mpeg4", "-c:a", "libmp3lame", "-ac", "1",
        ),
        make_media(tmp_path / "Part 3.mp4", *video_input(2, "120x160"), "-c:v", "libx264", "-pix_fmt", "yuv420p"),
    ]


@needs_ffmpeg
def test_joining_videos_of_every_kind(parts: list[Path]) -> None:
    progress = Progress()
    result = run_tool(ToolRequest("join", str(parts[0]), {"paths": [str(path) for path in parts]}), progress)
    assert result.error == "" and result.output_path
    assert result.output_path.name == "Part 1_合併.mp4"
    video = streams_of(result.output_path, "video")[0]
    assert (video["codec_name"], video["pix_fmt"], video["width"], video["height"]) == ("h264", "yuv420p", 320, 240)
    assert [stream["codec_name"] for stream in streams_of(result.output_path, "audio")] == ["aac"]
    duration = get_media_duration(result.output_path)
    assert duration is not None and 6.6 < duration < 7.5
    assert any(0 < value < 1 for value in progress.values) and progress.texts[-1] == "合併影片完成"
    assert not leftovers(parts[0].parent)


@needs_ffmpeg
def test_joining_follows_the_order_chosen(parts: list[Path]) -> None:
    # The first file sets the size and the name; the others are fitted inside it.
    result = run_tool(ToolRequest("join", str(parts[2]), {"paths": [str(parts[2]), str(parts[0])]}), Progress())
    assert result.error == "" and result.output_path
    assert result.output_path.name == "Part 3_合併.mp4"
    assert video_size(result.output_path) == (120, 160)
    # Sound from the second, and silence for the first, which has none.
    assert len(streams_of(result.output_path, "audio")) == 1


@needs_ffmpeg
def test_joining_videos_without_sound_makes_a_video_without_sound(parts: list[Path]) -> None:
    copy = shutil.copy(parts[2], parts[2].with_name("Part 3 again.mp4"))
    result = run_tool(ToolRequest("join", str(parts[2]), {"paths": [str(parts[2]), str(copy)]}), Progress())
    assert result.error == "" and result.output_path
    assert streams_of(result.output_path, "audio") == []


@needs_ffmpeg
def test_joining_refuses_what_it_cannot_join(parts: list[Path], tmp_path: Path) -> None:
    alone = run_tool(ToolRequest("join", str(parts[0]), {"paths": [str(parts[0])]}), Progress())
    assert alone.error == "請至少選兩個檔案來合併。"
    song = make_media(tmp_path / "song.m4a", *audio_input(2))
    with_song = run_tool(ToolRequest("join", str(parts[0]), {"paths": [str(parts[0]), str(song)]}), Progress())
    assert with_song.error == "「song.m4a」沒有影像，合併影片只能用在影片上。"
    missing = run_tool(ToolRequest("join", str(parts[0]), {"paths": [str(parts[0]), str(tmp_path / "gone.mp4")]}), Progress())
    assert missing.error == "找不到「gone.mp4」，可能已被移動或刪除。"
    assert not [path.name for path in tmp_path.iterdir() if "合併" in path.name]


@needs_ffmpeg
def test_gif_of_part_of_a_video(workspace_video: Path) -> None:
    progress = Progress()
    result = run_tool(ToolRequest("gif", str(workspace_video), {"start": 1.0, "end": 3.0, "width": 240, "fps": 10}), progress)
    assert result.error == "" and result.output_path
    assert result.output_path.name == f"{workspace_video.stem}.gif"
    gif = streams_of(result.output_path, "video")[0]
    assert (gif["codec_name"], gif["width"], gif["height"]) == ("gif", 240, 180)
    assert 19 <= frame_count(result.output_path) <= 21
    # Two passes: the palette, then the picture.
    assert any(0.4 <= value <= 0.6 for value in progress.values) and progress.texts[-1] == "轉成 GIF完成"
    assert not leftovers(workspace_video.parent)


@needs_ffmpeg
def test_gif_never_enlarges_the_picture(workspace_video: Path) -> None:
    result = run_tool(ToolRequest("gif", str(workspace_video), {"start": 0.0, "end": 1.0, "width": 640, "fps": 15}), Progress())
    assert result.error == "" and result.output_path
    assert video_size(result.output_path) == (320, 240)


@needs_ffmpeg
def test_gif_refuses_a_range_it_cannot_make(workspace_video: Path, tmp_path: Path) -> None:
    def gif(start: float, end: float) -> str:
        return run_tool(ToolRequest("gif", str(workspace_video), {"start": start, "end": end, "width": 320, "fps": 10}), Progress()).error

    assert gif(0, 61) == "GIF 最長 60 秒，請縮短時間範圍。"
    assert gif(3, 2) == "結束時間要晚於開始時間。"
    assert gif(10, 12) == "開始時間超過了影片長度（00:06）。"
    song = make_media(tmp_path / "song.m4a", *audio_input(2))
    assert run_tool(ToolRequest("gif", str(song), {"start": 0, "end": 1}), Progress()).error == "「song.m4a」沒有影像，轉成 GIF只能用在影片上。"
    assert not [path for path in tmp_path.iterdir() if path.suffix == ".gif"]


# Windows takes no ":" in a file name, so the graph's own syntax is copied without it.
HOSTILE_MEDIA_NAMES = ["a'b,c;d[e]f=g%h 中文.mp4", "clip[0][1]concat=n=9[v].mp4", "%d;negate,split.mp4"]


@needs_ffmpeg
def test_new_tools_take_hostile_names_as_plain_arguments(sample_video: Path, tmp_path: Path) -> None:
    paths = [Path(shutil.copy(sample_video, tmp_path / name)) for name in HOSTILE_MEDIA_NAMES]
    joined = run_tool(ToolRequest("join", str(paths[0]), {"paths": [str(path) for path in paths]}), Progress())
    assert joined.error == "" and joined.output_path
    duration = get_media_duration(joined.output_path)
    assert duration is not None and 17 < duration < 19
    normalized = run_tool(ToolRequest("loudnorm", str(paths[1]), {"loudness": -16}), Progress())
    assert normalized.error == "" and normalized.output_path
    gif = run_tool(ToolRequest("gif", str(paths[2]), {"start": 0, "end": 1, "width": 240, "fps": 10}), Progress())
    assert gif.error == "" and gif.output_path and gif.output_path.name == "%d;negate,split.gif"


def test_file_names_never_reach_the_new_tools_filter_graphs(tmp_path: Path) -> None:
    from videocap.media_tools import MediaInfo, build_join_command

    hostile = str(tmp_path / "x[0:v];negate,split'.mp4")
    out = tmp_path / "out.gif"
    commands = [
        build_ffmpeg_command(ToolRequest("loudnorm", hostile, {"loudness": -16}), out, "ffmpeg", MediaInfo(audio_streams=1)),
        build_ffmpeg_command(ToolRequest("gif", hostile, {"start": 1, "end": 2, "width": 320, "fps": 10}), out, "ffmpeg", MediaInfo()),
        build_join_command(
            ToolRequest("join", hostile, {"paths": [hostile, hostile]}), out, "ffmpeg", [MediaInfo(audio_streams=1, size=(320, 240))] * 2
        ),
    ]
    for command in commands:
        assert command.count(hostile) == command.count("-i") - command.count("palette.png")
        graphs = [command[index + 1] for index, part in enumerate(command) if part in ("-af", "-vf", "-filter_complex")]
        assert graphs and not [graph for graph in graphs if "negate" in graph]


@needs_ffmpeg
def test_real_gpu_join(parts: list[Path]) -> None:
    from videocap.media_tools import hardware_h264_encoder
    from videocap.tooling import get_ffmpeg_status

    if not hardware_h264_encoder(get_ffmpeg_status()[1]):
        pytest.skip("no hardware H.264 encoder on this machine")
    result = run_tool(ToolRequest("join", str(parts[0]), {"paths": [str(path) for path in parts], "gpu": True}), Progress())
    assert result.error == "" and result.output_path and result.warning == ""
    video = streams_of(result.output_path, "video")[0]
    assert (video["codec_name"], video["pix_fmt"], video["profile"]) == ("h264", "yuv420p", "High")


NEW_TOOL_TABS = {"音量標準化", "合併影片", "轉成 GIF"}


@needs_ffmpeg
@pytest.mark.parametrize(("mode", "owner"), [("local", True), ("cloud", False)])
def test_the_new_tools_are_only_for_the_owner(tools_app: Path, monkeypatch: pytest.MonkeyPatch, mode: str, owner: bool) -> None:
    monkeypatch.setenv("VIDEOCAP_MODE", mode)
    if not owner:
        monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    tools_app.mkdir(parents=True)
    (tools_app / "clip.mp4").write_bytes(b"x")
    app = run_tools_page()
    assert (NEW_TOOL_TABS <= {tab.label for tab in app.tabs}) is owner
    run_buttons = {item.key for item in app.button} & {"loudnorm_run", "join_run", "gif_run"}
    assert run_buttons == ({"loudnorm_run", "join_run", "gif_run"} if owner else set())


@needs_ffmpeg
def test_the_new_tools_send_what_was_chosen(tools_app: Path, submitted: list[ToolRequest], monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_tools as page_tools

    tools_app.mkdir(parents=True)
    for name in ("Part 1.mp4", "Part 2.mp4", "Part 3.mkv", "song.m4a", "Elsewhere/Part 4.mp4"):
        (tools_app / name).parent.mkdir(parents=True, exist_ok=True)
        (tools_app / name).write_bytes(b"x")
    monkeypatch.setattr(page_tools, "hardware_h264_encoder", lambda ffmpeg: "h264_nvenc")
    folder = tools_app.resolve()
    app = run_tools_page()
    by_label(app.selectbox, "來源檔案").set_value(str(folder / "Part 1.mp4"))
    app.run()

    by_label(app.radio, "目標響度").set_value("較安靜（-23 LUFS，廣播標準）")
    by_label(app.button, "開始標準化").click()
    app.run()

    # The videos beside the source, by name; not the song, and not a video in another folder.
    files = by_label(app.multiselect, "要合併的檔案（依選取的順序）")
    assert files.options == ["Part 1.mp4", "Part 2.mp4", "Part 3.mkv"]
    assert files.value == [str(folder / "Part 1.mp4")]
    chosen = [str(folder / name) for name in ("Part 1.mp4", "Part 3.mkv", "Part 2.mp4")]
    files.set_value(chosen)
    app.checkbox(key="join_gpu").check()
    by_label(app.button, "開始合併").click()
    app.run()

    app.text_input(key="gif_start").set_value("0:01")
    app.text_input(key="gif_end").set_value("0:04")
    app.selectbox(key="gif_width").set_value(480)
    app.selectbox(key="gif_fps").set_value(20)
    by_label(app.button, "轉成 GIF").click()
    app.run()
    assert not app.exception, app.exception
    assert [(request.tool, Path(request.input_path).name, request.params) for request in submitted] == [
        ("loudnorm", "Part 1.mp4", {"loudness": -23}),
        ("join", "Part 1.mp4", {"paths": chosen, "gpu": True}),
        ("gif", "Part 1.mp4", {"start": 1.0, "end": 4.0, "width": 480, "fps": 20}),
    ]


@needs_ffmpeg
def test_the_new_tools_explain_what_they_cannot_do_before_queueing(tools_app: Path, submitted: list[ToolRequest]) -> None:
    tools_app.mkdir(parents=True)
    for name in ("Part 1.mp4", "Part 2.mp4"):
        (tools_app / name).write_bytes(b"x")
    app = run_tools_page()
    by_label(app.button, "開始合併").click()
    app.run()
    assert "請至少選兩個檔案來合併。" in [item.value for item in app.error]

    for start, end, message in (("0:00", "1:30", "GIF 最長 60 秒，請縮短時間範圍。"), ("0:05", "0:02", "結束時間要晚於開始時間。"), ("", "", "請填入結束時間。")):
        app.text_input(key="gif_start").set_value(start)
        app.text_input(key="gif_end").set_value(end)
        by_label(app.button, "轉成 GIF").click()
        app.run()
        assert message in [item.value for item in app.error], (start, end)
    assert submitted == []


# 語音辨識字幕


def test_transcribe_is_run_by_the_speech_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.media_tools as media_tools
    import videocap.speech as speech
    from videocap.media_tools import TOOL_LABELS, MediaInfo, ToolResult

    source = tmp_path / "talk.m4a"
    source.write_bytes(b"x")
    runs: list[tuple[ToolRequest, str]] = []

    def fake_transcribe(request: ToolRequest, ffmpeg: str, progress_ui: Any, should_stop: Any = None) -> ToolResult:
        runs.append((request, ffmpeg))
        return ToolResult(output_path=source.with_name("talk.en.srt"))

    monkeypatch.setattr(media_tools, "get_ffmpeg_status", lambda: (True, "C:/tools/ffmpeg.exe"))
    monkeypatch.setattr(media_tools, "read_media_info", lambda path: MediaInfo(has_video=False, audio_streams=1))
    monkeypatch.setattr(speech, "transcribe", fake_transcribe)
    monkeypatch.chdir(tmp_path)
    result = run_tool(ToolRequest("transcribe", "talk.m4a", {"model": "tiny"}), Progress())
    assert result.output_path == source.with_name("talk.en.srt")
    # The full path, as for every other tool, and the ffmpeg that decodes the sound.
    assert runs == [(ToolRequest("transcribe", str(source.resolve()), {"model": "tiny"}), "C:/tools/ffmpeg.exe")]
    assert TOOL_LABELS["transcribe"] == "語音辨識字幕"


@needs_ffmpeg
def test_transcribe_needs_sound(tmp_path: Path) -> None:
    source = make_media(tmp_path / "silent.mp4", *video_input(), "-c:v", "libx264", "-pix_fmt", "yuv420p")
    result = run_tool(ToolRequest("transcribe", str(source), {"model": "tiny"}), Progress())
    assert result.error == "「silent.mp4」沒有聲音，語音辨識字幕只能用在有聲音的檔案上。"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["silent.mp4"]


@pytest.fixture
def speech_tab(tools_app: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The 語音辨識 tab with faster-whisper installed or not, as many CUDA devices as asked, and cuBLAS or not, without
    the package."""
    import videocap.speech as speech

    machine: dict[str, Any] = {"installed": True, "cuda": 0, "cublas": True}
    monkeypatch.setattr(speech, "is_installed", lambda: machine["installed"])
    monkeypatch.setattr(speech, "cuda_device_count", lambda: machine["cuda"])
    monkeypatch.setattr(speech, "cuda_libraries_ready", lambda: machine["cublas"])
    tools_app.mkdir(parents=True)
    for name in ("talk.mp4", "song.m4a"):
        (tools_app / name).write_bytes(b"x")
    return machine


@needs_ffmpeg
def test_speech_tab_sends_the_choices(speech_tab: dict[str, Any], submitted: list[ToolRequest], tools_app: Path) -> None:
    app = run_tools_page()
    assert "語音辨識" in [tab.label for tab in app.tabs]
    model = app.selectbox(key="speech_model")
    assert model.value == "small" and model.options == [
        "tiny（約 75 MB）", "base（約 145 MB）", "small（約 480 MB，建議）", "medium（約 1.5 GB）", "large-v3-turbo（約 1.6 GB）", "large-v3（約 3 GB）"
    ]
    assert app.checkbox(key="speech_traditional").value is True
    # No CUDA device, so no GPU option.
    assert not [box for box in app.checkbox if box.key == "speech_gpu"]
    by_label(app.selectbox, "來源檔案").set_value(str(tools_app.resolve() / "talk.mp4"))
    app.run()
    by_label(app.button, "開始辨識").click()
    app.run()

    # Audio files too.
    by_label(app.selectbox, "來源檔案").set_value(str(tools_app.resolve() / "song.m4a"))
    app.selectbox(key="speech_model").set_value("tiny")
    app.selectbox(key="speech_language").set_value("zh")
    app.run()
    app.checkbox(key="speech_traditional").uncheck()
    by_label(app.button, "開始辨識").click()
    app.run()

    # English needs no conversion, so the box goes away.
    app.selectbox(key="speech_language").set_value("en")
    app.run()
    assert not [box for box in app.checkbox if box.key == "speech_traditional"]
    by_label(app.button, "開始辨識").click()
    app.run()
    assert not app.exception, app.exception
    assert [(request.tool, Path(request.input_path).name, request.params) for request in submitted] == [
        ("transcribe", "talk.mp4", {"model": "small", "language": "auto", "traditional": True}),
        ("transcribe", "song.m4a", {"model": "tiny", "language": "zh", "traditional": False}),
        ("transcribe", "song.m4a", {"model": "tiny", "language": "en"}),
    ]


@needs_ffmpeg
def test_speech_tab_says_where_the_result_goes_and_what_the_first_run_downloads(speech_tab: dict[str, Any]) -> None:
    shown = " ".join(caption.value for caption in run_tools_page().caption)
    assert "工具結果" in shown and "媒體庫" in shown
    assert "第一次" in shown and "約 480 MB" in shown


@needs_ffmpeg
def test_with_a_usable_gpu_the_speech_tab_recommends_turbo_on_it(speech_tab: dict[str, Any], submitted: list[ToolRequest]) -> None:
    speech_tab["cuda"] = 1
    app = run_tools_page()
    model = app.selectbox(key="speech_model")
    assert model.value == "large-v3-turbo" and "large-v3-turbo（約 1.6 GB，建議）" in model.options
    assert app.checkbox(key="speech_gpu").value is True
    by_label(app.button, "開始辨識").click()
    app.run()
    app.checkbox(key="speech_gpu").uncheck()
    by_label(app.button, "開始辨識").click()
    app.run()
    assert not app.exception, app.exception
    assert [request.params for request in submitted] == [
        {"model": "large-v3-turbo", "language": "auto", "traditional": True, "gpu": True},
        {"model": "large-v3-turbo", "language": "auto", "traditional": True},
    ]


@needs_ffmpeg
def test_a_gpu_without_cublas_gets_the_install_hint_instead(speech_tab: dict[str, Any]) -> None:
    from videocap.speech import GPU_INSTALL_BUTTON_LABEL

    speech_tab["cuda"] = 1
    speech_tab["cublas"] = False
    app = run_tools_page()
    assert not [box for box in app.checkbox if box.key == "speech_gpu"]
    assert app.selectbox(key="speech_model").value == "small"
    assert any(GPU_INSTALL_BUTTON_LABEL in caption.value and "系統" in caption.value for caption in app.caption)


@needs_ffmpeg
def test_speech_tab_without_faster_whisper_points_at_the_install_button(speech_tab: dict[str, Any]) -> None:
    from videocap.speech import INSTALL_BUTTON_LABEL

    speech_tab["installed"] = False
    app = run_tools_page()
    assert any(INSTALL_BUTTON_LABEL in item.value and "系統" in item.value for item in app.info)
    assert "開始辨識" not in [item.label for item in app.button]
    assert not [box for box in app.selectbox if box.key == "speech_model"]


def test_visitors_get_no_speech_tab(speech_tab: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.speech as speech

    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    monkeypatch.setattr(speech, "is_installed", lambda: pytest.fail("a visitor's page looked for faster-whisper"))
    app = run_tools_page()
    assert "語音辨識" not in [tab.label for tab in app.tabs]
    assert "開始辨識" not in [item.label for item in app.button]
