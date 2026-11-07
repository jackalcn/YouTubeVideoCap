from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

from conftest import shortcuts_can_be_saved

import videocap.tooling as tooling


@pytest.fixture
def only_a_fake_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Hides every real JavaScript runtime and puts a node that prints the given version on PATH."""
    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setattr(sysconfig, "get_path", lambda *args, **kwargs: str(tmp_path / "no-scripts"))
    monkeypatch.chdir(tmp_path)
    tooling.get_js_runtime_status.cache_clear()

    def install(version: str) -> None:
        if sys.platform == "win32":
            (tools / "node.bat").write_text(f"@echo v{version}\n", encoding="ascii")
        else:
            script = tools / "node"
            script.write_text(f"#!/bin/sh\necho v{version}\n", encoding="ascii")
            script.chmod(0o755)

    yield install
    tooling.get_js_runtime_status.cache_clear()


def test_a_javascript_runtime_too_old_for_yt_dlp_is_reported(only_a_fake_node) -> None:  # type: ignore[no-untyped-def]
    only_a_fake_node("18.19.0")
    available, status = tooling.get_js_runtime_status()
    assert not available
    assert "18.19.0" in status and "22" in status


def test_a_supported_javascript_runtime_is_found(only_a_fake_node) -> None:  # type: ignore[no-untyped-def]
    only_a_fake_node("22.11.0")
    available, path = tooling.get_js_runtime_status()
    assert available
    assert Path(path).stem.lower() == "node"


# Updating yt-dlp


class FakePip:
    """Stands in for pip and for the fresh interpreter that reads the yt-dlp version left on disk."""

    def __init__(self, installed: str, returncode: int = 0) -> None:
        self.installed = installed
        self.returncode = returncode
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if "pip" in command:
            return subprocess.CompletedProcess(command, self.returncode, "Requirement already satisfied", "")
        return subprocess.CompletedProcess(command, 0, f"{self.installed}\n", "")

    @property
    def pip_command(self) -> list[str]:
        return next(command for command in self.commands if "pip" in command)


@pytest.fixture
def running_ytdlp(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, str, str], FakePip]:
    """Sets the yt-dlp version this process runs, the one pip leaves on disk, and the newest one on PyPI."""

    def setup(running: str, installed: str, latest: str) -> FakePip:
        fake = FakePip(installed)
        monkeypatch.setattr(tooling, "YT_DLP_VERSION", running)
        monkeypatch.setattr(tooling.subprocess, "run", fake)
        monkeypatch.setattr(tooling, "check_ytdlp_update", lambda timeout=10, proxy="": tooling.UpdateCheck(running, latest))
        return fake

    return setup


def test_an_update_that_installs_a_newer_yt_dlp_needs_a_restart(running_ytdlp: Callable[[str, str, str], FakePip]) -> None:
    fake = running_ytdlp("2026.10.21", "2026.10.26", "2026.10.26")
    result = tooling.install_ytdlp_update()
    assert result.succeeded and result.restart_required
    # The running process keeps the copy it imported, so only a fresh interpreter sees what pip installed.
    assert any(command[0] == sys.executable and "-c" in command for command in fake.commands)


def test_an_update_that_installs_nothing_on_an_old_python_is_reported(running_ytdlp: Callable[[str, str, str], FakePip]) -> None:
    running_ytdlp("2026.10.21", "2026.10.21", "2026.10.26")
    result = tooling.install_ytdlp_update()
    assert not result.succeeded and not result.restart_required
    assert "Python 3.11" in result.message
    assert tooling.update_ytdlp() == (False, result.message)


def test_an_update_when_yt_dlp_is_already_the_newest_says_so(running_ytdlp: Callable[[str, str, str], FakePip]) -> None:
    running_ytdlp("2026.10.26", "2026.10.26", "2026.10.26")
    result = tooling.install_ytdlp_update()
    assert result.succeeded and not result.restart_required
    assert "已是最新版本" in result.message


def test_a_failed_pip_run_installs_nothing(running_ytdlp: Callable[[str, str, str], FakePip]) -> None:
    fake = running_ytdlp("2026.10.21", "2026.10.21", "2026.10.26")
    fake.returncode = 1
    result = tooling.install_ytdlp_update()
    assert not result.succeeded and not result.restart_required
    assert result.message == "Requirement already satisfied"


def test_the_update_goes_through_the_proxy(running_ytdlp: Callable[[str, str, str], FakePip]) -> None:
    fake = running_ytdlp("2026.10.21", "2026.10.26", "2026.10.26")
    tooling.update_ytdlp(proxy="http://proxy.example:8080")
    command = fake.pip_command
    assert command[command.index("--proxy") + 1] == "http://proxy.example:8080"

    tooling.update_ytdlp()
    assert "--proxy" not in fake.commands[-2]


# Installing speech recognition


@pytest.fixture
def speech_pip(monkeypatch: pytest.MonkeyPatch) -> FakePip:
    """Stands in for pip installing faster-whisper, which then imports."""
    import importlib.util

    fake = FakePip("")
    monkeypatch.setattr(tooling.subprocess, "run", fake)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "faster_whisper" else None)
    return fake


def test_speech_recognition_installs_without_upgrading_what_the_app_runs(speech_pip: FakePip) -> None:
    result = tooling.install_speech_recognition(proxy="http://proxy.example:8080")
    assert result.succeeded and not result.restart_required
    assert result.message == "Requirement already satisfied"
    command = speech_pip.pip_command
    assert command[:4] == [sys.executable, "-m", "pip", "install"] and command[-1] == "faster-whisper>=1.1"
    # --upgrade would also replace packages the running app has already imported.
    assert "--upgrade" not in command
    assert command[command.index("--proxy") + 1] == "http://proxy.example:8080"
    tooling.install_speech_recognition()
    assert "--proxy" not in speech_pip.commands[-1]


def test_a_failed_speech_install_shows_pips_output(speech_pip: FakePip) -> None:
    speech_pip.returncode = 1
    result = tooling.install_speech_recognition()
    assert not result.succeeded and result.message == "Requirement already satisfied"


def test_a_speech_install_python_cannot_see_yet_asks_for_a_restart(speech_pip: FakePip, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    result = tooling.install_speech_recognition()
    assert result.succeeded and result.restart_required


def test_only_one_pip_runs_at_a_time(speech_pip: FakePip, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tooling, "check_ytdlp_update", lambda timeout=10, proxy="": tooling.UpdateCheck("2026.10.26", "2026.10.26"))
    with tooling._pip_lock:
        assert tooling.install_speech_recognition() == tooling.UpdateResult(False, tooling.PIP_BUSY_MESSAGE)
        assert tooling.install_ytdlp_update() == tooling.UpdateResult(False, tooling.PIP_BUSY_MESSAGE)
    assert speech_pip.commands == []
    assert tooling.install_speech_recognition().succeeded


def test_gpu_support_installs_cublas_for_cuda_12(speech_pip: FakePip) -> None:
    result = tooling.install_gpu_support(proxy="http://proxy.example:8080")
    assert result.succeeded and result.message == "Requirement already satisfied"
    command = speech_pip.pip_command
    # CTranslate2 loads only cuBLAS on the GPU; its Windows and Linux builds do not use cuDNN.
    assert tooling.GPU_INSTALL_TARGETS == ("nvidia-cublas-cu12",)
    assert command[-1:] == list(tooling.GPU_INSTALL_TARGETS) and "--upgrade" not in command
    assert command[command.index("--proxy") + 1] == "http://proxy.example:8080"
    speech_pip.returncode = 1
    assert tooling.install_gpu_support() == tooling.UpdateResult(False, "Requirement already satisfied")
    with tooling._pip_lock:
        assert tooling.install_gpu_support() == tooling.UpdateResult(False, tooling.PIP_BUSY_MESSAGE)


def test_requirements_speech_lists_what_the_buttons_install() -> None:
    lines = (Path(__file__).resolve().parents[1] / "requirements-speech.txt").read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line.strip() and not line.startswith("#")] == [tooling.SPEECH_INSTALL_TARGET]
    # The GPU part is optional, so commented out, but named exactly as the button installs it.
    assert [line.removeprefix("# ") for line in lines if line.removeprefix("# ") in tooling.GPU_INSTALL_TARGETS] == list(tooling.GPU_INSTALL_TARGETS)


def test_a_user_install_that_made_the_user_folder_is_importable_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In Docker pip installs into PYTHONUSERBASE, whose site-packages Python only adds at start when it exists."""
    import site

    user_site = tmp_path / "python" / "lib" / "site-packages"
    monkeypatch.setattr(site, "ENABLE_USER_SITE", True)
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(user_site))
    monkeypatch.setattr(sys, "path", list(sys.path))
    tooling.refresh_import_paths()
    assert str(user_site) not in sys.path
    user_site.mkdir(parents=True)
    tooling.refresh_import_paths()
    tooling.refresh_import_paths()
    assert sys.path.count(str(user_site)) == 1


def test_the_update_check_goes_through_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers: list[list[object]] = []

    class FakeOpener:
        def open(self, request: object, timeout: float) -> io.BytesIO:
            return io.BytesIO(json.dumps({"info": {"version": "2099.1.1"}}).encode())

    def build_opener(*given: object) -> FakeOpener:
        handlers.append(list(given))
        return FakeOpener()

    monkeypatch.setattr(tooling.urllib.request, "build_opener", build_opener)
    check = tooling.check_ytdlp_update(proxy="http://proxy.example:8080")
    assert check.latest == "2099.1.1" and check.has_update
    proxy_handler = handlers[-1][0]
    assert isinstance(proxy_handler, urllib.request.ProxyHandler)
    assert proxy_handler.proxies == {"http": "http://proxy.example:8080", "https": "http://proxy.example:8080"}

    # Without a proxy setting, the system's proxy still applies, as it did before.
    assert tooling.check_ytdlp_update().latest == "2099.1.1"
    assert handlers[-1] == []


# Asking GitHub for a newer version of the app, from a server on this machine that stands in for GitHub's API


class GitHubStandIn(BaseHTTPRequestHandler):
    """Answers with what the running test chose, and records what it was asked."""

    answer: dict[str, Any] = {}
    requests: list[dict[str, str]] = []

    def do_GET(self) -> None:
        GitHubStandIn.requests.append({"path": self.path, **dict(self.headers)})
        body = json.dumps(GitHubStandIn.answer.get("json", {})).encode()
        self.send_response(GitHubStandIn.answer.get("status", 200))
        for name, value in GitHubStandIn.answer.get("headers", {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


# One server for the whole run, on a port the system picks; other test files import the fixture below.
GITHUB_SERVER = ThreadingHTTPServer(("127.0.0.1", 0), GitHubStandIn)
GITHUB_SERVER.daemon_threads = True
threading.Thread(target=GITHUB_SERVER.serve_forever, name="test-github-server", daemon=True).start()
GITHUB_BASE = f"http://127.0.0.1:{GITHUB_SERVER.server_address[1]}"
NEWEST_SHA = "abc1234" + "0" * 33
# Midday, so the day is the same on most clocks; the expected day is still worked out on this machine's own.
NEWEST_DATE = "2026-11-07T12:00:00Z"


def latest_commit(sha: str = NEWEST_SHA, when: str = NEWEST_DATE) -> dict[str, Any]:
    return {"json": {"sha": sha, "commit": {"author": {"date": when}, "committer": {"date": when}}}}


def local_day(when: str) -> str:
    return datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone().date().isoformat()


def point_at_github_stand_in(monkeypatch: pytest.MonkeyPatch) -> type[GitHubStandIn]:
    monkeypatch.setattr(tooling, "GITHUB_API", GITHUB_BASE)
    GitHubStandIn.answer = latest_commit()
    GitHubStandIn.requests = []
    return GitHubStandIn


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> type[GitHubStandIn]:
    return point_at_github_stand_in(monkeypatch)


def test_the_newest_app_version_is_read_from_the_apps_own_repository(github: type[GitHubStandIn]) -> None:
    before = time.time()
    check = tooling.check_app_update()
    assert check.error == ""
    assert check.latest_commit == NEWEST_SHA
    assert check.latest_date == local_day(NEWEST_DATE)
    assert check.checked_at >= before
    (request,) = github.requests
    # The repository the README's guide points at.
    assert request["path"] == "/repos/jackalcn/YouTubeVideoCap/commits/main"
    assert request["User-Agent"] == "YouTube-VideoCap"
    assert tooling.app_repository_url() == "https://github.com/jackalcn/YouTubeVideoCap"


def test_the_running_commit_is_compared_with_githubs_newest() -> None:
    check = tooling.AppUpdateCheck(NEWEST_SHA, "2026-11-07")
    # (newer, only a guess from dates)
    assert check.compare("abc1234", "v2026.11.06.1") == (False, False)
    assert check.compare("1111111", "v2026.11.07.1") == (True, False)
    # Without git the running commit is unknown, and only the version's date is left to go by.
    assert check.compare("", "v2026.11.06.1") == (True, True)
    assert check.compare("", "v2026.11.07.1") == (False, True)


@pytest.mark.parametrize(
    ("answer", "phrase"),
    [
        # Without a token GitHub takes 60 requests an hour from one address.
        ({"status": 403, "headers": {"X-RateLimit-Remaining": "0"}, "json": {"message": "API rate limit exceeded"}}, "次數"),
        ({"status": 429, "json": {}}, "次數"),
        ({"status": 500, "json": {}}, "HTTP 500"),
        ({"json": {"unexpected": True}}, "看不懂"),
    ],
)
def test_a_failed_app_check_says_why(github: type[GitHubStandIn], answer: dict[str, Any], phrase: str) -> None:
    github.answer = answer
    check = tooling.check_app_update()
    assert phrase in check.error
    assert check.latest_commit == "" and check.checked_at > 0


def test_an_app_check_without_a_network_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    # A port nothing listens on, as close to having no network as a test can get without leaving this machine.
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    monkeypatch.setattr(tooling, "GITHUB_API", f"http://127.0.0.1:{port}")
    assert "連不上 GitHub" in tooling.check_app_update(timeout=5).error


def test_the_app_check_goes_through_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers: list[list[object]] = []
    seen: list[Any] = []

    class FakeOpener:
        def open(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
            seen.append((request.get_header("User-agent"), timeout))
            return io.BytesIO(json.dumps(latest_commit()["json"]).encode())

    def build_opener(*given: object) -> FakeOpener:
        handlers.append(list(given))
        return FakeOpener()

    monkeypatch.setattr(tooling.urllib.request, "build_opener", build_opener)
    assert tooling.check_app_update(proxy="http://proxy.example:8080").latest_commit == NEWEST_SHA
    proxy_handler = handlers[-1][0]
    assert isinstance(proxy_handler, urllib.request.ProxyHandler)
    assert proxy_handler.proxies == {"http": "http://proxy.example:8080", "https": "http://proxy.example:8080"}
    assert seen == [("YouTube-VideoCap", 10)]
    tooling.check_app_update()
    assert handlers[-1] == []


def test_a_saved_app_check_is_read_back_or_ignored() -> None:
    check = tooling.AppUpdateCheck(NEWEST_SHA, "2026-11-07", checked_at=1.0)
    assert tooling.app_update_check_from_dict(check.to_dict()) == check
    # Whatever an older or damaged database holds.
    assert tooling.app_update_check_from_dict(None) is None
    assert tooling.app_update_check_from_dict({"latest_commit": 5, "checked_at": "soon"}) is None


def test_a_writable_folder_is_created_and_left_clean(tmp_path: Path) -> None:
    folder = tmp_path / "new" / "videos"
    assert tooling.can_write_folder(folder)
    assert folder.is_dir() and list(folder.iterdir()) == []


def test_a_folder_that_refuses_new_files_is_not_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(**kwargs: object) -> tuple[int, str]:
        raise PermissionError(13, "Permission denied")

    # As on a Docker bind mount owned by another user, where the folder exists but nothing can be written in it.
    monkeypatch.setattr(tooling.tempfile, "mkstemp", refuse)
    assert not tooling.can_write_folder(tmp_path)


def test_no_tool_opens_a_console_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import videocap.environment as environment

    flags: list[object] = []

    def record(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        flags.append(kwargs.get("creationflags"))
        return subprocess.CompletedProcess(command, 0, "2026.10.26\n", "")

    monkeypatch.setattr(subprocess, "run", record)
    monkeypatch.setattr(tooling, "check_ytdlp_update", lambda timeout=10, proxy="": tooling.UpdateCheck("2026.10.26", "2026.10.26"))
    tooling.probe_executable("python", "--version", str(Path(sys.executable).parent))
    tooling.read_tool_version(sys.executable, "--version")
    tooling.installed_ytdlp_version()
    tooling.install_ytdlp_update()
    tooling.install_speech_recognition()
    monkeypatch.delenv("SOURCE_COMMIT", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    environment.detect_runtime_commit_short()
    expected = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    assert len(flags) >= 6 and set(flags) == {expected}


# Starting with Windows

windows_only = pytest.mark.skipif(
    sys.platform != "win32" or not shortcuts_can_be_saved(), reason="the Startup folder is Windows', and this session must save shortcuts"
)


def read_shortcut(path: Path) -> list[str]:
    """What a shortcut runs, read back the way Windows reads it."""
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:VIDEOCAP_SHORTCUT); "
        "$s.TargetPath; $s.Arguments; $s.WorkingDirectory"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        encoding="utf-8",
        check=True,
        env={**os.environ, "VIDEOCAP_SHORTCUT": str(path)},
    )
    return result.stdout.splitlines()


@pytest.fixture
def startup_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Startup folder of the test's own, and an app folder with a quote and Chinese in its name."""
    import videocap.environment as environment

    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    app_dir = tmp_path / "Matthew's 下載 [app]"
    (app_dir / ".venv" / "Scripts").mkdir(parents=True)
    (app_dir / ".venv" / "Scripts" / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(environment, "APP_DIR", app_dir)
    return app_dir


@windows_only
def test_starting_with_windows_is_turned_on_and_off(startup_folder: Path, tmp_path: Path) -> None:
    import videocap.environment as environment

    shortcut = tmp_path / "AppData" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "YouTube VideoCap.lnk"
    assert environment.autostart_shortcut() == shortcut
    assert not environment.autostart_enabled()

    assert environment.enable_autostart() == ""
    assert environment.autostart_enabled()
    assert read_shortcut(shortcut) == [
        str(startup_folder / ".venv" / "Scripts" / "pythonw.exe"),
        "-m videocap --server.headless true --server.address localhost",
        str(startup_folder),
    ]

    assert environment.disable_autostart() == ""
    assert not shortcut.exists() and not environment.autostart_enabled()
    assert environment.disable_autostart() == ""


@windows_only
def test_starting_with_windows_needs_the_app_set_up_first(startup_folder: Path) -> None:
    import videocap.environment as environment

    (startup_folder / ".venv" / "Scripts" / "pythonw.exe").unlink()
    assert "start.bat" in environment.enable_autostart()
    assert not environment.autostart_enabled()


def test_the_page_and_autostart_bat_make_the_same_shortcut() -> None:
    import videocap.environment as environment

    script = (Path(environment.__file__).resolve().parents[1] / "autostart.bat").read_text(encoding="utf-8")
    assert environment.AUTOSTART_SHORTCUT_NAME in script
    assert environment.AUTOSTART_ARGUMENTS in script


def short_temp_folder() -> Path:
    """A folder in the system's own temp folder: the tests' one is so deep that a shortcut's path inside it would pass
    Windows' 260 characters."""
    import tempfile

    temp = os.environ.get("TEMP") or tempfile.gettempdir()
    # A run whose TEMP points somewhere deep would otherwise fail on the path's length, not on what is tested.
    if len(temp) > 80:
        temp = os.path.join(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"), "Temp")
        os.makedirs(temp, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="vc-", dir=temp))


@windows_only
def test_an_app_folder_too_deep_for_a_shortcut_is_refused_with_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    import videocap.environment as environment

    base = short_temp_folder()
    try:
        monkeypatch.setenv("APPDATA", str(base / "AppData"))
        # The shortcut object quietly changes a target longer than MAX_PATH, so the shortcut would start nothing.
        app_dir = base / ("x" * 100) / ("y" * 100) / ("z" * 60) / "app"
        (app_dir / ".venv" / "Scripts").mkdir(parents=True)
        (app_dir / ".venv" / "Scripts" / "pythonw.exe").write_bytes(b"")
        monkeypatch.setattr(environment, "APP_DIR", app_dir)
        problem = environment.enable_autostart()
        assert "太長" in problem and "%" not in problem
        assert not environment.autostart_shortcut().exists() and not environment.autostart_enabled()
    finally:
        monkeypatch.undo()
        shutil.rmtree(base, ignore_errors=True)


@windows_only
@pytest.mark.parametrize("folder", ["a $(New-Item pwned) `b ${env_TEMP} 'q' 下載 [app] ;& 100%", "空 白 $x`$y' %NO_SUCH_VARIABLE%"])
def test_starting_with_windows_survives_any_folder_name(monkeypatch: pytest.MonkeyPatch, folder: str) -> None:
    import shutil

    import videocap.environment as environment

    base = short_temp_folder()
    try:
        # Both the app's folder and the user's profile folder can hold characters PowerShell would read as code.
        monkeypatch.setenv("APPDATA", str(base / folder))
        app_dir = base / folder / "app"
        (app_dir / ".venv" / "Scripts").mkdir(parents=True)
        (app_dir / ".venv" / "Scripts" / "pythonw.exe").write_bytes(b"")
        monkeypatch.setattr(environment, "APP_DIR", app_dir)
        monkeypatch.chdir(base)
        # Another program's shortcut in the same folder.
        environment.startup_folder().mkdir(parents=True)
        other = environment.startup_folder() / "Other app.lnk"
        other.write_bytes(b"other")

        assert environment.enable_autostart() == ""
        assert read_shortcut(environment.autostart_shortcut()) == [
            str(app_dir / ".venv" / "Scripts" / "pythonw.exe"),
            environment.AUTOSTART_ARGUMENTS,
            str(app_dir),
        ]
        # Nothing in the names ran as a command.
        assert not list(base.rglob("pwned*"))
        assert environment.disable_autostart() == ""
        assert not environment.autostart_shortcut().exists()
        assert other.read_bytes() == b"other"
    finally:
        monkeypatch.undo()
        shutil.rmtree(base, ignore_errors=True)


@windows_only
@pytest.mark.parametrize("folder", ["a %USERNAME% b", "x %TEMP% y"])
def test_a_folder_name_windows_would_expand_is_refused(monkeypatch: pytest.MonkeyPatch, folder: str) -> None:
    import shutil

    import videocap.environment as environment

    # Windows' shortcut object replaces %USERNAME% in a target with the user's name, so it would start nothing.
    base = short_temp_folder()
    try:
        monkeypatch.setenv("APPDATA", str(base / "AppData"))
        app_dir = base / folder
        (app_dir / ".venv" / "Scripts").mkdir(parents=True)
        (app_dir / ".venv" / "Scripts" / "pythonw.exe").write_bytes(b"")
        monkeypatch.setattr(environment, "APP_DIR", app_dir)
        assert "%" in environment.enable_autostart()
        assert not environment.autostart_shortcut().exists() and not environment.autostart_enabled()
    finally:
        monkeypatch.undo()
        shutil.rmtree(base, ignore_errors=True)
