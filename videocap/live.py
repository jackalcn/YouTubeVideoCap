from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from io import BufferedReader
from typing import IO, Any, Callable, Mapping, Optional, Protocol, cast

from videocap.config import LIVE_MIN_FREE_BYTES, RECORDING_STATUS_PREFIX
from videocap.media_tools import reserve_output_path
from videocap.processes import end_process_tree, start_tied_process
from videocap.tooling import format_bytes, format_duration, get_free_disk_bytes


QUIT_WAIT_SECONDS = 15
POLL_SECONDS = 0.5
DISK_CHECK_SECONDS = 30
REMUX_TIMEOUT_SECONDS = 600
COPY_CHUNK_BYTES = 256 * 1024
OUT_TIME_RE = re.compile(r"^out_time_ms=(\d+)")
PROGRESS_LINE_RE = re.compile(r"^[a-z_0-9]+=\S*$")
# Live audio arrives in a form MP4 cannot hold without a filter, and an MP4 cut short has no index. MPEG-TS needs
# neither, so what was recorded stays playable whatever ends the recording.
RECORDING_SUFFIX = ".recording.ts"
UNSUPPORTED_PROXY_MESSAGE = "直播錄製只能使用 http:// 開頭的代理伺服器。為了不繞過你設定的代理直接連線，這場直播沒有錄製。"


class RecordingProgress(Protocol):
    def progress(self, value: float, text: str = "") -> None: ...


@dataclass(frozen=True)
class RecordingResult:
    path: Optional[Path] = None
    problem: str = ""


def build_recording_command(ffmpeg: str, sources: list[Mapping[str, Any]], max_seconds: Optional[int] = None) -> list[str]:
    # The recording goes to stdout, and progress to stderr. If the app dies, the broken pipe stops ffmpeg; writing
    # to a file instead would leave it recording into the disk with nothing left to stop it.
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:2"]
    for source in sources:
        headers = source.get("http_headers") or {}
        if headers:
            command += ["-headers", "".join(f"{name}: {value}\r\n" for name, value in headers.items())]
        command += [*((source.get("downloader_options") or {}).get("ffmpeg_args") or []), "-i", str(source["url"])]
    command += ["-c", "copy"]
    if len(sources) > 1:
        # Separate video and audio streams; a single source already holds both.
        for index, source in enumerate(sources):
            command += ["-map", f"{index}:{source.get('manifest_stream_number') or 0}"]
    if max_seconds:
        command += ["-t", str(max_seconds)]
    return [*command, "-f", "mpegts", "pipe:1"]


def build_remux_command(ffmpeg: str, recording_path: Path, output_path: Path) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
        "-i", str(recording_path),
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(output_path),
    ]  # fmt: skip


def finish_recording(ffmpeg: str, recording_path: Path, output_path: Path) -> Path:
    """Turns the recording into the requested file. If that fails the recording itself is kept, because it still plays."""
    if output_path.suffix.lower() == ".ts":
        return recording_path.replace(output_path)
    try:
        # A long recording takes minutes to remux, and that ffmpeg must not outlive the app either.
        process = start_tied_process(
            build_remux_command(ffmpeg, recording_path, output_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError:
        remove_if_empty(output_path)
        return recording_path
    try:
        returncode = process.wait(timeout=REMUX_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        end_process_tree(process)
        process.wait()
        remove_if_empty(output_path)
        return recording_path
    if returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        remove_if_empty(output_path)
        return recording_path
    remove_quietly(recording_path)
    return output_path


def remove_quietly(path: Path) -> None:
    # A file locked by a player must not turn a finished recording into a failed job.
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def remove_if_empty(path: Path) -> None:
    """Removes a name reserved for a file that never got written, and nothing else."""
    try:
        if path.is_file() and path.stat().st_size == 0:
            path.unlink()
    except OSError:
        pass


def record_live_stream(
    sources: list[Mapping[str, Any]],
    output_path: Path,
    ffmpeg: str,
    progress_ui: RecordingProgress,
    should_stop: Optional[Callable[[], bool]] = None,
    max_seconds: Optional[int] = None,
    proxy: str = "",
) -> RecordingResult:
    """Records until the stream ends or someone stops it, and keeps what was recorded.

    yt-dlp hands live streams to an ffmpeg process of its own and blocks without calling back, which leaves no way to
    stop it. Running ffmpeg here allows asking it to quit, so it can finish the file properly.
    """
    if proxy and not proxy.lower().startswith("http://"):
        # ffmpeg only knows http:// proxies, and would otherwise connect directly, past the proxy the person set.
        remove_if_empty(output_path)
        return RecordingResult(problem=UNSUPPORTED_PROXY_MESSAGE)
    # Reserved, because a recording left over from a crash has the same name and must not be overwritten.
    recording_path = reserve_output_path(output_path.with_name(f"{output_path.stem}{RECORDING_SUFFIX}"))
    environment = {**os.environ, "HTTP_PROXY": proxy, "http_proxy": proxy} if proxy else None
    process = start_tied_process(
        build_recording_command(ffmpeg, sources, max_seconds),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    state: dict[str, float] = {"seconds": 0.0, "bytes": 0.0}
    problems: deque[str] = deque(maxlen=10)

    def copy_recording(stream: IO[bytes]) -> None:
        with recording_path.open("wb") as target:
            # read1 hands over whatever has arrived, so a slow stream reaches the disk at once rather than in large blocks.
            while chunk := cast(BufferedReader, stream).read1(COPY_CHUNK_BYTES):
                target.write(chunk)
                target.flush()
                state["bytes"] += len(chunk)

    def read_status(stream: IO[bytes]) -> None:
        for raw_line in stream:
            line = raw_line.decode("utf-8", "replace").strip()
            time_match = OUT_TIME_RE.match(line)
            if time_match:
                state["seconds"] = int(time_match.group(1)) / 1_000_000
            elif line and not PROGRESS_LINE_RE.match(line):
                problems.append(line)

    assert process.stdout is not None and process.stderr is not None
    # Each pipe on its own thread: a stalled stream produces no output, and stopping must not wait for it.
    readers = [
        threading.Thread(target=copy_recording, args=(process.stdout,), daemon=True),
        threading.Thread(target=read_status, args=(process.stderr,), daemon=True),
    ]
    for reader in readers:
        reader.start()

    low_disk = False
    asked_to_stop = False
    last_disk_check = time.monotonic()
    while process.poll() is None:
        now = time.monotonic()
        if now - last_disk_check >= DISK_CHECK_SECONDS:
            last_disk_check = now
            free = get_free_disk_bytes(output_path.parent)
            low_disk = free is not None and free < LIVE_MIN_FREE_BYTES
        if low_disk or (should_stop and should_stop()):
            asked_to_stop = True
            progress_ui.progress(0.0, text="正在結束錄製...")
            ask_ffmpeg_to_quit(process)
            break
        progress_ui.progress(
            0.0,
            text=f"{RECORDING_STATUS_PREFIX} {format_duration(state['seconds'])} · {format_bytes(state['bytes'])}",
        )
        time.sleep(POLL_SECONDS)

    process.wait()
    for reader in readers:
        reader.join(timeout=10)

    failed = not asked_to_stop and process.returncode != 0
    failure = f"直播錄製中斷：{' '.join(problems)[-300:]}" if failed else ""
    if not recording_path.exists() or recording_path.stat().st_size == 0:
        remove_quietly(recording_path)
        remove_if_empty(output_path)
        return RecordingResult(problem=failure or "直播錄製失敗：沒有收到任何畫面。")

    progress_ui.progress(0.0, text="正在整理錄影檔...")
    final_path = finish_recording(ffmpeg, recording_path, output_path)
    progress_ui.progress(1.0, text=f"錄製完成，共 {format_duration(state['seconds'])}")
    if low_disk:
        return RecordingResult(final_path, f"硬碟空間不足，已提前結束錄製。已錄製的 {format_duration(state['seconds'])} 有保留。")
    return RecordingResult(final_path, failure)


def ask_ffmpeg_to_quit(process: subprocess.Popen[bytes]) -> None:
    try:
        assert process.stdin is not None
        process.stdin.write(b"q\n")
        process.stdin.flush()
        process.wait(timeout=QUIT_WAIT_SECONDS)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        end_process_tree(process)
