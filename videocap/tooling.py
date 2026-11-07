from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from yt_dlp.version import __version__ as YT_DLP_VERSION

from videocap.config import LOCAL_RUN_GUIDE_URL

try:
    from yt_dlp.utils._jsruntime import BunJsRuntime, DenoJsRuntime, NodeJsRuntime

    # yt-dlp's own checks, because it ignores a runtime older than it supports, such as Node 18 or 20.
    YTDLP_JS_RUNTIMES: dict[str, type] = {"deno": DenoJsRuntime, "node": NodeJsRuntime, "bun": BunJsRuntime}
except ImportError:
    # A private module of yt-dlp, which a later release may move; then any working runtime counts.
    YTDLP_JS_RUNTIMES = {}


# yt-dlp checks these in order, and each is installed differently.
JS_RUNTIMES = (("deno", "--version"), ("node", "--version"), ("bun", "--version"))
PYPI_YT_DLP_URL = "https://pypi.org/pypi/yt-dlp/json"
YT_DLP_INSTALL_TARGET = "yt-dlp[default,deno]"
# The same as requirements-speech.txt, which the Docker image does not carry.
SPEECH_INSTALL_TARGET = "faster-whisper>=1.1"
# What CTranslate2 needs to run Whisper on an NVIDIA card: cuBLAS for CUDA 12. Its Windows and Linux builds call no cuDNN.
GPU_INSTALL_TARGETS = ("nvidia-cublas-cu12",)
PIP_BUSY_MESSAGE = "另一個安裝或更新還在進行，請等它完成後再試一次。"
GITHUB_API = "https://api.github.com"
# GitHub refuses API requests without one.
APP_USER_AGENT = "YouTube-VideoCap"
# A hidden copy started at sign-in must not flash a console window for every tool it runs.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def probe_executable(name: str, version_flag: str, search_path: Optional[str] = None) -> tuple[bool, str]:
    executable = shutil.which(name, path=search_path)
    if not executable:
        return False, f"PATH 中找不到 {name}"

    try:
        result = subprocess.run(
            [executable, version_flag],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            creationflags=NO_WINDOW,
        )
    except Exception as exc:
        return False, f"{name} 無法執行：{exc}"

    if result.returncode != 0:
        stderr_text = (result.stderr or "").strip()
        stdout_text = (result.stdout or "").strip()
        details = stderr_text or stdout_text or "unknown error"
        return False, f"{name} 無法使用：{details}"

    return True, executable


@lru_cache(maxsize=1)
def get_ffmpeg_status() -> tuple[bool, str]:
    return probe_executable("ffmpeg", "-version")


@lru_cache(maxsize=1)
def get_ffprobe_status() -> tuple[bool, str]:
    return probe_executable("ffprobe", "-version")


@lru_cache(maxsize=1)
def get_js_runtime_status() -> tuple[bool, str]:
    # yt-dlp checks Python's scripts directory before PATH, so a pip-installed Deno works without an activated venv.
    search_path = os.pathsep.join([sysconfig.get_path("scripts"), os.environ.get("PATH", "")])
    problems: list[str] = []
    for name, flag in JS_RUNTIMES:
        runtime = YTDLP_JS_RUNTIMES.get(name)
        if runtime is None:
            available, status = probe_executable(name, flag, search_path)
            if available:
                return True, status
            problems.append(status)
            continue
        info = runtime().info
        if info is None:
            problems.append(f"找不到 {name}")
        elif not info.supported:
            minimum = ".".join(str(part) for part in runtime.MIN_SUPPORTED_VERSION)
            problems.append(f"{name} {info.version} 版本太舊，yt-dlp 需要 {minimum} 以上")
        else:
            return True, shutil.which(info.path) or info.path
    return False, "；".join(problems)


def reset_tool_status() -> None:
    get_ffmpeg_status.cache_clear()
    get_ffprobe_status.cache_clear()
    get_js_runtime_status.cache_clear()


def read_tool_version(path: str, flag: str) -> str:
    try:
        result = subprocess.run([path, flag], capture_output=True, text=True, check=False, timeout=5, creationflags=NO_WINDOW)
    except Exception:
        return ""
    first_line = (result.stdout or result.stderr or "").strip().splitlines()
    return first_line[0][:120] if first_line else ""


def days_since_ytdlp_release(version: str = YT_DLP_VERSION, today: Optional[date] = None) -> Optional[int]:
    """yt-dlp versions are release dates, such as 2026.10.26."""
    parts = version.split(".")
    try:
        released = date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return None
    return ((today or date.today()) - released).days


def parse_version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


@dataclass(frozen=True)
class UpdateCheck:
    current: str
    latest: str
    error: str = ""

    @property
    def has_update(self) -> bool:
        return bool(self.latest) and parse_version_key(self.latest) > parse_version_key(self.current)


def check_ytdlp_update(timeout: float = 10, proxy: str = "") -> UpdateCheck:
    # Without a proxy setting, build_opener still uses the system's proxy, as urlopen does.
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    try:
        request = urllib.request.Request(PYPI_YT_DLP_URL, headers={"User-Agent": "youtube-videocap"})
        with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response:
            latest = str(json.load(response)["info"]["version"])
    except Exception as exc:
        return UpdateCheck(YT_DLP_VERSION, "", f"無法查詢最新版本：{exc}")
    return UpdateCheck(YT_DLP_VERSION, latest)


def installed_ytdlp_version(timeout: float = 60) -> str:
    """The version on disk, read in a fresh interpreter because this process keeps the copy it imported."""
    command = [sys.executable, "-c", "import yt_dlp.version; print(yt_dlp.version.__version__)"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout, creationflags=NO_WINDOW)
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


@dataclass(frozen=True)
class UpdateResult:
    succeeded: bool
    message: str
    # The yt-dlp on disk is no longer the one this process runs.
    restart_required: bool = False


_pip_lock = threading.Lock()


def run_pip_install(arguments: list[str], timeout: float, proxy: str = "") -> tuple[Optional[int], str]:
    """pip install into the running interpreter's environment, one at a time: two at once can leave it half
    installed. Returns pip's exit code and the end of its output, or None and why pip did not run."""
    command = [sys.executable, "-m", "pip", "install", *arguments]
    if proxy:
        command[4:4] = ["--proxy", proxy]
    if not _pip_lock.acquire(blocking=False):
        return None, PIP_BUSY_MESSAGE
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout, creationflags=NO_WINDOW)
    except Exception as exc:
        return None, str(exc)
    finally:
        _pip_lock.release()
    return result.returncode, (result.stdout + result.stderr).strip()[-2000:]


def refresh_import_paths() -> None:
    """Lets the running app import what pip just installed. A user install, as in Docker, may have created the user
    site-packages folder, which Python only puts on sys.path at start when it already exists."""
    user_site = site.getusersitepackages() if site.ENABLE_USER_SITE else ""
    if user_site and os.path.isdir(user_site) and user_site not in sys.path:
        site.addsitedir(user_site)
    importlib.invalidate_caches()


def install_speech_recognition(timeout: float = 900, proxy: str = "") -> UpdateResult:
    """Installs faster-whisper for 語音辨識字幕. Without --upgrade pip keeps every package that already fits, so what
    the app has imported stays as it is and no restart is needed."""
    code, output = run_pip_install([SPEECH_INSTALL_TARGET], timeout, proxy)
    if code is None:
        return UpdateResult(False, output if output == PIP_BUSY_MESSAGE else f"安裝失敗：{output}")
    if code != 0:
        return UpdateResult(False, output)
    refresh_import_paths()
    # Installed somewhere this process does not look, so only a restart finds it.
    return UpdateResult(True, output, restart_required=importlib.util.find_spec("faster_whisper") is None)


def install_gpu_support(timeout: float = 1800, proxy: str = "") -> UpdateResult:
    """Installs cuBLAS for 語音辨識字幕 on an NVIDIA card, about 650 MB. speech.cuda_libraries_ready loads it from
    there, so whether it works shows only once its answer is cleared."""
    code, output = run_pip_install(list(GPU_INSTALL_TARGETS), timeout, proxy)
    if code is None:
        return UpdateResult(False, output if output == PIP_BUSY_MESSAGE else f"安裝失敗：{output}")
    if code != 0:
        return UpdateResult(False, output)
    refresh_import_paths()
    return UpdateResult(True, output)


def install_ytdlp_update(timeout: float = 300, proxy: str = "") -> UpdateResult:
    """Installs into the running interpreter's environment. Until a restart, the running app mixes the old copy in
    memory with new files read from disk, which breaks YouTube downloads; AppServices.update_ytdlp guards this."""
    code, output = run_pip_install(["--upgrade", YT_DLP_INSTALL_TARGET], timeout, proxy)
    if code is None:
        return UpdateResult(False, output if output == PIP_BUSY_MESSAGE else f"更新失敗：{output}")
    if code != 0:
        return UpdateResult(False, output)

    installed = installed_ytdlp_version()
    if installed != YT_DLP_VERSION:
        # Also when the version cannot be read: then pip may have changed the files, so a restart is the safe answer.
        return UpdateResult(True, output, restart_required=True)
    # pip exits 0 without installing anything when no newer release supports this Python, as with 3.10.
    if check_ytdlp_update(proxy=proxy).has_update:
        python = f"{sys.version_info.major}.{sys.version_info.minor}"
        return UpdateResult(False, f"這個 Python 版本（{python}）裝不了較新的 yt-dlp，請改用 Python 3.11 以上。")
    return UpdateResult(True, f"已是最新版本（{installed}），不需要更新。")


def update_ytdlp(timeout: float = 300, proxy: str = "") -> tuple[bool, str]:
    result = install_ytdlp_update(timeout, proxy)
    return result.succeeded, result.message


def app_repository_url() -> str:
    """The GitHub repository the README's guide is in, such as https://github.com/jackalcn/YouTubeVideoCap."""
    parsed = urlparse(LOCAL_RUN_GUIDE_URL)
    return f"{parsed.scheme}://{parsed.netloc}/{'/'.join(parsed.path.strip('/').split('/')[:2])}"


def app_version_date(version: str) -> Optional[date]:
    """APP_VERSION names the day it was made, as v2026.11.06.1."""
    found = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", version)
    try:
        return date(int(found.group(1)), int(found.group(2)), int(found.group(3))) if found else None
    except ValueError:
        return None


@dataclass(frozen=True)
class AppUpdateCheck:
    """GitHub's newest commit on main as it was found, so a later visit compares it with whatever runs by then."""

    latest_commit: str = ""
    # That commit's day on this machine's calendar, as 2026-11-07.
    latest_date: str = ""
    error: str = ""
    checked_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def compare(self, running_commit: str, version: str) -> tuple[bool, bool]:
        """Whether GitHub has a newer app, and whether that is only a guess from dates: a copy without git does not
        know its commit, so its version's date is compared with the newest commit's day instead."""
        if running_commit:
            return not self.latest_commit.startswith(running_commit), False
        released = app_version_date(version)
        try:
            latest = date.fromisoformat(self.latest_date)
        except ValueError:
            return False, True
        return released is not None and latest > released, True


def app_update_check_from_dict(data: Any) -> Optional[AppUpdateCheck]:
    """A check saved earlier, or None when there is none or it is damaged."""
    if not isinstance(data, dict):
        return None
    texts = [data.get(name, "") for name in ("latest_commit", "latest_date", "error")]
    checked_at = data.get("checked_at")
    if not all(isinstance(text, str) for text in texts) or isinstance(checked_at, bool) or not isinstance(checked_at, (int, float)):
        return None
    return AppUpdateCheck(*texts, checked_at=float(checked_at))


def check_app_update(timeout: float = 10, proxy: str = "") -> AppUpdateCheck:
    """Asks GitHub for the newest commit on main. Failures come back as the error, in words for the person."""
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    repository = urlparse(app_repository_url()).path.strip("/")
    request = urllib.request.Request(
        f"{GITHUB_API}/repos/{repository}/commits/main",
        headers={"User-Agent": APP_USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    checked_at = time.time()
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response:
            answer = json.load(response)
        committed = datetime.fromisoformat(str(answer["commit"]["committer"]["date"]).replace("Z", "+00:00"))
        return AppUpdateCheck(str(answer["sha"]), committed.astimezone().date().isoformat(), checked_at=checked_at)
    except urllib.error.HTTPError as exc:
        # Without a token GitHub answers 60 requests an hour from one network address, then refuses.
        if exc.code == 429 or (exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0"):
            error = "GitHub 暫時不接受查詢：每小時的查詢次數已用完，請過一小時再試。"
        else:
            error = f"GitHub 回應錯誤（HTTP {exc.code}），請稍後再試。"
    except OSError:
        error = "連不上 GitHub，請確認網路連線或代理伺服器設定後再試。"
    except Exception:
        error = "看不懂 GitHub 的回應，請稍後再試。"
    return AppUpdateCheck(error=error, checked_at=checked_at)


def get_free_disk_bytes(path: Path) -> Optional[int]:
    """The folder may not exist yet, so this checks the nearest parent that does."""
    current = path.expanduser()
    for candidate in [current, *current.parents]:
        try:
            if candidate.exists():
                return shutil.disk_usage(candidate).free
        except OSError:
            continue
    return None


def can_write_folder(folder: Path) -> bool:
    """Creates the folder and a scratch file in it: permission bits alone miss read-only mounts, ACLs and a Docker
    bind mount owned by another user."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handle, probe = tempfile.mkstemp(prefix=".videocap-write-test-", dir=folder)
        os.close(handle)
        os.unlink(probe)
    except OSError:
        return False
    return True


def format_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
