"""Passing a site's check for a person in a browser window of the app's own, and asking the site as that browser did.
The browser is a program of tests/fake_chromium.py, which answers what the window asks of one; with a real browser the
tests marked `browser` do the same."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import pytest
import yt_dlp

import videocap.verification as verification
from conftest import needs_ffmpeg
from fake_browser import browser_fixture, real_handler, recording, stand_in_handler  # noqa: F401
from offline_web import CLEARANCE_COOKIE, LocalSite, new_site_fixture, web_media_fixture  # noqa: F401
from offline_youtube import duration_of
from videocap import impersonation
from videocap.downloader import run_download
from videocap.errors import HUMAN_CHECK, failure_kind
from videocap.inspector import inspect_link
from videocap.options import AccessLimits, DownloadOptions, options_from_spec
from videocap.settings import DownloadSettings, JobSpec
from videocap.verification import SiteCookie, VerifiedSession, VerificationUnavailable


NewSite = Callable[[], LocalSite]
QUIET = {"quiet": True, "logger": impersonation.Silent()}
YOUTUBE_VIDEO = "https://www.youtube.com/watch?v=abc123def45"
PAGE = "https://site.example/watch/1"
# A browser newer than any the stand-in can pass for, and its way of introducing itself.
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/1000.0.0.0 Safari/537.36"
HINTS = (("Sec-Ch-Ua", '"Chromium";v="1000", "Not.A/Brand";v="24"'), ("Sec-Ch-Ua-Mobile", "?0"), ("Sec-Ch-Ua-Platform", '"Windows"'))
FAR = time.time() + 365 * 24 * 3600


class Progress:
    def progress(self, value: float, text: str = "") -> None:
        pass


def owner_options(url: str, folder: Path, **settings: Any) -> DownloadOptions:
    spec = JobSpec(url=url, settings=DownloadSettings(**settings), output_dir=str(folder))
    return options_from_spec(spec, AccessLimits())


def passed(host: str, clearance: str = "good", expires: Optional[float] = None, **extra: Any) -> VerifiedSession:
    """What a browser was given by a site whose check its owner passed."""
    cookie = SiteCookie(CLEARANCE_COOKIE, clearance, host, "/", FAR if expires is None else expires, False, True)
    return VerifiedSession(host, (cookie, SiteCookie("site", "abc", host)), AGENT, HINTS, "Chrome", time.time(), **extra)


@pytest.fixture(name="plain_site")
def plain_site_fixture(tmp_path: Path) -> Iterator[LocalSite]:
    """A site of text files, which needs no ffmpeg."""
    (tmp_path / "site").mkdir()
    site = LocalSite(tmp_path / "site", {})
    site.require_clearance("good", AGENT)
    yield site
    site.close()


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verification, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(verification, "SETTLE_SECONDS", 0.2)


# Cookies and sessions


@pytest.mark.parametrize(
    ("domain", "host", "expected"),
    [
        (".site.example", "site.example", True),
        (".site.example", "www.site.example", True),
        (".site.example", "other.example", False),
        # A name that only ends the same way is no subdomain.
        (".site.example", "notsite.example", False),
        ("www.site.example", "www.site.example", True),
        ("www.site.example", "site.example", False),
        ("www.site.example", "a.www.site.example", False),
    ],
)
def test_a_cookie_is_sent_to_its_own_site_and_the_subdomains_a_domain_cookie_names(domain: str, host: str, expected: bool) -> None:
    assert SiteCookie("a", "1", domain).applies_to(host) is expected


def test_a_cookie_becomes_the_cookie_a_jar_sends() -> None:
    cookie = SiteCookie("cf_clearance", "v", ".site.example", "/", 2_000_000_000.7, True, True).as_cookie()
    assert (cookie.name, cookie.value, cookie.domain, cookie.domain_initial_dot, cookie.secure) == ("cf_clearance", "v", ".site.example", True, True)
    assert cookie.expires == 2_000_000_000 and not cookie.discard and cookie.has_nonstandard_attr("HttpOnly")
    session_cookie = SiteCookie("a", "1", "site.example").as_cookie()
    assert session_cookie.expires is None and session_cookie.discard and not session_cookie.has_nonstandard_attr("HttpOnly")


def test_a_session_serves_until_its_clearance_runs_out() -> None:
    now = time.time()
    session = passed("site.example", expires=now + 100)
    assert session.usable(now) and session.covers(PAGE, now)
    # The other cookie has no end, and is no use without the clearance that went with it.
    assert not session.usable(now + 200) and not session.covers(PAGE, now + 200)
    # A site that never gave a clearance cookie has its session while any cookie lives.
    other = VerifiedSession("site.example", (SiteCookie("sid", "1", "site.example"),), AGENT)
    assert other.usable(now + 10**9)
    assert not VerifiedSession("site.example", (), AGENT).usable(now)


def test_a_session_covers_the_links_its_cookies_are_sent_to_and_no_others() -> None:
    session = passed("site.example")
    assert session.covers("https://site.example/a/b?c=d") and not session.covers("https://other.example/") and not session.covers("not a link")


def test_a_session_says_how_the_browser_introduces_itself() -> None:
    session = passed("site.example")
    assert session.headers() == {"User-Agent": AGENT, **dict(HINTS)} and session.browser_major == 1000
    edge = VerifiedSession("site.example", (), "Mozilla/5.0 Chrome/158.0.0.0 Safari/537.36 Edg/158.0.0.0")
    assert edge.browser_major == 158 and VerifiedSession("site.example", ()).headers() == {} and VerifiedSession("a", ()).browser_major is None


def test_the_registry_keeps_a_session_for_each_site_until_it_is_forgotten() -> None:
    sites = verification.VerifiedSites()
    sites.add(passed("site.example"))
    sites.add(passed("other.example", expires=time.time() - 5))
    assert sites.session_for(PAGE) is not None and sites.session_for("https://other.example/") is None
    # A session that ran out is not listed either.
    assert [session.host for session in sites.all()] == ["site.example"]
    sites.add(passed("site.example", clearance="newer"))
    assert sites.session_for(PAGE).cookies[0].value == "newer"  # type: ignore[union-attr]
    sites.forget("site.example")
    assert sites.session_for(PAGE) is None
    sites.add(passed("site.example"))
    sites.clear()
    assert sites.all() == []


# Where the window opens, and whether it can


def test_a_page_is_opened_as_it_was_and_a_file_or_stream_by_its_site() -> None:
    assert verification.opening_address("https://site.example/watch/1?x=1") == "https://site.example/watch/1?x=1"
    assert verification.opening_address("https://cdn.example:8080/a/b/index.m3u8?t=1") == "https://cdn.example:8080/"
    assert verification.opening_address("https://cdn.example/a.mp4") == "https://cdn.example/"


def test_without_a_browser_no_window_is_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verification, "known_browsers", lambda: [])
    assert verification.unavailable_reason() == verification.NO_BROWSER_MESSAGE
    with pytest.raises(VerificationUnavailable, match="找不到 Chrome"):
        verification.start_window(PAGE)


@pytest.mark.parametrize("address", ["--remote-allow-origins=*", "file:///C:/Windows/win.ini", "chrome://settings", "javascript:alert(1)", "site.example/watch/1", ""])
def test_only_a_web_address_is_given_to_the_browser(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    opened: list[Any] = []
    monkeypatch.setattr(verification, "launch", lambda *args: opened.append(args))
    with pytest.raises(VerificationUnavailable, match=verification.NOT_A_WEB_ADDRESS):
        verification.start_window(address)
    assert not opened


@pytest.mark.parametrize(
    "proxy, expected",
    [
        ("", ""),
        ("   ", ""),
        ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("HTTPS://proxy.example:8443/", "https://proxy.example:8443"),
        ("socks5h://proxy.example:1080", "socks5://proxy.example:1080"),
        ("socks5://someone:secret@proxy.example:1080", "socks5://proxy.example:1080"),
        ("http://[::1]:3128", "http://[::1]:3128"),
        ("ftp://proxy.example:21", ""),
        ("http://", ""),
        ("http://proxy.example:not-a-port", ""),
    ],
)
def test_the_proxy_of_the_app_is_given_to_the_browser_as_it_takes_one(proxy: str, expected: str) -> None:
    assert verification.browser_proxy(proxy) == expected


def test_a_machine_with_no_desktop_opens_no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verification, "known_browsers", lambda: [verification.Browser("Chrome", Path("chrome"))])
    monkeypatch.setattr(sys, "platform", "linux")
    for name in ("DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    assert verification.unavailable_reason() == verification.NO_DESKTOP_MESSAGE
    monkeypatch.setenv("DISPLAY", ":0")
    assert verification.unavailable_reason() == ""


def test_the_owners_choice_of_browser_comes_first_and_chrome_before_edge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "programs"
    for relative in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe"):
        # Joined as the app joins it: folders on Windows, and one file with backslashes in its name anywhere else,
        # where this test still runs with the platform said to be Windows.
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
    chosen = tmp_path / "my-browser.exe"
    chosen.write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", str(root))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "nowhere"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nowhere"))
    monkeypatch.delenv(verification.BROWSER_ENV, raising=False)
    assert [browser.name for browser in verification.known_browsers()] == ["Chrome", "Edge"]
    monkeypatch.setenv(verification.BROWSER_ENV, str(chosen))
    assert [browser.name for browser in verification.known_browsers()][:2] == ["my-browser", "Chrome"]


# The window, with a browser that opens none


def fake_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **scenario: Any) -> None:
    """A browser for the app to find: a program that stands in for one, doing what the scenario says."""
    script = Path(__file__).with_name("fake_chromium.py")
    if sys.platform == "win32":
        launcher = tmp_path / "fake-browser.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = tmp_path / "fake-browser"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    monkeypatch.setenv(verification.BROWSER_ENV, str(launcher))
    monkeypatch.setenv("FAKE_CHROMIUM", json.dumps(scenario))
    monkeypatch.setenv("DISPLAY", ":0")


def reach(window: verification.VerificationWindow, state: str, seconds: float = 20.0) -> verification.WindowStatus:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = window.status()
        if status.state == state:
            return status
        time.sleep(0.05)
    raise AssertionError(f"the window is {window.status().state}, not {state}: {window.status().message}")


def until(window: verification.VerificationWindow, ready: Callable[[verification.WindowStatus], bool], seconds: float = 20.0) -> verification.WindowStatus:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if ready(window.status()):
            return window.status()
        time.sleep(0.05)
    raise AssertionError(f"the window is {window.status().state}: {window.status().message}")


def ended(window: verification.VerificationWindow) -> verification.WindowStatus:
    window.join(30)
    assert not window.status().active
    return window.status()


def profiles_left() -> list[str]:
    return [folder.name for folder in Path(tempfile.gettempdir()).glob(f"{verification.PROFILE_DIR_PREFIX}*")]


CLEARANCE = {"name": "cf_clearance", "value": "good", "domain": ".site.example", "path": "/", "expires": FAR, "httpOnly": True, "secure": True}
SITE_COOKIE = {"name": "site", "value": "abc", "domain": "site.example", "path": "/", "expires": -1, "httpOnly": False, "secure": False}
ELSEWHERE = {"name": "tracker", "value": "x", "domain": ".other.example", "path": "/", "expires": FAR, "httpOnly": False, "secure": False}
BRAND_HINTS = {"brands": [{"brand": "Not-A.Brand", "version": "99"}, {"brand": "Google Chrome", "version": "157"}], "mobile": False, "platform": "Windows"}


def test_a_window_waits_for_the_check_and_keeps_what_the_browser_was_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_browser(tmp_path, monkeypatch, cookies=[SITE_COOKIE, ELSEWHERE], clearance={"after": 0.3, "cookie": CLEARANCE}, hints=BRAND_HINTS)
    window = verification.start_window(PAGE)
    reach(window, verification.WAITING)
    waiting = until(window, lambda status: bool(status.page_title))
    assert waiting.host == "site.example" and waiting.address == PAGE and waiting.page_title == "Just a moment..." and waiting.seconds_left > 0
    final = ended(window)
    assert final.state == verification.PASSED and "site.example" in final.message
    session = final.session
    assert session is not None
    # The site's cookies, and no cookie of any other site.
    assert sorted(cookie.name for cookie in session.cookies) == ["cf_clearance", "site"]
    assert {cookie.name: cookie.http_only for cookie in session.cookies} == {"cf_clearance": True, "site": False}
    assert session.user_agent.endswith("Safari/537.36") and session.browser == "fake-browser" and session.browser_major == 157
    assert dict(session.client_hints) == {
        "Sec-Ch-Ua": '"Not-A.Brand";v="99", "Google Chrome";v="157"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }
    assert verification.verified_sites.session_for("https://site.example/anything") is session
    assert verification.current_window() is window
    # The browser ended with it, and its profile went away.
    assert profiles_left() == []


def test_the_window_goes_the_way_the_apps_own_requests_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "arguments.json"
    fake_browser(tmp_path, monkeypatch, cookies=[CLEARANCE], argv_log=str(log))
    ended(verification.start_window(PAGE, proxy="socks5://someone:secret@proxy.example:1080"))
    arguments = json.loads(log.read_text(encoding="utf-8"))
    # Through the same proxy, without its login on a command line, and the page is what the browser is given last.
    assert "--proxy-server=socks5://proxy.example:1080" in arguments and not any("secret" in argument for argument in arguments)
    assert arguments[-1] == PAGE and any(argument.startswith("--user-data-dir=") for argument in arguments)
    # Without one, the browser's own settings stay.
    ended(verification.start_window(PAGE))
    assert not any(argument.startswith("--proxy-server") for argument in json.loads(log.read_text(encoding="utf-8")))


def test_a_browser_that_cannot_tell_its_client_hints_still_gives_its_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_browser(tmp_path, monkeypatch, cookies=[CLEARANCE], hints=None)
    session = ended(verification.start_window(PAGE)).session
    assert session is not None and session.client_hints == () and session.headers() == {"User-Agent": session.user_agent}


def test_the_owner_says_the_check_is_passed_for_a_site_whose_cookie_the_window_does_not_know(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_browser(tmp_path, monkeypatch, cookies=[SITE_COOKIE])
    window = verification.start_window(PAGE)
    reach(window, verification.WAITING)
    time.sleep(0.3)
    # Nothing in the browser says it is passed, so the window goes on waiting.
    assert window.status().state == verification.WAITING
    window.finish()
    final = ended(window)
    assert final.state == verification.PASSED and final.session is not None and [cookie.name for cookie in final.session.cookies] == ["site"]


def test_saying_it_is_passed_with_nothing_to_show_for_it_is_no_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_browser(tmp_path, monkeypatch, cookies=[ELSEWHERE])
    window = verification.start_window(PAGE)
    reach(window, verification.WAITING)
    window.finish()
    final = ended(window)
    assert final.state == verification.FAILED and "site.example" in final.message
    assert verification.verified_sites.all() == [] and profiles_left() == []


def test_a_window_the_owner_cancels_closes_the_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_browser(tmp_path, monkeypatch)
    window = verification.start_window(PAGE)
    reach(window, verification.WAITING)
    window.cancel()
    assert ended(window).state == verification.CANCELED and profiles_left() == [] and verification.verified_sites.all() == []


def test_a_window_the_owner_closes_ends_as_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_browser(tmp_path, monkeypatch, exit_after=0.6)
    window = verification.start_window(PAGE)
    final = ended(window)
    assert final.state == verification.CLOSED and "關閉" in final.message and profiles_left() == []


def test_a_window_gives_up_after_the_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verification, "WAIT_SECONDS", 0.5)
    fake_browser(tmp_path, monkeypatch)
    final = ended(verification.start_window(PAGE))
    assert final.state == verification.TIMED_OUT and profiles_left() == []


def test_a_browser_that_cannot_start_is_a_failure_that_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_browser(tmp_path, monkeypatch, exit_at_once=True)
    final = ended(verification.start_window(PAGE))
    assert final.state == verification.FAILED and "沒有順利開啟" in final.message and profiles_left() == []


def test_only_one_window_is_open_at_a_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_browser(tmp_path, monkeypatch)
    window = verification.start_window(PAGE)
    # The same site asks again: it is the same window.
    assert verification.start_window("https://site.example/watch/2") is window
    with pytest.raises(VerificationUnavailable, match=verification.BUSY_MESSAGE):
        verification.start_window("https://other.example/")
    window.cancel()
    ended(window)
    assert verification.start_window("https://other.example/") is not window
    verification.current_window().cancel()  # type: ignore[union-attr]
    ended(verification.current_window())  # type: ignore[arg-type]


def test_the_last_window_is_remembered_until_the_page_forgets_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_browser(tmp_path, monkeypatch)
    window = verification.start_window(PAGE)
    reach(window, verification.WAITING)
    # One that is open is not forgotten.
    verification.forget_window()
    assert verification.current_window() is window
    window.cancel()
    ended(window)
    assert verification.current_window() is window
    verification.forget_window()
    assert verification.current_window() is None


def test_profiles_left_by_an_app_that_was_killed_are_removed_when_the_next_window_opens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old, fresh = Path(tempfile.gettempdir()) / f"{verification.PROFILE_DIR_PREFIX}old", Path(tempfile.gettempdir()) / f"{verification.PROFILE_DIR_PREFIX}fresh"
    for folder in (old, fresh):
        folder.mkdir()
    long_ago = time.time() - verification.STALE_PROFILE_SECONDS - 60
    os.utime(old, (long_ago, long_ago))
    fake_browser(tmp_path, monkeypatch, cookies=[CLEARANCE])
    ended(verification.start_window(PAGE))
    assert not old.exists() and fresh.exists()
    fresh.rmdir()


# Asking the site as the browser did


def test_a_page_that_wants_a_person_opens_once_the_check_was_passed(browser: Any, plain_site: LocalSite, tmp_path: Path) -> None:
    site = plain_site
    site.write_page("verified/watch.html", "Behind a check", "<p>no video</p>")
    url = site.url("verified/watch.html")
    # As the app has always said it: tried as a browser, and the check still wants a person.
    error = inspect_link(url, owner_options(url, tmp_path))["error"]
    assert error.startswith("這個網站要求真人驗證") and failure_kind(error) == HUMAN_CHECK

    verification.verified_sites.add(passed("127.0.0.1"))
    # Now the page is read, and says what a page without a video says.
    assert inspect_link(url, owner_options(url, tmp_path))["error"].startswith("這個網址裡找不到可以下載的影片")
    for headers in site.headers_for("verified/watch.html")[-2:]:
        # The cookies of the check, and the way the browser that passed it introduces itself, as that browser sends them.
        assert f"{CLEARANCE_COOKIE}=good" in headers["cookie"] and "site=abc" in headers["cookie"]
        assert headers["user-agent"] == AGENT and headers["sec-ch-ua"] == dict(HINTS)["Sec-Ch-Ua"] and headers["sec-ch-ua-platform"] == '"Windows"'


@needs_ffmpeg
def test_a_file_behind_a_check_downloads_once_the_check_was_passed(browser: Any, new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    site.require_clearance("good", AGENT)
    site.add_clip("browsers-only/verified/clip.mp4")
    url = site.add_page("watch.html", ("browsers-only/verified/clip.mp4",), title="Behind a check")
    verification.verified_sites.add(passed("127.0.0.1"))
    outcome = run_download(url, owner_options(url, tmp_path), Progress())
    assert outcome["errors"] == [] and duration_of(Path(outcome["items"][0]["path"])) == pytest.approx(2, abs=0.3)
    # The clip refuses a program, so it was asked for as a browser, and still as the one that passed the check.
    assert site.asked_as_a_browser("browsers-only/verified/clip.mp4") >= 1
    assert all(headers["user-agent"] == AGENT for headers in site.headers_for("browsers-only/verified/clip.mp4"))


def test_a_check_that_was_passed_for_another_site_changes_nothing() -> None:
    verification.verified_sites.add(passed("site.example"))
    opts = {"http_headers": {"Referer": "https://x/"}, "impersonate": impersonation.any_browser(), **QUIET}
    with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(opts), "https://other.example/watch", True) as ydl:
        assert ydl.params["http_headers"]["Referer"] == "https://x/" and ydl.params["http_headers"]["User-Agent"] != AGENT
        assert len(ydl.cookiejar) == 0


def test_the_session_adds_its_cookies_and_headers_and_leaves_what_the_run_had() -> None:
    verification.verified_sites.add(passed("site.example"))
    opts = {"http_headers": {"referer": " https://x/ "}, **QUIET}
    with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(opts), PAGE, True) as ydl:
        assert ydl.params["http_headers"]["User-Agent"] == AGENT and ydl.params["http_headers"]["Referer"] == "https://x/"
        assert ydl.params["http_headers"]["Sec-Ch-Ua-Mobile"] == "?0"
        assert sorted(cookie.name for cookie in ydl.cookiejar) == ["cf_clearance", "site"]
    # The options that were passed in are the caller's, and stay as they were.
    assert opts["http_headers"] == {"referer": " https://x/ "}


def test_the_owners_own_user_agent_stays_over_the_browsers() -> None:
    verification.verified_sites.add(passed("site.example"))
    with impersonation.youtube_dl(yt_dlp.YoutubeDL, {"http_headers": {"User-Agent": "Mine/1"}, **QUIET}, PAGE, True) as ydl:
        assert ydl.params["http_headers"]["User-Agent"] == "Mine/1" and ydl.params["http_headers"]["Sec-Ch-Ua-Platform"] == '"Windows"'


@pytest.mark.parametrize(("url", "other_sites"), [(YOUTUBE_VIDEO, True), ("https://youtu.be/abc123def45", True), (PAGE, False)], ids=["YouTube", "youtu.be", "a visitor"])
def test_a_passed_check_is_never_carried_to_youtube_or_to_a_visitor(url: str, other_sites: bool) -> None:
    # Even for a session that names YouTube's own hosts.
    verification.verified_sites.add(passed("site.example"))
    verification.verified_sites.add(passed("www.youtube.com"))
    verification.verified_sites.add(passed("youtu.be"))
    with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(QUIET), url, other_sites) as ydl:
        assert ydl.params["http_headers"]["User-Agent"] != AGENT and len(ydl.cookiejar) == 0


def test_a_run_that_passes_for_a_browser_passes_for_one_like_the_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = stand_in_handler()
    monkeypatch.setattr(impersonation, "_taken", {handler.RH_KEY: handler})
    target = impersonation.browser_like(1000)
    assert (target.client, target.version) == ("chrome", "999")
    # A browser older than any it can pass for, or one whose version is not known: any.
    assert impersonation.browser_like(998) == impersonation.any_browser() and impersonation.browser_like(None) == impersonation.any_browser()
    options = impersonation.with_verified_session({"impersonate": impersonation.any_browser(), **QUIET}, passed("site.example"))
    assert (options["impersonate"].client, options["impersonate"].version) == ("chrome", "999")
    # A run that did not pass for a browser still does not.
    assert "impersonate" not in impersonation.with_verified_session(dict(QUIET), passed("site.example"))


def test_without_the_package_a_browser_like_the_owners_is_any_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(impersonation, "_taken", {})
    assert impersonation.browser_like(157) == impersonation.any_browser()


# With a real browser


@pytest.mark.browser
def test_a_real_browser_gives_the_session_a_page_leaves_it(new_site: NewSite, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Needs Chrome, Edge or Brave on this machine, which is run without a window: pytest -m browser."""
    import http.server
    import threading

    real = verification.find_browser()
    if real is None:
        pytest.skip("no Chromium-based browser here")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", f"{CLEARANCE_COOKIE}=real; Path=/; Max-Age=600; HttpOnly")
            self.end_headers()
            self.wfile.write(b"<html><head><title>Passed</title></head><body>ok</body></html>")

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        if sys.platform == "win32":
            launcher = tmp_path / "headless.cmd"
            launcher.write_text(f'@"{real.path}" --headless=new %*\r\n', encoding="utf-8")
        else:
            launcher = tmp_path / "headless"
            launcher.write_text(f'#!/bin/sh\nexec "{real.path}" --headless=new --no-sandbox "$@"\n', encoding="utf-8")
            launcher.chmod(0o755)
        monkeypatch.setenv(verification.BROWSER_ENV, str(launcher))
        window = verification.start_window(f"http://127.0.0.1:{server.server_address[1]}/watch")
        final = ended(window)
        assert final.state == verification.PASSED, final.message
        session = final.session
        assert session is not None and [cookie.name for cookie in session.cookies] == [CLEARANCE_COOKIE] and session.cookies[0].http_only
        assert "Chrome/" in session.user_agent and session.browser_major and any(name == "Sec-Ch-Ua" for name, _ in session.client_hints)
        assert profiles_left() == []
    finally:
        server.shutdown()
