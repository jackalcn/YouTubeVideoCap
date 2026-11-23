"""Passing a site's check for a person yourself, in a real browser window.

Some sites turn a program away with a check that asks for a person: a box to tick, a puzzle. The app does not pass it
for anyone. What it can do is open a real browser, whichever of Chrome, Edge or Brave this machine has, in a profile of
its own at the link that was refused, and wait while the owner passes the check there. It then asks that browser,
through the debugging connection every Chromium-based browser offers on this machine's own address, for what the site
gave it (its cookies for that site) and for how it introduces itself (its User-Agent and client hints), and closes it.
Later requests to the site carry the same cookies and introduce themselves the same way, which is what its check was
about: see impersonation.youtube_dl.

The profile is new each time and removed after, so nothing of the owner's own browser is read, and of the cookies in it
only the site's are kept. What is kept stays in memory, until the app ends."""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from videocap.processes import end_process_tree, remove_when_released, start_tied_process
from videocap.sites import host_of, is_media_link


# Names a browser to open instead of the ones looked for below, by the path of its program.
BROWSER_ENV = "VIDEOCAP_BROWSER"
PROFILE_DIR_PREFIX = "videocap-verify-"
# A profile older than any window could be open: the app that made it was killed before it could clean up.
STALE_PROFILE_SECONDS = 24 * 60 * 60
# How long a window waits for the owner, how long the browser has to start, and how often the window looks at it.
WAIT_SECONDS = 10 * 60
START_SECONDS = 20.0
POLL_SECONDS = 1.0
# Once the site's cookie for a passed check shows, the page it leads to is let load, which may set more cookies.
SETTLE_SECONDS = 3.0
# What Cloudflare's check leaves in the browser that passed it. Other sites' checks leave other cookies, which the
# owner tells the window about by saying it is passed.
CLEARANCE_COOKIE = "cf_clearance"
CHROME_VERSION_RE = re.compile(r"Chrome/(\d+)")

STARTING = "starting"
WAITING = "waiting"
PASSED = "passed"
CLOSED = "closed"
CANCELED = "canceled"
TIMED_OUT = "timed_out"
FAILED = "failed"
ACTIVE_STATES = frozenset({STARTING, WAITING})

# The browsers looked for, the first found being the one opened. Each has to be one whose debugging connection is the
# protocol below: Firefox, Safari and the like are not.
WINDOWS_BROWSERS = (
    ("Chrome", r"Google\Chrome\Application\chrome.exe"),
    ("Edge", r"Microsoft\Edge\Application\msedge.exe"),
    ("Brave", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
    ("Chromium", r"Chromium\Application\chrome.exe"),
)
MACOS_BROWSERS = (
    ("Chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ("Edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ("Brave", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
    ("Chromium", "/Applications/Chromium.app/Contents/MacOS/Chromium"),
)
LINUX_BROWSERS = (
    ("Chrome", ("google-chrome", "google-chrome-stable")),
    ("Edge", ("microsoft-edge", "microsoft-edge-stable")),
    ("Brave", ("brave-browser", "brave")),
    ("Chromium", ("chromium", "chromium-browser")),
)
# What is asked of the browser's page, to know the client hints it sends with every request.
HINTS_SCRIPT = (
    "JSON.stringify(navigator.userAgentData ? "
    "{brands: navigator.userAgentData.brands, mobile: navigator.userAgentData.mobile, platform: navigator.userAgentData.platform} : null)"
)
NO_BROWSER_MESSAGE = "這台電腦找不到 Chrome、Edge 或 Brave，請先安裝其中一個（也可以用環境變數 VIDEOCAP_BROWSER 指定瀏覽器的程式）。"
NO_DESKTOP_MESSAGE = "這台電腦沒有桌面畫面，開不了瀏覽器視窗。"
NO_WEBSOCKETS_MESSAGE = "缺少 websockets 套件，請執行：python -m pip install \"yt-dlp[default]\""
BUSY_MESSAGE = "已經有一個驗證視窗開著，請先完成或取消它。"
NOT_A_WEB_ADDRESS = "只有網頁的網址（http:// 或 https:// 開頭）才能在瀏覽器視窗裡開啟。"
# The kinds of proxy a browser can be told to go through. It speaks the others' names too, but only under these.
PROXY_SCHEMES = ("http", "https", "socks4", "socks5")
PROXY_SCHEME_ALIASES = {"socks5h": "socks5", "socks4a": "socks4"}


class VerificationUnavailable(Exception):
    """No window can be opened now; the message says why."""


class DevToolsError(Exception):
    """The browser answered a command with an error, or not at all."""


@dataclass(frozen=True)
class Browser:
    name: str
    path: Path


def known_browsers() -> list[Browser]:
    found: list[Browser] = []
    chosen = os.environ.get(BROWSER_ENV, "").strip()
    if chosen and Path(chosen).expanduser().is_file():
        found.append(Browser(Path(chosen).stem, Path(chosen).expanduser()))
    if sys.platform == "win32":
        roots = [os.environ.get(name, "") for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        found += [Browser(name, Path(root) / relative) for name, relative in WINDOWS_BROWSERS for root in roots if root and (Path(root) / relative).is_file()]
    elif sys.platform == "darwin":
        found += [Browser(name, Path(path)) for name, path in MACOS_BROWSERS if Path(path).is_file()]
    else:
        for name, commands in LINUX_BROWSERS:
            located = next((shutil.which(command) for command in commands if shutil.which(command)), None)
            if located:
                found.append(Browser(name, Path(located)))
    return found


def find_browser() -> Optional[Browser]:
    return next(iter(known_browsers()), None)


def unavailable_reason() -> str:
    """Why no window can be opened on this machine, or "" when one can."""
    if find_browser() is None:
        return NO_BROWSER_MESSAGE
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return NO_DESKTOP_MESSAGE
    try:
        import websockets.sync.client  # noqa: F401
    except ImportError:
        return NO_WEBSOCKETS_MESSAGE
    return ""


@dataclass(frozen=True)
class SiteCookie:
    name: str
    value: str
    domain: str
    path: str = "/"
    # Seconds since 1970, or None for a cookie that lasts until the browser closes.
    expires: Optional[float] = None
    secure: bool = False
    http_only: bool = False

    def applies_to(self, host: str) -> bool:
        domain = self.domain.lower()
        return host == domain.lstrip(".") or (domain.startswith(".") and host.endswith(domain))

    def expired(self, now: float) -> bool:
        return self.expires is not None and self.expires <= now

    def as_cookie(self) -> http.cookiejar.Cookie:
        return http.cookiejar.Cookie(
            version=0,
            name=self.name,
            value=self.value,
            port=None,
            port_specified=False,
            domain=self.domain,
            domain_specified=True,
            domain_initial_dot=self.domain.startswith("."),
            path=self.path or "/",
            path_specified=True,
            secure=self.secure,
            expires=int(self.expires) if self.expires is not None else None,
            discard=self.expires is None,
            comment=None,
            comment_url=None,
            rest={"HttpOnly": ""} if self.http_only else {},
            rfc2109=False,
        )


@dataclass(frozen=True)
class VerifiedSession:
    """What a browser was given by a site whose check its owner passed, and how it introduced itself."""

    host: str
    cookies: tuple[SiteCookie, ...]
    user_agent: str = ""
    # Sec-Ch-Ua and the like, as (name, value) pairs in the order the browser sends them.
    client_hints: tuple[tuple[str, str], ...] = ()
    browser: str = ""
    captured_at: float = 0.0

    def live_cookies(self, now: Optional[float] = None) -> list[SiteCookie]:
        now = time.time() if now is None else now
        return [cookie for cookie in self.cookies if not cookie.expired(now)]

    def usable(self, now: Optional[float] = None) -> bool:
        """Whether there is still something to ask the site with. A clearance that ran out is the check, again."""
        live = self.live_cookies(now)
        had_clearance = any(cookie.name == CLEARANCE_COOKIE for cookie in self.cookies)
        return bool(live) and (not had_clearance or any(cookie.name == CLEARANCE_COOKIE for cookie in live))

    def covers(self, url: str, now: Optional[float] = None) -> bool:
        host = host_of(url)
        return bool(host) and any(cookie.applies_to(host) for cookie in self.live_cookies(now)) and self.usable(now)

    def headers(self) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent} if self.user_agent else {}
        return {**headers, **dict(self.client_hints)}

    @property
    def browser_major(self) -> Optional[int]:
        match = CHROME_VERSION_RE.search(self.user_agent)
        return int(match.group(1)) if match else None


class VerifiedSites:
    """The sites whose check was passed, each with its session. Kept in memory only, and shared by every request of the
    app, which is the owner's."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, VerifiedSession] = {}

    def add(self, session: VerifiedSession) -> None:
        with self._lock:
            self._sessions[session.host] = session

    def forget(self, host: str) -> None:
        with self._lock:
            self._sessions.pop(host, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def all(self, now: Optional[float] = None) -> list[VerifiedSession]:
        with self._lock:
            return [session for session in self._sessions.values() if session.usable(now)]

    def session_for(self, url: str, now: Optional[float] = None) -> Optional[VerifiedSession]:
        with self._lock:
            sessions = list(self._sessions.values())
        return next((session for session in sessions if session.covers(url, now)), None)


verified_sites = VerifiedSites()


class DevTools:
    """The least of the browser's debugging protocol that a window needs: a command, and the answer to it."""

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        from websockets.sync.client import connect

        self.timeout = timeout
        # No proxy: the address is this machine's own, and a system proxy would not know it.
        self._socket = connect(url, proxy=None, max_size=None, open_timeout=timeout)
        self._last = 0

    def call(self, method: str, params: Optional[dict[str, Any]] = None, session: str = "") -> dict[str, Any]:
        self._last += 1
        message: dict[str, Any] = {"id": self._last, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self._socket.send(json.dumps(message))
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DevToolsError(f"{method}: no answer")
            try:
                reply = json.loads(self._socket.recv(timeout=remaining))
            except TimeoutError:
                raise DevToolsError(f"{method}: no answer") from None
            # What else the browser says, such as events, is not what was asked for.
            if reply.get("id") != self._last:
                continue
            if "error" in reply:
                raise DevToolsError(f"{method}: {reply['error'].get('message') or reply['error']}")
            return dict(reply.get("result") or {})

    def close(self) -> None:
        try:
            self._socket.close()
        except Exception:
            pass


def opening_address(url: str) -> str:
    """Where the window opens: the page that was refused, or the site's front page when it was a file or a stream, which
    a browser would only download."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/" if is_media_link(url) else url


def browser_proxy(proxy: str) -> str:
    """The proxy the app's own requests go through, as a browser's --proxy-server takes it, or "" when there is none or
    a browser cannot use it. A check is passed from the address the site sees, and later requests are to come from the
    same one. A login in the proxy's address stays out of the command line, where every program of the machine can read
    it: the browser asks for it itself."""
    text = proxy.strip()
    if not text:
        return ""
    parts = urlsplit(text if "://" in text else f"http://{text}")
    scheme = PROXY_SCHEME_ALIASES.get(parts.scheme.lower(), parts.scheme.lower())
    try:
        port = parts.port
    except ValueError:
        return ""
    if scheme not in PROXY_SCHEMES or not parts.hostname:
        return ""
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{scheme}://{host}" + (f":{port}" if port else "")


def remove_stale_profiles() -> None:
    cutoff = time.time() - STALE_PROFILE_SECONDS
    for folder in Path(tempfile.gettempdir()).glob(f"{PROFILE_DIR_PREFIX}*"):
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            continue


def launch(browser: Browser, address: str, profile: Path, proxy: str = "") -> subprocess.Popen[Any]:
    command = [
        str(browser.path),
        f"--user-data-dir={profile}",
        # Port 0 lets the browser pick one, and write it in the profile for the window to read.
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--password-store=basic",
        "--use-mock-keychain",
        "--window-size=1000,800",
    ]
    if proxy:
        command.append(f"--proxy-server={proxy}")
    command.append(address)
    return start_tied_process(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def read_endpoint(profile: Path, process: subprocess.Popen[Any], timeout: float = START_SECONDS) -> str:
    """The address of the browser's debugging connection, which it writes in its profile once it listens."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise DevToolsError("the browser ended as it started")
        try:
            lines = (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        if len(lines) >= 2 and lines[0].strip().isdigit():
            return f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
        time.sleep(0.1)
    raise DevToolsError("the browser did not open its debugging connection")


def client_hints(devtools: DevTools) -> tuple[tuple[str, str], ...]:
    """The Sec-Ch-Ua headers the browser sends, read from the page it has open. Nothing when it cannot be asked."""
    try:
        targets = devtools.call("Target.getTargets").get("targetInfos", [])
        page = next(target for target in targets if target.get("type") == "page" and str(target.get("url", "")).startswith("http"))
        session = devtools.call("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})["sessionId"]
        try:
            answer = devtools.call("Runtime.evaluate", {"expression": HINTS_SCRIPT, "returnByValue": True}, session)
        finally:
            try:
                devtools.call("Target.detachFromTarget", {"sessionId": session})
            except DevToolsError:
                pass
        data = json.loads(answer["result"]["value"] or "null")
    except (DevToolsError, StopIteration, KeyError, ValueError, TypeError):
        return ()
    if not isinstance(data, dict) or not data.get("brands"):
        return ()
    brands = ", ".join(f'"{brand["brand"]}";v="{brand["version"]}"' for brand in data["brands"])
    hints = [("Sec-Ch-Ua", brands), ("Sec-Ch-Ua-Mobile", "?1" if data.get("mobile") else "?0")]
    if data.get("platform"):
        hints.append(("Sec-Ch-Ua-Platform", f'"{data["platform"]}"'))
    return tuple(hints)


def site_cookies(devtools: DevTools, host: str) -> tuple[SiteCookie, ...]:
    """The browser's cookies that the site at this host is sent, and no others."""
    found = []
    for raw in devtools.call("Storage.getCookies").get("cookies", []):
        expires = raw.get("expires")
        cookie = SiteCookie(
            name=str(raw.get("name", "")),
            value=str(raw.get("value", "")),
            domain=str(raw.get("domain", "")),
            path=str(raw.get("path", "/")),
            expires=float(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
            secure=bool(raw.get("secure")),
            http_only=bool(raw.get("httpOnly")),
        )
        if cookie.name and cookie.applies_to(host):
            found.append(cookie)
    return tuple(found)


def capture(devtools: DevTools, host: str, browser: str) -> VerifiedSession:
    return VerifiedSession(
        host=host,
        cookies=site_cookies(devtools, host),
        user_agent=str(devtools.call("Browser.getVersion").get("userAgent", "")),
        client_hints=client_hints(devtools),
        browser=browser,
        captured_at=time.time(),
    )


@dataclass(frozen=True)
class WindowStatus:
    """What a window is doing, at one moment."""

    state: str
    message: str
    host: str
    address: str
    page_title: str = ""
    seconds_left: int = 0
    session: Optional[VerifiedSession] = None

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES


class VerificationWindow:
    """One browser window for the owner to pass a site's check in. It runs on a thread of its own, and ends in one of
    PASSED, CLOSED, CANCELED, TIMED_OUT or FAILED, having closed the browser and removed its profile."""

    def __init__(self, url: str, browser: Browser, sites: VerifiedSites, proxy: str = "") -> None:
        self.address = opening_address(url)
        self.host = host_of(self.address)
        self.browser = browser
        self.sites = sites
        self.proxy = browser_proxy(proxy)
        self._lock = threading.Lock()
        self._state = STARTING
        self._message = "正在開啟瀏覽器…"
        self._title = ""
        self._session: Optional[VerifiedSession] = None
        self._deadline = 0.0
        self._wake = threading.Event()
        self._finish_asked = False
        self._cancel_asked = False
        self._thread = threading.Thread(target=self._run, name="videocap-verification", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout)

    def finish(self) -> None:
        """The owner says the check is passed, for a site whose check leaves no cookie the window knows."""
        self._finish_asked = True
        self._wake.set()

    def cancel(self) -> None:
        self._cancel_asked = True
        self._wake.set()

    def status(self) -> WindowStatus:
        with self._lock:
            left = max(int(self._deadline - time.monotonic()), 0) if self._state == WAITING else 0
            return WindowStatus(self._state, self._message, self.host, self.address, self._title, left, self._session)

    def _set(self, state: str, message: str) -> None:
        with self._lock:
            self._state, self._message = state, message

    def _run(self) -> None:
        profile = Path(tempfile.mkdtemp(prefix=PROFILE_DIR_PREFIX))
        process: Optional[subprocess.Popen[Any]] = None
        devtools: Optional[DevTools] = None
        try:
            process = launch(self.browser, self.address, profile, self.proxy)
            devtools = DevTools(read_endpoint(profile, process))
            with self._lock:
                self._deadline = time.monotonic() + WAIT_SECONDS
            self._set(WAITING, f"請在剛開啟的{self.browser.name}視窗裡通過驗證（例如勾選「我不是機器人」）。通過後這裡會自動偵測，視窗也會自動關閉。")
            self._watch(process, devtools)
        except Exception as error:
            # Ended while the owner was at it, the window was closed; ended before, the browser did not start.
            if self._state == WAITING and process is not None and process.poll() is not None:
                self._set(CLOSED, "瀏覽器視窗被關閉了，沒有收到驗證。")
            else:
                self._set(FAILED, f"瀏覽器視窗沒有順利開啟：{error}")
        finally:
            self._close(process, devtools, profile)

    def _watch(self, process: subprocess.Popen[Any], devtools: DevTools) -> None:
        settle_until: Optional[float] = None
        while True:
            if self._cancel_asked:
                self._set(CANCELED, "已取消。")
                return
            if process.poll() is not None:
                self._set(CLOSED, "瀏覽器視窗被關閉了，沒有收到驗證。")
                return
            if time.monotonic() > self._deadline:
                self._set(TIMED_OUT, "等太久了，視窗已關閉。需要的話請再開一次。")
                return
            try:
                self._look(devtools)
                cleared = any(cookie.name == CLEARANCE_COOKIE for cookie in site_cookies(devtools, self.host))
            except Exception:
                # The browser went away between the check above and the question.
                if process.poll() is not None:
                    self._set(CLOSED, "瀏覽器視窗被關閉了，沒有收到驗證。")
                    return
                raise
            if cleared and settle_until is None:
                settle_until = time.monotonic() + SETTLE_SECONDS
            if self._finish_asked or (settle_until is not None and time.monotonic() >= settle_until):
                session = capture(devtools, self.host, self.browser.name)
                if session.usable():
                    self.sites.add(session)
                    with self._lock:
                        self._session = session
                    self._set(PASSED, f"已通過驗證：{self.host}。現在可以回到下載頁，再試一次。")
                else:
                    self._set(FAILED, f"沒有收到 {self.host} 的 cookies，驗證可能還沒通過。請再開一次視窗。")
                return
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def _look(self, devtools: DevTools) -> None:
        targets = devtools.call("Target.getTargets").get("targetInfos", [])
        titles = [str(target.get("title") or "") for target in targets if target.get("type") == "page"]
        with self._lock:
            self._title = titles[0] if titles else ""

    def _close(self, process: Optional[subprocess.Popen[Any]], devtools: Optional[DevTools], profile: Path) -> None:
        if devtools is not None:
            try:
                devtools.call("Browser.close")
            except Exception:
                pass
            devtools.close()
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                end_process_tree(process)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        remove_when_released(profile, timeout=5.0)


_current_lock = threading.Lock()
_current: Optional[VerificationWindow] = None


def current_window() -> Optional[VerificationWindow]:
    """The window that is open, or else the one that last was, for the page to say how it ended."""
    return _current


def start_window(url: str, sites: Optional[VerifiedSites] = None, proxy: str = "") -> VerificationWindow:
    """Opens a window at this link, going through the proxy the app's own requests go through, if it has one. Raises
    VerificationUnavailable, with the reason, when none can be opened now."""
    global _current
    # What the browser is given as its page: nothing that it could read as one of its own options, or a file of this machine.
    if not opening_address(url).lower().startswith(("http://", "https://")) or not host_of(url):
        raise VerificationUnavailable(NOT_A_WEB_ADDRESS)
    reason = unavailable_reason()
    if reason:
        raise VerificationUnavailable(reason)
    browser = find_browser()
    assert browser is not None
    with _current_lock:
        if _current is not None and _current.status().active:
            if _current.host == host_of(opening_address(url)):
                return _current
            raise VerificationUnavailable(BUSY_MESSAGE)
        remove_stale_profiles()
        _current = VerificationWindow(url, browser, sites if sites is not None else verified_sites, proxy)
        _current.start()
        return _current


def forget_window() -> None:
    """Drops the memory of the last window that ended, once the page has said how it went."""
    global _current
    with _current_lock:
        if _current is not None and not _current.status().active:
            _current = None
