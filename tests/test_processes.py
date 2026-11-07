from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Starts a long-running program the way the app starts ffmpeg, prints both process ids, and waits to be killed.
APP_STANDIN = """
import os, subprocess, sys, time
from videocap.processes import start_tied_process, tie_to_app, tied_popen_options

command = [sys.executable, "-c", "import time; time.sleep(120)"]
if sys.argv[1] == "tied":
    long_job = start_tied_process(command)
elif sys.argv[1] == "started elsewhere":
    # The way yt-dlp starts ffmpeg: its own Popen call, with the app's options and a tie afterwards.
    long_job = subprocess.Popen(command, **tied_popen_options())
    tie_to_app(long_job)
else:
    long_job = subprocess.Popen(command)
print(os.getpid(), long_job.pid, flush=True)
time.sleep(120)
"""


def is_running(pid: int) -> bool:
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return code.value == 259  # STILL_ACTIVE
    try:
        # A killed process waits as a zombie until whoever adopted it collects it.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True


def kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
    except OSError:
        pass


def start_app_and_kill_it(script: Path, mode: str) -> int:
    """Returns the id of the long-running program, after the process that started it was killed."""
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
    # The interpreter itself, not a venv's launcher: on Windows the launcher holds its own kill-on-close job, which
    # would end the long-running program whether or not the helper works. The stand-in only needs the standard library.
    python = getattr(sys, "_base_executable", sys.executable)
    app = subprocess.Popen([python, str(script), mode], stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert app.stdout is not None
        app_pid, long_job_pid = (int(value) for value in app.stdout.readline().split())
        assert is_running(long_job_pid)
        kill(app_pid)
        app.wait(timeout=30)
    finally:
        app.kill()
    return long_job_pid


def wait_until_gone(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not is_running(pid):
            return True
        time.sleep(0.1)
    return False


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS has no way to tie a child to its parent")
@pytest.mark.parametrize("mode", ["tied", "started elsewhere"])
def test_a_tied_process_ends_with_the_app(tmp_path: Path, mode: str) -> None:
    script = tmp_path / "app_standin.py"
    script.write_text(APP_STANDIN, encoding="utf-8")
    long_job = start_app_and_kill_it(script, mode)
    try:
        assert wait_until_gone(long_job)
    finally:
        kill(long_job)


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS has no way to tie a child to its parent")
def test_an_untied_process_outlives_the_app(tmp_path: Path) -> None:
    # Shows the check above can fail: plain Popen leaves the program running.
    script = tmp_path / "app_standin.py"
    script.write_text(APP_STANDIN, encoding="utf-8")
    long_job = start_app_and_kill_it(script, "plain")
    try:
        assert not wait_until_gone(long_job, seconds=1.5)
    finally:
        kill(long_job)


def test_a_tied_process_works_like_popen() -> None:
    from videocap.processes import start_tied_process

    process = start_tied_process([sys.executable, "-c", "print('hello')"], stdout=subprocess.PIPE, text=True)
    output, _ = process.communicate(timeout=30)
    assert process.returncode == 0
    assert output.strip() == "hello"


def test_tied_options_keep_the_callers_own() -> None:
    from videocap.processes import tied_popen_options

    given = {"stdout": subprocess.PIPE}
    options = tied_popen_options(given)
    assert options["stdout"] == subprocess.PIPE and given == {"stdout": subprocess.PIPE}
    assert ("preexec_fn" in options) is sys.platform.startswith("linux")
    # A caller's own preexec_fn is never replaced.
    assert tied_popen_options({"preexec_fn": print})["preexec_fn"] is print


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_ffmpeg_runs_yt_dlp_starts_join_the_apps_job() -> None:
    import videocap.processes as processes_module
    from videocap.downloader import TrackedPopen

    process = TrackedPopen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        in_job = ctypes.c_int()
        assert kernel32.IsProcessInJob(ctypes.c_void_p(int(process._handle)), ctypes.c_void_p(processes_module._job_handle), ctypes.byref(in_job))  # type: ignore[attr-defined]
        assert in_job.value == 1
    finally:
        process.kill()
        process.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "win32", reason="package-manager launchers are a Windows habit")
def test_ending_a_process_also_ends_what_it_started(tmp_path: Path) -> None:
    import subprocess
    import time

    from videocap.processes import end_process_tree

    # cmd stands in for a launcher such as Chocolatey's ffmpeg shim: the real work runs in its child.
    pid_file = tmp_path / "pid.txt"
    script = tmp_path / "child.py"
    # A file, because cmd would mangle quotes and semicolons in a -c argument.
    lines = ["import os, time", f"open(r'{pid_file}', 'w').write(str(os.getpid()))", "time.sleep(60)", ""]
    script.write_text("\n".join(lines), encoding="utf-8")
    launcher = subprocess.Popen(["cmd", "/d", "/c", sys.executable, str(script)], creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 15
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        child_pid = int(pid_file.read_text())
        end_process_tree(launcher)
        launcher.wait(timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            listing = subprocess.run(["tasklist", "/FI", f"PID eq {child_pid}", "/NH"], capture_output=True, text=True, check=False).stdout
            if str(child_pid) not in listing:
                break
            time.sleep(0.2)
        assert str(child_pid) not in listing
    finally:
        if launcher.poll() is None:
            launcher.kill()
