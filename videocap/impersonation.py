"""Passing for a browser, for the sites that refuse anything else.

Some sites, mostly behind Cloudflare, answer HTTP 403 to a program and let a browser through, telling the two apart by
how the connection is made. With the curl-cffi package yt-dlp can connect the way a browser does. The 系統 page
installs that package for this machine's owner.

YouTube must never be reached that way: its subtitle addresses ask for impersonation without needing it, and on
Windows that once made subtitle downloads fail. yt-dlp has no option that keeps one site away from it, so:

- yt-dlp's handler for it is taken out of the list every YoutubeDL is built from, and kept here. A YoutubeDL made
  anywhere in the app is then exactly what it is without the package.
- Only the owner's link to another site gets a YoutubeDL that has the handler: youtube_dl below.
- Even there the handler hands a request for one of YouTube's own hosts back to the ordinary handlers, for a page on
  another site that plays a YouTube video.

The first of these reaches into yt-dlp, which a release may rearrange. So an ordinary YoutubeDL is asked whether it
can impersonate. When it can, or cannot be asked, the handler is given to no one and 模擬瀏覽器 is switched off: the
next start then keeps the package from yt-dlp in a way that needs nothing inside it (videocap/__init__.py)."""

from __future__ import annotations

import functools
import importlib
import importlib.metadata
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from yt_dlp.YoutubeDL import YoutubeDL as PlainYoutubeDL

from videocap import impersonation_switch
from videocap.sites import host_of, is_youtube_host, is_youtube_url
from videocap.verification import VerifiedSession, verified_sites

try:
    from yt_dlp.networking.common import _REQUEST_HANDLERS, _RH_PREFERENCES
    from yt_dlp.networking.exceptions import HTTPError, UnsupportedRequest
    from yt_dlp.networking.impersonate import ImpersonateRequestHandler, ImpersonateTarget

    MISSING_FROM_YTDLP = ""
except ImportError as missing:
    # A later yt-dlp moved what this module leans on. The app still has to open.
    MISSING_FROM_YTDLP = str(missing)


PACKAGE = "curl-cffi"
MODULE = "curl_cffi"
# pip installs the version this yt-dlp asks for, whichever yt-dlp that is, so no version is written here.
INSTALL_TARGET = "yt-dlp[curl-cffi]"
# What the switch file says of who switched it off.
BY_OWNER = "owner"
BY_CHECK = "check"
HTTP_FORBIDDEN = 403
# Cloudflare's header on the page that asks a visitor to prove they are a person.
CHALLENGE_HEADER = "cf-mitigated"
CHALLENGE_VALUE = "challenge"
# Where YouTube's pages, API, streams and pictures are served from, and Google's sign-in and consent pages.
YOUTUBE_REQUEST_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "youtubekids.com",
    "googlevideo.com",
    "ytimg.com",
    "ggpht.com",
    "googleusercontent.com",
    "googleapis.com",
    "google.com",
)

INSTALL_BUTTON_LABEL = "安裝模擬瀏覽器（下載不到 10 MB）"
# What this machine's owner is told to do when a site wants a browser and the app cannot pass for one.
INSTALL_ADVICE = "請到「系統」頁安裝模擬瀏覽器後再試。"
SWITCHED_OFF_ADVICE = "模擬瀏覽器目前是停用的，請到「系統」頁啟用後再試。"
UNSUPPORTED_ADVICE = "已安裝的模擬瀏覽器和目前的 yt-dlp 不相容，請到「系統」頁重新安裝後再試。"
RESTART_ADVICE = "模擬瀏覽器已經安裝，重新啟動 App 後才能使用。"
NOT_SEPARATE_MESSAGE = "模擬瀏覽器沒有和 YouTube 的連線隔開（可能是 yt-dlp 更新後內部結構變了），YouTube 的字幕下載可能受影響。App 已自動把它停用，請重新啟動 App。"


@dataclass
class Refusals:
    """What a site refused a run with, as the answers themselves said it. yt-dlp's messages do not: a stream's
    refused segments end as "the downloaded file is empty", and a check for a person as one more HTTP 403."""

    # How many requests were answered HTTP 403.
    forbidden: int = 0
    # One of them with Cloudflare's challenge header: the site wants a person to pass its check.
    challenged: bool = False
    # The first address that was answered with that check: the site whose check it is, which need not be the link's.
    challenged_at: str = ""
    # The addresses that were answered so, which tell a refused page from a refused stream.
    addresses: list[str] = field(default_factory=list)

    def note(self, error: Any) -> None:
        if error.status != HTTP_FORBIDDEN:
            return
        self.forbidden += 1
        address = str(error.response.url)
        if is_challenge(error):
            self.challenged = True
            self.challenged_at = self.challenged_at or address
        self.addresses.append(address)


class Silent:
    def debug(self, message: str) -> None: ...
    def info(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


_lock = threading.RLock()
# yt-dlp's handlers that can pass for a browser, by yt-dlp's name for each, once taken out of yt-dlp's own list.
_taken: dict[str, Any] = {}
# What a YoutubeDL for another site is given: each of those, made to leave YouTube alone. Empty when there is none,
# when 模擬瀏覽器 is switched off, and when the check below failed.
_kept: tuple[Any, ...] = ()
# What the check found wrong in this process. Until a restart, an ordinary YoutubeDL may then pass for a browser.
_problem = ""
# Why yt-dlp would not take the installed package: its version, mostly.
_unsupported = ""


def installed_version() -> str:
    """The curl-cffi on disk, or "". Asked of the package's own record, because the module can be kept from loading."""
    try:
        return importlib.metadata.version(PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return ""


def ready() -> bool:
    """Whether a link to another site can be fetched as a browser would."""
    return bool(_kept)


def problem() -> str:
    return _problem


def advice(other_sites: bool) -> str:
    """What to do about a site that lets only a browser in, for errors.humanize_ytdlp_error. "" for a visitor, who
    gets no other site and has no 系統 page to install from, and "" once the app can pass for a browser."""
    if not other_sites or _kept:
        return ""
    if not installed_version():
        return INSTALL_ADVICE
    if switched_off()[0]:
        return SWITCHED_OFF_ADVICE
    return UNSUPPORTED_ADVICE if _unsupported else RESTART_ADVICE


def unsupported() -> str:
    return _unsupported


def switched_off() -> tuple[str, str]:
    """Who switched 模擬瀏覽器 off, BY_OWNER or BY_CHECK, and what the check had found. ("", "") while it is on."""
    try:
        by, _, why = impersonation_switch().read_text(encoding="utf-8").strip().partition(" ")
    except OSError:
        return "", ""
    return (BY_CHECK, why) if by == BY_CHECK else (BY_OWNER, "")


def write_switch(by: str, why: str = "") -> None:
    switch = impersonation_switch()
    switch.parent.mkdir(parents=True, exist_ok=True)
    switch.write_text(f"{by} {why}".strip() + "\n", encoding="utf-8")


def switch_off() -> None:
    """From now on no YoutubeDL is given the handler, and from the next start yt-dlp does not find the package."""
    write_switch(BY_OWNER)
    load()


def switch_on() -> None:
    impersonation_switch().unlink(missing_ok=True)
    load()


def any_browser() -> Any:
    """What yt-dlp's "impersonate" option takes for "whichever browser the handler does best"."""
    return ImpersonateTarget()


def goes_to_youtube(url: str) -> bool:
    host = host_of(url)
    return is_youtube_host(host) or any(host == domain or host.endswith(f".{domain}") for domain in YOUTUBE_REQUEST_DOMAINS)


def is_challenge(error: Any) -> bool:
    try:
        header = str(error.response.get_header(CHALLENGE_HEADER) or "")
    except Exception:
        return False
    return error.status == HTTP_FORBIDDEN and header.lower() == CHALLENGE_VALUE


@functools.cache
def leaving_youtube_alone(handler: Any) -> Any:
    """yt-dlp's handler, as a YoutubeDL for another site gets it."""

    class OtherSitesOnlyRH(handler):  # type: ignore[misc, valid-type]
        """Hands a request for one of YouTube's hosts back to the ordinary handlers."""

        # yt-dlp reads these off the class name otherwise, and looks the handler up by them.
        RH_KEY = handler.RH_KEY
        RH_NAME = handler.RH_NAME
        # The director this handler was put into, through which the ordinary handlers are asked.
        director: Any = None

        def _validate(self, request: Any) -> None:
            if goes_to_youtube(request.url) and not request.extensions.get("impersonate"):
                raise UnsupportedRequest("YouTube is not reached by impersonation")
            super()._validate(request)

        def send(self, request: Any) -> Any:
            if not goes_to_youtube(request.url):
                return super().send(request)
            # Only a request that asks for impersonation itself gets this far, as YouTube's subtitle addresses do. No
            # ordinary handler takes it with that asking, so it goes back without, and the check above then turns it
            # away from here.
            plain = request.copy()
            plain.extensions.pop("impersonate", None)
            return self.director.send(plain)

    return OtherSitesOnlyRH


@functools.cache
def noting_class(base: Any) -> Any:
    class NotingYoutubeDL(base):  # type: ignore[misc, valid-type]
        """A YoutubeDL that keeps what a site refused it with, and is otherwise the one it is made from."""

        refusals: Optional[Refusals] = None

        def urlopen(self, req: Any) -> Any:
            try:
                return super().urlopen(req)
            except HTTPError as error:
                if self.refusals is not None:
                    self.refusals.note(error)
                raise

    return NotingYoutubeDL


@functools.cache
def session_class(base: Any) -> Any:
    class OtherSiteYoutubeDL(noting_class(base)):  # type: ignore[misc]
        """A YoutubeDL for the owner's link to another site: it can pass for a browser, and it keeps what the site
        refused it with."""

        @functools.cached_property
        def _request_director(self) -> Any:
            # yt-dlp's own, with the kept handlers added to its list.
            director = self.build_request_director([*_REQUEST_HANDLERS.values(), *_kept], _RH_PREFERENCES)
            for handler in director.handlers.values():
                if isinstance(handler, ImpersonateRequestHandler):
                    handler.director = director  # type: ignore[attr-defined]
            return director

    return OtherSiteYoutubeDL


def offered_to(url: str, other_sites: bool) -> bool:
    """Whether this link may be fetched as a browser would: only this machine's owner's link to another site. A link
    without a host, as written without https://, is read as YouTube's."""
    return bool(other_sites and _kept and host_of(url) and not is_youtube_url(url))


def browser_like(browser_major: Optional[int]) -> Any:
    """The browser yt-dlp passes for when asked for one like a Chrome-based browser of this version: the newest Chrome it
    can pass for that is not newer than that, or any browser when it knows none."""
    handler = next(iter(_taken.values()), None)
    if handler is not None and browser_major:
        known = [
            target
            for target in handler.supported_targets
            if target.client == "chrome" and str(target.version).isdigit() and int(target.version) <= browser_major and target.os not in ("android", "ios")
        ]
        if known:
            return max(known, key=lambda target: (int(target.version), target.os == "windows"))
    return any_browser()


def with_verified_session(opts: dict[str, Any], session: VerifiedSession) -> dict[str, Any]:
    """The options of a run for a site whose check the owner passed in a browser: asked as that browser asks. The
    owner's own headers stay over it, and a run that passes for a browser passes for one like that browser."""
    merged = dict(opts)
    headers = {name.title(): value for name, value in session.headers().items()}
    headers.update({name.title(): str(value).strip() for name, value in (opts.get("http_headers") or {}).items()})
    merged["http_headers"] = headers
    if merged.get("impersonate") is not None:
        merged["impersonate"] = browser_like(session.browser_major)
    return merged


def youtube_dl(base: Any, opts: dict[str, Any], url: str, other_sites: bool, refusals: Optional[Refusals] = None) -> Any:
    """The YoutubeDL for a link. `base` is yt_dlp.YoutubeDL as the caller's module knows it. A link that may be
    fetched as a browser would gets one that can do it. Every other one is never told to impersonate. For the owner's
    link to another site, either kind notes in `refusals`, when given, what the site refused it with. A link to a site
    whose check for a person the owner passed in a browser (see videocap.verification) gets that browser's cookies and
    way of introducing itself."""
    another_site = bool(other_sites and host_of(url) and not is_youtube_url(url))
    verified = verified_sites.session_for(url) if another_site else None
    if verified:
        opts = with_verified_session(opts, verified)
    if offered_to(url, other_sites):
        ydl = session_class(base)(opts)
        ydl.refusals = refusals
    else:
        if "impersonate" in opts:
            opts = {key: value for key, value in opts.items() if key != "impersonate"}
        if another_site and refusals is not None and not MISSING_FROM_YTDLP:
            ydl = noting_class(base)(opts)
            ydl.refusals = refusals
        else:
            ydl = base(opts)
    if verified:
        for cookie in verified.live_cookies():
            ydl.cookiejar.set_cookie(cookie.as_cookie())
    return ydl


def load() -> None:
    """Brings this module in line with what is installed and switched on, and checks that YouTube is left out. Never
    raises: the app has to open whatever yt-dlp has become."""
    global _kept, _problem, _unsupported
    with _lock:
        kept: tuple[Any, ...] = ()
        found = unsupported_because = ""
        try:
            if MISSING_FROM_YTDLP:
                raise ImportError(MISSING_FROM_YTDLP)
            switched_on = not impersonation_switch().exists()
            if switched_on:
                unsupported_because = register_with_ytdlp()
            take_from_ytdlp()
            # A package removed while the app runs stays in memory, and is not to be used any more.
            if switched_on and not unsupported_because and installed_version():
                kept = tuple(leaving_youtube_alone(handler) for handler in _taken.values())
        except Exception as error:
            found = f"{type(error).__name__}: {error}"
        if package_is_loaded():
            found = found or check()
        else:
            # Without the package in this process yt-dlp has nothing to impersonate with, whatever else is wrong.
            found = ""
        _kept, _problem, _unsupported = (() if found else kept), found, unsupported_because
        if found:
            # Not left for the owner to do: the next start is then safe whether or not anyone reads the warning.
            try:
                write_switch(BY_CHECK, found)
            except OSError:
                pass


def package_is_loaded() -> bool:
    return sys.modules.get(MODULE) is not None


def register_with_ytdlp() -> str:
    """Has yt-dlp list its curl-cffi handler when the package arrived after yt-dlp was imported: installed from the
    系統 page, or switched back on. Returns why yt-dlp would not take the package, or ""."""
    if _taken or any(issubclass(handler, ImpersonateRequestHandler) for handler in _REQUEST_HANDLERS.values()):
        return ""
    if MODULE in sys.modules and sys.modules[MODULE] is None:
        # Kept out at the start, while it was switched off.
        del sys.modules[MODULE]
    importlib.invalidate_caches()
    try:
        package = importlib.import_module(MODULE)
    except ImportError as error:
        return str(error) if installed_version() else ""
    import yt_dlp.dependencies as dependencies

    # yt-dlp looked for the package once, when it was imported, and its handler's module reads the answer from here.
    dependencies.curl_cffi = package  # type: ignore[attr-defined]
    try:
        importlib.import_module("yt_dlp.networking._curlcffi")
    except ImportError as error:
        return str(error)
    return ""


def take_from_ytdlp() -> None:
    for key, handler in list(_REQUEST_HANDLERS.items()):
        if issubclass(handler, ImpersonateRequestHandler):
            _taken[key] = _REQUEST_HANDLERS.pop(key)


def check() -> str:
    """Whether an ordinary YoutubeDL is what it would be without the package. Returns what is wrong, or ""."""
    try:
        with PlainYoutubeDL({"quiet": True, "logger": Silent()}) as ydl:  # type: ignore[arg-type]
            leaking = ydl._impersonate_target_available(ImpersonateTarget())
    except Exception as error:
        return f"無法確認（{type(error).__name__}: {error}）"
    return "一般的 yt-dlp 連線也找得到模擬瀏覽器" if leaking else ""


load()
