from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import needs_ffmpeg
from videocap.library import get_media_duration, probe_media
from videocap.live import build_recording_command, record_live_stream


class Progress:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def progress(self, value: float, text: str = "") -> None:
        self.texts.append(text)


def fake_live_source(video: Path, endless: bool = True) -> dict[str, object]:
    """A local stand-in for a live stream: H.264 and AAC delivered in real time, endless until stopped."""
    arguments = ["-re", "-stream_loop", "-1"] if endless else ["-re"]
    return {"url": str(video), "downloader_options": {"ffmpeg_args": arguments}}


def stop_after(seconds: float) -> threading.Event:
    event = threading.Event()
    threading.Timer(seconds, event.set).start()
    return event


def test_separate_streams_are_mapped_one_each() -> None:
    sources = [{"url": "http://v", "http_headers": {"User-Agent": "x"}}, {"url": "http://a", "manifest_stream_number": 2}]
    command = build_recording_command("ffmpeg", sources, 60)
    assert command[command.index("-headers") + 1] == "User-Agent: x\r\n"
    maps = [command[index + 1] for index, part in enumerate(command) if part == "-map"]
    assert maps == ["0:0", "1:2"]
    assert command[command.index("-t") + 1] == "60"
    # Written to a pipe, so ffmpeg stops when the app does.
    assert command[-3:] == ["-f", "mpegts", "pipe:1"]


def test_single_source_needs_no_mapping() -> None:
    assert "-map" not in build_recording_command("ffmpeg", [{"url": "http://v"}])


@needs_ffmpeg
def test_stopping_keeps_a_playable_file(tmp_path: Path, sample_video: Path) -> None:
    progress = Progress()
    stopped = stop_after(3.0)
    started = time.time()
    result = record_live_stream([fake_live_source(sample_video)], tmp_path / "live.mp4", "ffmpeg", progress, stopped.is_set)

    assert time.time() - started < 30
    assert result.problem == ""
    assert result.path == tmp_path / "live.mp4"
    probe = probe_media(result.path)
    assert probe is not None
    assert {stream["codec_type"] for stream in probe["streams"]} >= {"video", "audio"}
    duration = get_media_duration(result.path)
    assert duration is not None and duration > 1.0
    assert not list(tmp_path.glob("*.ts"))
    assert any(text.startswith("錄製中") for text in progress.texts)


@needs_ffmpeg
def test_recording_and_remuxing_are_both_tied_to_the_app(tmp_path: Path, sample_video: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.live as live_module
    from videocap.processes import start_tied_process

    started: list[list[str]] = []

    def noting_tied_process(command: list[str], **options: object) -> subprocess.Popen[bytes]:
        started.append(command)
        return start_tied_process(command, **options)

    monkeypatch.setattr(live_module, "start_tied_process", noting_tied_process)
    result = record_live_stream([fake_live_source(sample_video, endless=False)], tmp_path / "live.mp4", "ffmpeg", Progress())
    assert result.problem == ""
    assert result.path == tmp_path / "live.mp4"
    # Killed from Task Manager, the app would otherwise leave either ffmpeg running on its own.
    assert [command[-1] for command in started] == ["pipe:1", str(tmp_path / "live.mp4")]


@needs_ffmpeg
def test_stream_ending_by_itself_is_a_success(tmp_path: Path, sample_video: Path) -> None:
    result = record_live_stream([fake_live_source(sample_video, endless=False)], tmp_path / "live.mp4", "ffmpeg", Progress())
    assert result.problem == ""
    assert result.path and result.path.exists()


@needs_ffmpeg
def test_length_limit_ends_the_recording(tmp_path: Path, sample_video: Path) -> None:
    started = time.time()
    result = record_live_stream([fake_live_source(sample_video)], tmp_path / "live.mp4", "ffmpeg", Progress(), max_seconds=2)
    assert time.time() - started < 30
    assert result.path
    duration = get_media_duration(result.path)
    assert duration is not None and duration < 5


@needs_ffmpeg
def test_failure_is_reported_instead_of_a_silent_empty_file(tmp_path: Path) -> None:
    source = {"url": str(tmp_path / "missing.m3u8")}
    result = record_live_stream([source], tmp_path / "live.mp4", "ffmpeg", Progress())
    assert result.path is None
    assert "直播錄製" in result.problem
    assert list(tmp_path.iterdir()) == []


@needs_ffmpeg
def test_keeps_the_raw_recording_when_the_final_file_cannot_be_made(tmp_path: Path, sample_video: Path) -> None:
    # A target path that is a folder makes the remux fail after a successful recording.
    target = tmp_path / "live.mp4"
    target.mkdir()
    stopped = stop_after(2.0)
    result = record_live_stream([fake_live_source(sample_video)], target, "ffmpeg", Progress(), stopped.is_set)
    assert result.path == tmp_path / "live.recording.ts"
    assert result.path.stat().st_size > 0


@needs_ffmpeg
def test_a_leftover_recording_is_never_overwritten(tmp_path: Path, sample_video: Path) -> None:
    leftover = tmp_path / "live.recording.ts"
    leftover.write_bytes(b"recorded before a crash")
    target = tmp_path / "live.mp4"
    target.mkdir()  # Forces the raw recording to be kept, so its name is visible.
    stopped = stop_after(2.0)
    result = record_live_stream([fake_live_source(sample_video)], target, "ffmpeg", Progress(), stopped.is_set)
    assert result.path == tmp_path / "live.recording (1).ts"
    assert leftover.read_bytes() == b"recorded before a crash"


RECORDER_SCRIPT = """
import subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from videocap.live import record_live_stream

real_popen = subprocess.Popen

def popen_noting_the_pid(*args, **kwargs):
    process = real_popen(*args, **kwargs)
    Path(sys.argv[4]).write_text(str(process.pid))
    return process

subprocess.Popen = popen_noting_the_pid

class Progress:
    def progress(self, value, text=""):
        pass

source = {"url": sys.argv[2], "downloader_options": {"ffmpeg_args": ["-re", "-stream_loop", "-1"]}}
record_live_stream([source], Path(sys.argv[3]), "ffmpeg", Progress())
"""


def process_is_running(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        synchronize, wait_timeout = 0x00100000, 0x102
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@needs_ffmpeg
def test_recording_stops_when_the_app_is_killed(tmp_path: Path, sample_video: Path) -> None:
    script = tmp_path / "recorder.py"
    script.write_text(RECORDER_SCRIPT, encoding="utf-8")
    recording = tmp_path / "live.recording.ts"
    pid_file = tmp_path / "ffmpeg.pid"
    repo = str(Path(__file__).resolve().parents[1])
    app = subprocess.Popen([sys.executable, str(script), repo, str(sample_video), str(tmp_path / "live.mp4"), str(pid_file)])
    try:
        # What arrives reaches the disk at once, not in large blocks that a slow stream takes many seconds to fill.
        deadline = time.time() + 8
        while time.time() < deadline and not (recording.exists() and recording.stat().st_size > 0):
            time.sleep(0.2)
        assert recording.exists() and recording.stat().st_size > 0
        ffmpeg_pid = int(pid_file.read_text())
        assert process_is_running(ffmpeg_pid)
    finally:
        app.kill()
        app.wait()

    deadline = time.time() + 10
    while time.time() < deadline and process_is_running(ffmpeg_pid):
        time.sleep(0.2)
    assert not process_is_running(ffmpeg_pid)


def test_a_proxy_ffmpeg_cannot_use_stops_the_recording_instead_of_bypassing_it(tmp_path: Path) -> None:
    output = tmp_path / "live.mp4"
    output.touch()  # Reserved by the caller.
    result = record_live_stream([{"url": "http://live"}], output, "ffmpeg", Progress(), proxy="socks5://127.0.0.1:1080")
    assert result.path is None
    assert "代理" in result.problem
    assert list(tmp_path.iterdir()) == []
