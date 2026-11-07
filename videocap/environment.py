from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from videocap.config import DEPLOY_VERIFY_MIN_COMMIT


TRUE_VALUES = {"1", "true", "yes", "on"}
# A hidden copy started at sign-in must not flash a console window for every tool it runs.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
APP_DIR = Path(__file__).resolve().parents[1]
# The same shortcut autostart.bat makes, so the 系統 page and the script each see what the other did.
AUTOSTART_SHORTCUT_NAME = "YouTube VideoCap.lnk"
# Only this computer can connect, whatever else is configured, since nobody sees this copy run.
AUTOSTART_ARGUMENTS = "-m videocap --server.headless true --server.address localhost"
# PowerShell reads the paths from the environment, so quotes or brackets in a folder name cannot break the command.
# The shortcut object replaces a %NAME% in the target with that variable's value, which cannot be escaped, so the
# saved target is read back and a wrong one removed.
CREATE_SHORTCUT_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; $shell = New-Object -ComObject WScript.Shell; "
    "$s = $shell.CreateShortcut($env:VIDEOCAP_SHORTCUT); "
    "$s.TargetPath = $env:VIDEOCAP_TARGET; $s.Arguments = $env:VIDEOCAP_ARGUMENTS; "
    "$s.WorkingDirectory = $env:VIDEOCAP_APP_DIR; $s.Save(); "
    "if ($shell.CreateShortcut($env:VIDEOCAP_SHORTCUT).TargetPath -cne $env:VIDEOCAP_TARGET) "
    "{ Remove-Item -LiteralPath $env:VIDEOCAP_SHORTCUT; exit 3 }"
)
SHORTCUT_TARGET_CHANGED = 3
PERCENT_IN_FOLDER_MESSAGE = (
    "App 所在的資料夾路徑裡有「%」，Windows 的捷徑會把它當成變數而指到別的地方。請把資料夾改名、拿掉「%」後再開啟。"
)
TARGET_CHANGED_MESSAGE = "Windows 記下的捷徑目標和 App 的位置不同，所以沒有建立捷徑。請把 App 移到較短、沒有特殊符號的路徑後再開啟。"
# Windows' MAX_PATH less the terminator. A longer target is silently cut or changed by the shortcut object.
MAX_SHORTCUT_TARGET_LENGTH = 259


def in_docker() -> bool:
    """Set by the Dockerfile. Restarting there means restarting the container, not closing a window."""
    return os.getenv("VIDEOCAP_IN_DOCKER", "") == "1"


def is_streamlit_cloud_environment() -> bool:
    mode = os.getenv("VIDEOCAP_MODE", "").strip().lower()
    if mode in {"cloud", "public"}:
        return True
    if mode == "local":
        return False

    markers = [
        os.getenv("STREAMLIT_RUNTIME_ENV", ""),
        os.getenv("STREAMLIT_SHARING_MODE", ""),
        os.getenv("IS_STREAMLIT_CLOUD", ""),
        os.getenv("STREAMLIT_CLOUD", ""),
        os.getenv("STREAMLIT_ENV", ""),
        os.getenv("ENVIRONMENT", ""),
    ]
    normalized = [value.strip().lower() for value in markers if value]
    if any(value in {"cloud", "true", "1", "sharing", "production", "prod"} for value in normalized):
        return True

    host_markers = [
        os.getenv("HOSTNAME", "").lower(),
        os.getenv("HOST", "").lower(),
        os.getenv("STREAMLIT_SERVER_ADDRESS", "").lower(),
    ]
    if any("streamlit.app" in marker for marker in host_markers if marker):
        return True

    # Streamlit Cloud commonly mounts repositories under /mount/src/<repo>.
    cwd_text = str(Path.cwd()).replace("\\", "/").lower()
    return cwd_text.startswith("/mount/src")


def remote_visitors_are_trusted() -> bool:
    """For a private install reached over the network, such as Docker, whose visitors never look like localhost."""
    return os.getenv("VIDEOCAP_TRUST_REMOTE", "").strip().lower() in TRUE_VALUES


def detect_runtime_commit_short() -> str:
    env_keys = [
        "SOURCE_COMMIT",
        "GITHUB_SHA",
        "GIT_COMMIT",
        "COMMIT_SHA",
        "STREAMLIT_GIT_SHA",
    ]

    for key in env_keys:
        value = os.getenv(key, "").strip()
        if value:
            return value[:7]

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
            creationflags=NO_WINDOW,
        )
        commit_value = result.stdout.strip()
        if result.returncode == 0 and commit_value:
            return commit_value[:7]
    except Exception:
        pass

    return DEPLOY_VERIFY_MIN_COMMIT


# Starting with Windows


def autostart_supported() -> bool:
    return sys.platform == "win32"


def startup_folder() -> Path:
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def autostart_shortcut() -> Path:
    return startup_folder() / AUTOSTART_SHORTCUT_NAME


def autostart_enabled() -> bool:
    return autostart_supported() and autostart_shortcut().exists()


def enable_autostart(app_dir: Optional[Path] = None) -> str:
    """Puts a shortcut in the Startup folder that starts the app hidden at each sign-in, as autostart.bat does.
    Returns a problem for the person, or ""."""
    if not autostart_supported():
        return "開機自動啟動只支援 Windows。"
    app_dir = app_dir or APP_DIR
    pythonw = app_dir / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        return "找不到 .venv 裡的 pythonw.exe。請先執行一次 start.bat 完成安裝，再開啟開機自動啟動。"
    if len(str(pythonw)) > MAX_SHORTCUT_TARGET_LENGTH:
        return (
            f"App 所在的資料夾路徑太長（{len(str(pythonw))} 個字元），Windows 的捷徑最多只能記 {MAX_SHORTCUT_TARGET_LENGTH} 個。"
            "請把 App 移到較短的路徑後再開啟。"
        )
    shortcut = autostart_shortcut()
    environment = {
        **os.environ,
        "VIDEOCAP_SHORTCUT": str(shortcut),
        "VIDEOCAP_TARGET": str(pythonw),
        "VIDEOCAP_ARGUMENTS": AUTOSTART_ARGUMENTS,
        "VIDEOCAP_APP_DIR": str(app_dir),
    }
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    try:
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", CREATE_SHORTCUT_SCRIPT],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env=environment,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"無法在「啟動」資料夾建立捷徑：{exc}"
    # A %NAME% the shortcut object could not fill in at all makes Save fail with a message about a value's range.
    if result.returncode == SHORTCUT_TARGET_CHANGED or (result.returncode != 0 and "%" in str(pythonw)):
        shortcut.unlink(missing_ok=True)
        return PERCENT_IN_FOLDER_MESSAGE if "%" in str(pythonw) else TARGET_CHANGED_MESSAGE
    if result.returncode != 0 or not shortcut.exists():
        details = (result.stderr or result.stdout or "").strip().splitlines()
        return "無法在「啟動」資料夾建立捷徑" + (f"：{details[0][:200]}" if details else "。")
    return ""


def disable_autostart() -> str:
    """Returns a problem for the person, or "". A copy already running keeps running until sign-out."""
    try:
        autostart_shortcut().unlink(missing_ok=True)
    except OSError as exc:
        return f"無法移除「啟動」資料夾裡的捷徑：{exc}"
    return ""
