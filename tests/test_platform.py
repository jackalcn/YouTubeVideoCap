"""The files people start the app with: the launchers, the container, the dev container and the requirements."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import shortcuts_can_be_saved


ROOT = Path(__file__).resolve().parents[1]
# A folder name like the ones people really use: spaces, Chinese and brackets all break careless quoting.
FOLDER_NAME = "測試 資料夾 (A)"
REQUIREMENTS = "streamlit>=1.57\nyt-dlp[default,deno]\n"

SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None, reason="needs a POSIX shell")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="start.bat is for Windows")
# autostart.bat saves a shortcut through WScript.Shell, which GitHub's Windows runners refuse.
needs_shortcuts = pytest.mark.skipif(not shortcuts_can_be_saved(), reason="this Windows session cannot save shortcuts")

# Stand-ins for the system Python and the one inside .venv. Files in $FAKE_STATE decide how they behave,
# so a test can make the network fail, the Python too old, or venv lack ensurepip, without installing anything.
FAKE_BASE_PYTHON_SH = """#!/bin/sh
echo "base $(basename "$0") $*" >> "$FAKE_STATE/log"
case "$1" in
  -c) [ ! -e "$FAKE_STATE/too_old_$(basename "$0")" ]; exit $? ;;
  -m)
    [ "$2" = venv ] || exit 2
    rm -rf .venv
    mkdir -p .venv/bin
    cp "$FAKE_STATE/venv_python" .venv/bin/python
    chmod +x .venv/bin/python
    if [ -e "$FAKE_STATE/no_ensurepip" ]; then
      echo "The virtual environment was not created successfully because ensurepip is not available." >&2
      exit 1
    fi
    touch .venv/pip
    exit 0 ;;
esac
exit 2
"""

FAKE_VENV_PYTHON_SH = """#!/bin/sh
echo "venv $*" >> "$FAKE_STATE/log"
venv_dir="$(dirname "$0")/.."
case "$1" in
  -c)
    case "$2" in *streamlit*) [ -e "$venv_dir/installed" ] || exit 1 ;; esac
    case "$2" in *pip*) [ -e "$venv_dir/pip" ] || exit 1 ;; esac
    exit 0 ;;
  -m)
    case "$2" in
      pip)
        [ -e "$venv_dir/pip" ] || { echo "No module named pip" >&2; exit 1; }
        [ ! -e "$FAKE_STATE/pip_fails" ] || { echo "ERROR: No matching distribution found for streamlit>=1.57" >&2; exit 1; }
        touch "$venv_dir/installed"
        exit 0 ;;
      videocap)
        [ -e "$venv_dir/installed" ] || { echo "No module named streamlit" >&2; exit 1; }
        exit 0 ;;
    esac ;;
esac
exit 2
"""

BASE_PYTHON_NAMES = ("python3", "python3.11", "python3.12", "python3.13", "python3.14")

# Stands in for the app's launcher (python -m videocap) in a copied project, recording how it was started.
FAKE_LAUNCHER = r'''
import os
import sys

with open(os.path.join(os.environ["FAKE_STATE"], "log"), "a", encoding="utf-8") as log:
    log.write("venv -m videocap " + " ".join(sys.argv[1:]) + "\n")
    log.write("cwd " + os.getcwd() + "\n")
'''


def make_project(tmp_path: Path, *names: str) -> Path:
    project = tmp_path / FOLDER_NAME
    project.mkdir()
    for name in names:
        shutil.copyfile(ROOT / name, project / name)
    (project / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    (project / "main.py").write_text("", encoding="utf-8")
    (project / "videocap").mkdir()
    (project / "videocap" / "__init__.py").write_text("", encoding="utf-8")
    (project / "videocap" / "__main__.py").write_text(FAKE_LAUNCHER, encoding="utf-8")
    return project


def read_log(state: Path) -> list[str]:
    log = state / "log"
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    log.unlink(missing_ok=True)
    return lines


@pytest.fixture
def sh_launcher(tmp_path: Path):
    project = make_project(tmp_path, "start.sh")
    state = tmp_path / "state"
    stubs = tmp_path / "stubs"
    state.mkdir()
    stubs.mkdir()
    (state / "venv_python").write_text(FAKE_VENV_PYTHON_SH, encoding="utf-8", newline="\n")
    for name in BASE_PYTHON_NAMES:
        stub = stubs / name
        stub.write_text(FAKE_BASE_PYTHON_SH, encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    env = {**os.environ, "FAKE_STATE": state.as_posix(), "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}
    for name in ("VIDEOCAP_ADDRESS", "VIDEOCAP_HEADLESS"):
        env.pop(name, None)

    def run(**extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [SH, str(project / "start.sh")],
            cwd=tmp_path,
            env={**env, **extra_env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

    return project, state, run


def launched(log: list[str]) -> bool:
    # Through the app's launcher, which starts the download engine before Streamlit and not on the first page view.
    return any(line.startswith("venv -m videocap ") for line in log)


def launch_options(log: list[str]) -> str:
    return next(line.removeprefix("venv -m videocap ") for line in log if line.startswith("venv -m videocap "))


@needs_sh
def test_start_sh_retries_an_install_that_failed_the_first_time(sh_launcher) -> None:
    project, state, run = sh_launcher
    (state / "pip_fails").touch()
    first = run()
    assert first.returncode != 0
    assert not launched(read_log(state))
    assert "network" not in first.stderr.lower()

    (state / "pip_fails").unlink()
    second = run()
    log = read_log(state)
    assert second.returncode == 0, second.stderr
    assert "venv -m pip install -r requirements.txt" in log
    assert launched(log)


@needs_sh
def test_start_sh_installs_again_only_when_requirements_change(sh_launcher) -> None:
    project, state, run = sh_launcher
    assert run().returncode == 0
    read_log(state)

    assert run().returncode == 0
    log = read_log(state)
    assert not any("pip install" in line or line.startswith("base") for line in log)
    assert launched(log)

    (project / "requirements.txt").write_text(REQUIREMENTS + "secretstorage\n", encoding="utf-8")
    assert run().returncode == 0
    log = read_log(state)
    assert "venv -m pip install -r requirements.txt" in log
    # The environment still works, so it is updated in place rather than rebuilt from nothing.
    assert not any(line.startswith("base") and "venv" in line for line in log)


@needs_sh
def test_start_sh_rebuilds_an_environment_whose_python_is_gone(sh_launcher) -> None:
    project, state, run = sh_launcher
    assert run().returncode == 0
    read_log(state)
    # What .venv/bin/python does after the Python it was made from has been uninstalled or upgraded.
    (project / ".venv" / "bin" / "python").write_text("#!/bin/sh\nexit 127\n", encoding="utf-8", newline="\n")

    result = run()
    log = read_log(state)
    assert result.returncode == 0, result.stderr
    assert "base python3 -m venv --clear .venv" in log
    assert launched(log)


@needs_sh
def test_start_sh_recovers_once_python3_venv_is_installed(sh_launcher) -> None:
    project, state, run = sh_launcher
    (state / "no_ensurepip").touch()
    first = run()
    assert first.returncode != 0
    assert "python3-venv" in first.stderr
    assert (project / ".venv" / "bin" / "python").exists()

    (state / "no_ensurepip").unlink()
    second = run()
    assert second.returncode == 0, second.stderr
    assert launched(read_log(state))


@needs_sh
def test_start_sh_skips_a_python_older_than_3_11(sh_launcher) -> None:
    project, state, run = sh_launcher
    (state / "too_old_python3").touch()
    result = run()
    log = read_log(state)
    assert result.returncode == 0, result.stderr
    assert not any(line.startswith("base python3 -m venv") for line in log)
    assert any(re.match(r"base python3\.1\d -m venv", line) for line in log)

    shutil.rmtree(project / ".venv")
    for name in BASE_PYTHON_NAMES:
        (state / f"too_old_{name}").touch()
    result = run()
    assert result.returncode != 0
    assert "3.11" in result.stderr
    assert not (project / ".venv").exists()


@needs_sh
def test_start_sh_passes_its_streamlit_options_to_the_launcher(sh_launcher) -> None:
    project, state, run = sh_launcher
    assert run().returncode == 0
    assert launch_options(read_log(state)) == "--server.address localhost --server.headless false"

    assert run(VIDEOCAP_ADDRESS="0.0.0.0", VIDEOCAP_HEADLESS="true").returncode == 0
    assert launch_options(read_log(state)) == "--server.address 0.0.0.0 --server.headless true"


# start.bat must find an .exe at .venv\Scripts\python.exe, so on Windows the stand-in builds a real venv
# without pip, then adds a fake pip that "installs" empty streamlit and yt_dlp packages.
FAKE_PIP = r'''
import os
import sys
from pathlib import Path

state = Path(os.environ["FAKE_STATE"])
with open(state / "log", "a", encoding="utf-8") as log:
    log.write("venv -m pip " + " ".join(sys.argv[1:]) + "\n")
if (state / "pip_fails").exists():
    print("ERROR: No matching distribution found for streamlit>=1.57", file=sys.stderr)
    sys.exit(1)
site = Path(__file__).resolve().parents[1]
for name in ("streamlit", "yt_dlp"):
    (site / name).mkdir(exist_ok=True)
    (site / name / "__init__.py").write_text("")
'''

FAKE_BASE_PYTHON_PY = r'''
import os
import sys
import venv
from pathlib import Path

state = Path(os.environ["FAKE_STATE"])
args = sys.argv[1:]
with open(state / "log", "a", encoding="utf-8") as log:
    log.write("base " + " ".join(args) + "\n")
if args[:1] == ["-3"]:
    args = args[1:]
if args[:1] == ["-c"]:
    sys.exit(1 if (state / "too_old").exists() else 0)
if args[:2] == ["-m", "venv"]:
    target = Path(args[-1])
    venv.create(target, clear=True, with_pip=False)
    pip_dir = target / "Lib" / "site-packages" / "pip"
    pip_dir.mkdir()
    (pip_dir / "__init__.py").write_text("")
    (pip_dir / "__main__.py").write_text((state / "fake_pip.py").read_text())
    sys.exit(0)
sys.exit(2)
'''


@pytest.fixture
def bat_launcher(tmp_path: Path):
    project = make_project(tmp_path, "start.bat")
    state = tmp_path / "state"
    stubs = tmp_path / "stubs"
    state.mkdir()
    stubs.mkdir()
    (state / "fake_pip.py").write_text(FAKE_PIP, encoding="utf-8")
    (stubs / "fake_python.py").write_text(FAKE_BASE_PYTHON_PY, encoding="utf-8")
    (stubs / "py.cmd").write_text(f'@"{sys.executable}" "%~dp0fake_python.py" %*\r\n', encoding="utf-8")
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    env = {
        **os.environ,
        "FAKE_STATE": str(state),
        "PATH": os.pathsep.join([str(stubs), rf"{system_root}\System32", system_root]),
    }
    for name in ("VIDEOCAP_ADDRESS", "VIDEOCAP_HEADLESS"):
        env.pop(name, None)

    def run(folder: Path = project, cwd: Path = tmp_path, **extra_env: str) -> subprocess.CompletedProcess[str]:
        # The way Explorer runs a double-clicked .bat.
        return subprocess.run(
            f'cmd /d /c ""{folder / "start.bat"}" "',
            cwd=cwd,
            env={**env, **extra_env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=180,
        )

    return project, state, run


@windows_only
def test_start_bat_retries_an_install_that_failed_the_first_time(bat_launcher) -> None:
    project, state, run = bat_launcher
    (state / "pip_fails").touch()
    first = run()
    assert first.returncode != 0
    assert not launched(read_log(state))
    assert "network" not in first.stdout.lower()

    (state / "pip_fails").unlink()
    second = run()
    log = read_log(state)
    assert "venv -m pip install -r requirements.txt" in log, second.stdout
    assert launched(log), second.stdout

    (project / "requirements.txt").write_text(REQUIREMENTS + "secretstorage\n", encoding="utf-8")
    run()
    log = read_log(state)
    assert "venv -m pip install -r requirements.txt" in log
    assert not any(line.startswith("base") and "venv" in line for line in log)
    assert launched(log)


@windows_only
def test_start_bat_passes_its_streamlit_options_to_the_launcher(bat_launcher) -> None:
    project, state, run = bat_launcher
    run()
    assert launch_options(read_log(state)) == "--server.address localhost --server.headless false"

    run(VIDEOCAP_ADDRESS="0.0.0.0", VIDEOCAP_HEADLESS="true")
    assert launch_options(read_log(state)) == "--server.address 0.0.0.0 --server.headless true"


@windows_only
def test_start_bat_refuses_a_python_older_than_3_11(bat_launcher) -> None:
    project, state, run = bat_launcher
    (state / "too_old").touch()
    result = run()
    assert result.returncode != 0
    assert "3.11" in result.stdout
    assert not (project / ".venv").exists()
    assert not launched(read_log(state))


@windows_only
def test_start_bat_works_from_a_network_folder(bat_launcher) -> None:
    project, state, run = bat_launcher
    drive, rest = os.path.splitdrive(str(project))
    unc = Path(rf"\\localhost\{drive[0]}$" + rest)
    if not unc.exists():
        pytest.skip("the administrative share \\\\localhost\\C$ is not reachable here")

    # Explorer starts cmd inside the share, which cmd cannot use as its current folder.
    result = run(unc, cwd=unc)
    log = read_log(state)
    assert launched(log), result.stdout + result.stderr
    assert (project / ".venv" / "Scripts" / "python.exe").exists()
    assert log[-1].rstrip("\\").endswith(FOLDER_NAME)


AUTOSTART_ARGUMENTS = "-m videocap --server.headless true --server.address localhost"


@pytest.fixture
def autostart(tmp_path: Path):
    project = make_project(tmp_path, "autostart.bat")
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    startup = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    # What start.bat leaves behind; autostart.bat only points at it.
    (project / ".venv" / "Scripts").mkdir(parents=True)
    (project / ".venv" / "Scripts" / "pythonw.exe").touch()
    # An address meant for start.bat must not open the hidden copy to the network.
    env = {**os.environ, "APPDATA": str(appdata), "VIDEOCAP_ADDRESS": "0.0.0.0"}

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            f'cmd /d /c ""{project / "autostart.bat"}" {" ".join(arguments)}"',
            cwd=tmp_path,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )

    return project, startup, run


def read_shortcut(path: Path) -> dict[str, str]:
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SHORTCUT_PATH); "
        "ConvertTo-Json @{target = $s.TargetPath; arguments = $s.Arguments; folder = $s.WorkingDirectory}"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "SHORTCUT_PATH": str(path)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@windows_only
@needs_shortcuts
def test_autostart_starts_the_app_hidden_at_sign_in_for_this_computer_only(autostart) -> None:
    project, startup, run = autostart
    added = run()
    assert added.returncode == 0, added.stdout + added.stderr
    shortcuts = list(startup.glob("*.lnk"))
    assert len(shortcuts) == 1, added.stdout
    shortcut = read_shortcut(shortcuts[0])
    assert Path(shortcut["target"]) == project / ".venv" / "Scripts" / "pythonw.exe"
    assert shortcut["arguments"] == AUTOSTART_ARGUMENTS
    assert Path(shortcut["folder"]) == project

    # Running it again replaces the entry rather than starting the app twice at sign-in.
    assert run().returncode == 0
    assert len(list(startup.glob("*.lnk"))) == 1

    removed = run("remove")
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert not list(startup.iterdir())
    assert "removed" in removed.stdout.lower()
    assert run("remove").returncode == 0


@windows_only
@needs_shortcuts
def test_autostart_refuses_a_folder_with_a_percent_sign(autostart, tmp_path: Path) -> None:
    project, startup, run = autostart
    # The shortcut fills in %NAME% in its target, so it would point elsewhere and the app would never start.
    renamed = project.rename(tmp_path / "影片 %USERNAME% 備份")
    result = subprocess.run(
        # Run from inside the folder: cmd would fill in a %NAME% in its own command line too.
        r"cmd /d /c .\autostart.bat",
        cwd=renamed,
        env={**os.environ, "APPDATA": str(startup.parents[4])},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "rename the folder" in result.stdout.lower()
    assert not startup.exists() or not list(startup.iterdir())


@windows_only
@needs_shortcuts
def test_autostart_refuses_a_folder_too_deep_for_a_shortcut(autostart, tmp_path: Path) -> None:
    project, startup, run = autostart
    # The shortcut object quietly cuts a target past 259 characters, so the app would never start. The folder is made
    # just long enough for the target, while the script's own path still fits what cmd can run.
    deep = tmp_path / ("x" * max(240 - len(str(tmp_path)) - 1, 1))
    renamed = project.rename(deep)
    assert len(str(renamed / ".venv" / "Scripts" / "pythonw.exe")) > 259
    result = subprocess.run(
        f'cmd /d /c ""{renamed / "autostart.bat"}""',
        cwd=tmp_path,
        env={**os.environ, "APPDATA": str(startup.parents[4])},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert "too long" in result.stdout.lower() and "shorter path" in result.stdout.lower()
    assert not startup.exists() or not list(startup.iterdir())


@windows_only
@needs_shortcuts
def test_autostart_asks_for_start_bat_first(autostart) -> None:
    project, startup, run = autostart
    shutil.rmtree(project / ".venv")
    result = run()
    assert result.returncode != 0
    assert "start.bat" in result.stdout
    assert not startup.exists() or not list(startup.iterdir())


# The only port these tests start a server on.
LAUNCHER_TEST_PORT = 8646


def test_launcher_starts_the_engine_before_anyone_opens_a_page(tmp_path: Path) -> None:
    from videocap.services import COOKIE_DIR_PREFIX, STALE_COOKIE_DIR_SECONDS

    # The engine's first act is visible from outside: it clears logins left behind by an app that was killed.
    temp = tmp_path / "temp"
    left_behind = temp / f"{COOKIE_DIR_PREFIX}left-behind"
    left_behind.mkdir(parents=True)
    long_ago = time.time() - 2 * STALE_COOKIE_DIR_SECONDS
    os.utime(left_behind, (long_ago, long_ago))
    env = {
        **os.environ,
        "TEMP": str(temp),
        "TMP": str(temp),
        "TMPDIR": str(temp),
        "VIDEOCAP_MODE": "local",
        "VIDEOCAP_DATA_DIR": str(tmp_path / "data"),
        "VIDEOCAP_OUTPUT_DIR": str(tmp_path / "out"),
    }
    command = [
        sys.executable, "-m", "videocap", "--server.headless", "true", "--server.address", "127.0.0.1",
        "--server.port", str(LAUNCHER_TEST_PORT), "--server.fileWatcherType", "none",
    ]  # fmt: skip
    output = tmp_path / "server.log"
    with output.open("wb") as log:
        server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        health = f"http://127.0.0.1:{LAUNCHER_TEST_PORT}/_stcore/health"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.time() + 90
        while True:
            try:
                with opener.open(health, timeout=2) as response:
                    assert response.read() == b"ok"
                break
            except OSError:
                log_text = output.read_text(encoding="utf-8", errors="replace")
                assert server.poll() is None and time.time() < deadline, log_text
                time.sleep(0.5)
        assert not left_behind.exists()
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=20)


def test_first_launch_does_not_stop_at_streamlits_email_prompt(tmp_path: Path) -> None:
    # The launchers open the browser (headless false), and a new computer has no ~/.streamlit/credentials.toml.
    home = tmp_path / "home"
    home.mkdir()
    code = (
        "from streamlit.web import bootstrap\n"
        "from streamlit.runtime.credentials import check_credentials\n"
        "bootstrap.load_config_options({'server.headless': False})\n"
        "check_credentials()\n"
        "print('server would start now')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "HOME": str(home), "USERPROFILE": str(home)},
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert "Email" not in result.stdout
    assert "server would start now" in result.stdout, result.stdout + result.stderr


def test_linux_gets_the_keyring_reader_for_chromium_logins() -> None:
    from packaging.requirements import Requirement

    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    requirements = [Requirement(line) for line in lines if line.strip() and not line.startswith("#")]

    def names(platform: str) -> set[str]:
        environment = {"sys_platform": platform, "platform_system": platform.capitalize()}
        return {
            requirement.name.lower()
            for requirement in requirements
            if requirement.marker is None or requirement.marker.evaluate(environment)
        }

    assert "secretstorage" in names("linux")
    assert "secretstorage" not in names("win32")


def read_jsonc(path: Path) -> dict:
    text = re.sub(r"^\s*//.*$", "", path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    return json.loads(text)


def test_dev_container_keeps_streamlits_cross_site_protection() -> None:
    config = read_jsonc(ROOT / ".devcontainer" / "devcontainer.json")
    command = json.dumps(config.get("postAttachCommand", ""))
    assert "enableCORS false" not in command
    assert "enableXsrfProtection false" not in command
    assert "streamlit run main.py" in command


def test_container_starts_the_app_through_the_launcher() -> None:
    # Otherwise a restarted container checks no subscription until someone opens the page.
    lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    assert 'CMD ["python", "-m", "videocap"]' in lines


def test_compose_runs_in_taiwan_time_and_names_the_lan_addresses() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # Scheduled downloads and every time shown would otherwise be in UTC, 8 hours off.
    assert re.search(r'^ {6}TZ: "Asia/Taipei"$', compose, re.MULTILINE)
    assert re.search(r'^ {6}# VIDEOCAP_ALLOWED_HOSTS: "192\.168\.1\.20,nas\.local"$', compose, re.MULTILINE)


# Building the image takes minutes, so these run only when asked: VIDEOCAP_DOCKER_IMAGE=videocap:test pytest ...
DOCKER_IMAGE = os.environ.get("VIDEOCAP_DOCKER_IMAGE", "")
needs_image = pytest.mark.skipif(not DOCKER_IMAGE or not shutil.which("docker"), reason="set VIDEOCAP_DOCKER_IMAGE")


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300
    )


@needs_image
def test_container_can_save_into_a_downloads_folder_docker_created_as_root() -> None:
    # A tmpfs mount is root-owned, just like ./downloads when Docker creates it on a Linux host.
    result = docker(
        "run", "--rm", "--mount", "type=tmpfs,dst=/downloads", DOCKER_IMAGE,
        "sh", "-c", "mkdir /downloads/依頻道 && touch /downloads/依頻道/影片.mp4 && id -u",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1000"


@needs_image
def test_container_runs_as_the_owner_of_an_existing_downloads_folder() -> None:
    volume = "videocap-platform-test-downloads"
    docker("volume", "rm", "-f", volume)
    try:
        # Like a NAS share that already holds the user's files. Docker resets the owner of an empty volume.
        prepared = docker("run", "--rm", "--user", "0", "--entrypoint", "sh", "-v", f"{volume}:/downloads", DOCKER_IMAGE,
                          "-c", "touch /downloads/舊影片.mp4 && chown -R 1026:100 /downloads")
        assert prepared.returncode == 0, prepared.stderr
        result = docker("run", "--rm", "-v", f"{volume}:/downloads", DOCKER_IMAGE,
                        "sh", "-c", "touch /downloads/a && stat -c %u:%g /downloads/a && touch ~/.probe")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "1026:100"
    finally:
        docker("volume", "rm", "-f", volume)


@needs_image
def test_container_starts_the_engine_before_anyone_opens_a_page() -> None:
    from videocap.services import COOKIE_DIR_PREFIX

    # The image's own start command, run after planting a login that a killed app left behind two days ago.
    inspected = docker("image", "inspect", "-f", "{{json .Config.Cmd}}", DOCKER_IMAGE)
    assert inspected.returncode == 0, inspected.stderr
    left_behind = f"/tmp/{COOKIE_DIR_PREFIX}left-behind"
    plant = f"mkdir {left_behind} && chown 1000:1000 {left_behind} && touch -d '2 days ago' {left_behind}"
    started = docker("run", "-d", "--entrypoint", "sh", DOCKER_IMAGE,
                     "-c", f'{plant} && exec docker-entrypoint.sh "$@"', "sh", *json.loads(inspected.stdout))
    assert started.returncode == 0, started.stderr
    container = started.stdout.strip()
    try:
        health = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)"
        deadline = time.time() + 90
        while docker("exec", container, "python", "-c", health).returncode != 0:
            assert time.time() < deadline, docker("logs", container).stdout
            time.sleep(1)
        assert docker("exec", container, "test", "-e", left_behind).returncode == 1
    finally:
        docker("rm", "-f", container)


@needs_image
def test_container_follows_the_tz_setting() -> None:
    result = docker("run", "--rm", "-e", "TZ=Asia/Taipei", DOCKER_IMAGE, "python", "-c",
                    "import time; print(time.strftime('%z'))")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "+0800"


@needs_image
def test_container_has_a_font_for_chinese_subtitles() -> None:
    result = docker("run", "--rm", DOCKER_IMAGE, "fc-match", "-f", "%{family}", "Arial:charset=4e2d")
    assert result.returncode == 0, result.stderr
    assert "Noto Sans CJK TC" in result.stdout
