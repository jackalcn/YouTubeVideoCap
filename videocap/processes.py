"""Starts helper programs such as ffmpeg so they end with the app. Killed from Task Manager, a terminal or a crash,
the app would otherwise leave ffmpeg encoding on its own, untracked by the app that starts next."""
from __future__ import annotations

import ctypes
import os
import signal
import shutil
import subprocess
import sys
import time
import threading
from pathlib import Path
from functools import lru_cache
from typing import Any, Callable, Optional


# Windows: every process in the job ends when its last handle closes, and the app's handle closes when the app ends.
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
# Linux, from <linux/prctl.h>.
PR_SET_PDEATHSIG = 1

_job_lock = threading.Lock()
_job_handle: Optional[int] = None


def start_tied_process(command: list[str], **options: Any) -> subprocess.Popen[Any]:
    """subprocess.Popen, but the process is killed when the app ends, however it ends. Best effort: when the system
    refuses, the process still runs, as it would without this. macOS has no such mechanism, so there it is plain Popen.

    On Linux the signal comes when the thread that started the process ends, so start it from a thread that waits
    for it, as the job threads do."""
    process = subprocess.Popen(command, **tied_popen_options(options))
    tie_to_app(process)
    return process


def tied_popen_options(options: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """For a process another library starts with its own Popen call: the options to start it with, a copy of the
    given ones. Call tie_to_app on the process once it has started."""
    options = dict(options or {})
    if sys.platform.startswith("linux") and "preexec_fn" not in options:
        options["preexec_fn"] = _parent_death_signal(os.getpid())
    return options


def tie_to_app(process: subprocess.Popen[Any]) -> None:
    """The part of tying a process that can only happen after it has started; started with tied_popen_options."""
    if sys.platform == "win32":
        _add_to_app_job(process.pid)


def _parent_death_signal(parent_pid: int) -> Optional[Callable[[], None]]:
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None
    # Looked up before the fork: the child should do as little as possible before it runs the program.
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    kill_signal = int(signal.SIGKILL)

    def die_with_parent() -> None:
        prctl(PR_SET_PDEATHSIG, kill_signal, 0, 0, 0)
        # The app may have ended between the fork and the line above, and then no signal would ever come.
        if os.getppid() != parent_pid:
            os._exit(1)

    return die_with_parent


def _add_to_app_job(pid: int) -> None:
    job = _app_job()
    if job is None:
        return
    kernel32 = _kernel32()
    process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not process:
        return
    try:
        kernel32.AssignProcessToJobObject(job, process)
    finally:
        kernel32.CloseHandle(process)


@lru_cache(maxsize=1)
def _kernel32() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    handle = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [handle, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = handle
    kernel32.SetInformationJobObject.argtypes = [handle, ctypes.c_int, handle, ctypes.c_uint32]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = handle
    kernel32.AssignProcessToJobObject.argtypes = [handle, handle]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [handle]
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _app_job() -> Optional[int]:
    """One job for the whole app. Its handle is never closed, so only the app's end closes it."""
    global _job_handle
    with _job_lock:
        if _job_handle is not None:
            return _job_handle
        kernel32 = _kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel32.CloseHandle(job)
            return None
        _job_handle = job
        return job


def end_process_tree(process: subprocess.Popen[Any], timeout: float = 10.0) -> None:
    """Ends a child and everything it started. ffmpeg installed through a package manager is often a small launcher
    (Chocolatey's shim, for one) whose real ffmpeg is a child of its own: killing only the launcher would leave that
    ffmpeg writing its file. The caller waits for the process afterwards."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        process.kill()
    except OSError:
        pass


def remove_when_released(path: Path, timeout: float = 3.0) -> bool:
    """Deletes a file or folder a process that was just killed may still hold. Windows keeps the handle open for a
    moment after the process ends, and a delete in that moment fails with "being used by another process"."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            return True
        except PermissionError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        except OSError:
            return False
